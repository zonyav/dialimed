from __future__ import annotations

import logging
import re

import httpx

from . import __version__

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases/latest"

_checked: dict | None = None


def build_info() -> tuple[str, str]:

    version, repo = __version__, ""
    try:
        from . import _build  # создаётся при сборке на GitHub
        version = getattr(_build, "VERSION", version) or version
        repo = getattr(_build, "REPO", "") or ""
    except ImportError:
        pass
    return version, repo


def _numbers(tag: str) -> tuple[int, ...]:

    return tuple(int(x) for x in re.findall(r"\d+", tag or "")) or (0,)


def _newer(latest: str, current: str) -> bool:
    a, b = _numbers(latest), _numbers(current)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


async def check() -> dict:

    global _checked

    version, repo = build_info()
    if _checked is not None:
        return _checked
    result = {"version": version, "new": False, "latest": "", "url": ""}
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
        result.update(new=True, latest=tag.lstrip("vV"),
                      url=str(data.get("html_url") or ""))
    _checked = result
    return result
