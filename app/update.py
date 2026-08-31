"""Проверка и установка новой версии.

Программа живёт одним файлом .exe, поэтому обновление — это подмена самого
файла: новый скачивается рядом со старым, старый переименовывается, новый
встаёт на его место. Работающий exe переименовать можно, перезаписать —
нет, поэтому именно так; прежняя версия удаляется при следующем запуске.

Скачанное программа никогда не запускает и посторонних адресов не знает:
файл берётся только из релиза того репозитория, что записан в сборку,
и сверяется с контрольной суммой, которую сообщает GitHub.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases/latest"

# откуда GitHub отдаёт файлы релизов — с чужого адреса не качаем
ASSET_HOSTS = {"github.com", "objects.githubusercontent.com",
               "release-assets.githubusercontent.com"}
MIN_SIZE = 3 * 1024 * 1024      # exe весит ~21 МБ; меньшее — не наша сборка
MAX_SIZE = 300 * 1024 * 1024
CHUNK = 256 * 1024

_checked: dict | None = None
_asset: dict = {}
_installed = ""


def build_info() -> tuple[str, str]:

    version, repo = __version__, ""
    try:
        from . import _build  # создаётся при сборке на GitHub
        version = getattr(_build, "VERSION", version) or version
        repo = getattr(_build, "REPO", "") or ""
    except ImportError:
        pass
    return version, repo


def exe_path() -> Path | None:
    """Файл программы — только если она собрана в exe."""

    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def _replaceable(exe: Path | None) -> bool:
    """Можно ли положить новый файл рядом со старым."""

    if exe is None:
        return False
    probe = exe.with_name(exe.name + ".проверка")
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def sweep() -> None:
    """Убрать следы прошлого обновления.

    Прежняя версия занята, пока программа работает, — удалить её можно
    только при следующем запуске. Недокачанный файл убираем заодно.
    """

    exe = exe_path()
    if exe is None:
        return
    leftovers = list(exe.parent.glob("*.exe.old*"))
    leftovers += list(exe.parent.glob("*.exe.new"))
    for item in leftovers:
        try:
            item.unlink()
        except OSError as e:
            log.debug("не удалось убрать %s: %s", item.name, e)


def _numbers(tag: str) -> tuple[int, ...]:

    return tuple(int(x) for x in re.findall(r"\d+", tag or "")) or (0,)


def _newer(latest: str, current: str) -> bool:
    a, b = _numbers(latest), _numbers(current)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def _pick_asset(assets: list) -> dict:
    """Самый большой .exe релиза — это и есть программа."""

    best: dict = {}
    for a in assets:
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        size = int(a.get("size") or 0)
        if not name.lower().endswith(".exe") or not url.startswith("https://"):
            continue
        if (urlsplit(url).hostname or "") not in ASSET_HOSTS:
            continue
        if size <= int(best.get("size") or 0):
            continue
        best = {"name": name, "url": url, "size": size,
                "digest": str(a.get("digest") or "")}
    return best


async def check() -> dict:

    global _checked, _asset

    version, repo = build_info()
    if _checked is not None:
        return _checked
    result = {"version": version, "new": False, "latest": "", "url": "",
              "can_install": False}
    if not repo:
        _checked = result
        return result
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            r = await client.get(API.format(repo=repo),
                                 headers={"Accept": "application/vnd.github+json"})
            if r.status_code != 200:
                log.debug("проверка обновлений: GitHub ответил %s", r.status_code)
                return result
            data = r.json()
    except Exception as e:
        log.debug("проверка обновлений: %s: %s", type(e).__name__, e)
        return result

    tag = str(data.get("tag_name") or "")
    if tag and _newer(tag, version):
        _asset = _pick_asset(data.get("assets") or [])
        result.update(new=True, latest=tag.lstrip("vV"),
                      url=str(data.get("html_url") or ""),
                      can_install=bool(_asset) and _replaceable(exe_path()))
    _checked = result
    return result


def _drop(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _old_name(exe: Path) -> Path:
    """Свободное имя для прежней версии."""

    for n in range(20):
        cand = exe.with_name(exe.name + ".old" + (str(n) if n else ""))
        if not cand.exists():
            return cand
        try:
            cand.unlink()
            return cand
        except OSError:
            continue
    return exe.with_name(exe.name + ".old")


async def _download(url: str, target: Path) -> tuple[str, int, bytes]:
    """Скачать файл; вернуть sha256, размер и первые байты."""

    sha = hashlib.sha256()
    size = 0
    head = b""
    timeout = httpx.Timeout(600.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream(
                "GET", url, headers={"Accept": "application/octet-stream"}) as r:
            if r.status_code != 200:
                raise RuntimeError(f"GitHub ответил {r.status_code}")
            if (r.url.host or "") not in ASSET_HOSTS:
                raise RuntimeError("файл предлагают скачать с постороннего адреса")
            with open(target, "wb") as f:
                async for chunk in r.aiter_bytes(CHUNK):
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise RuntimeError("файл неправдоподобно большой")
                    if not head:
                        head = chunk[:2]
                    sha.update(chunk)
                    f.write(chunk)
    return sha.hexdigest(), size, head


async def install() -> dict:
    """Скачать новую версию и подменить ею файл программы."""

    global _installed

    exe = exe_path()
    if exe is None:
        return {"ok": False,
                "error": "Обновиться умеет только собранная программа (.exe)."}
    if _installed:
        return {"ok": True, "version": _installed}
    info = await check()
    if not info.get("new"):
        return {"ok": False, "error": "У вас и так последняя версия."}
    if not _asset.get("url"):
        return {"ok": False, "error": "В релизе нет файла программы — "
                                      "скачайте его со страницы загрузки."}
    if not _replaceable(exe):
        return {"ok": False,
                "error": "Папка с программой закрыта на запись. Перенесите "
                         "программу, например, на рабочий стол — или скачайте "
                         "новый файл со страницы загрузки вручную."}

    tmp = exe.with_name(exe.name + ".new")
    try:
        got, size, head = await _download(_asset["url"], tmp)
    except Exception as e:
        _drop(tmp)
        log.warning("обновление: не скачалось: %s: %s", type(e).__name__, e)
        return {"ok": False, "error": f"Не получилось скачать: {e}"}

    # сумму считает сам GitHub; если он её не прислал, остаётся то, что файл
    # пришёл по https из релиза нужного репозитория
    want = str(_asset.get("digest") or "").split(":")[-1].strip().lower()
    if want and got != want:
        _drop(tmp)
        log.warning("обновление: сумма не сошлась (%s вместо %s)", got, want)
        return {"ok": False, "error": "Скачанный файл повреждён — обновление "
                                      "отменено, ничего не тронуто."}
    if size < MIN_SIZE or head != b"MZ":
        _drop(tmp)
        return {"ok": False, "error": "Скачалась не программа — обновление "
                                      "отменено, ничего не тронуто."}

    old = _old_name(exe)
    try:
        os.replace(exe, old)
    except OSError as e:
        _drop(tmp)
        log.warning("обновление: старый файл не отодвинулся: %s", e)
        return {"ok": False, "error": "Файл программы занят — закройте её "
                                      "вторую копию и попробуйте ещё раз."}
    try:
        os.replace(tmp, exe)
    except OSError as e:
        os.replace(old, exe)
        _drop(tmp)
        log.warning("обновление: новый файл не встал на место: %s", e)
        return {"ok": False, "error": "Новый файл не встал на место — "
                                      "осталась прежняя версия."}

    _installed = str(info.get("latest") or "")
    log.info("обновление установлено: версия %s", _installed)
    return {"ok": True, "version": _installed}
