from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _base_dir() -> Path:

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _fallback_dir() -> Path:

    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(local) if local else Path.home()
    return root / "Медизделия ЕИС"


def _writable(path: Path) -> bool:

    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".запись"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


ROOT = _base_dir()
if not _writable(ROOT / "data"):
    ROOT = _fallback_dir()

DATA = ROOT / "data"
CACHE = DATA / "cache"
OUT = ROOT / "out"
for _p in (DATA, CACHE, OUT):
    _p.mkdir(parents=True, exist_ok=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _flt(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(slots=True)
class Settings:
    eis_base: str = "https://zakupki.gov.ru"
    eis_rate: float = _flt("MI_EIS_RATE", 1.3)
    eis_cooldown: float = _flt("MI_EIS_COOLDOWN", 80.0)
    eis_concurrency: int = _int("MI_EIS_CONCURRENCY", 4)
    eis_timeout: float = _flt("MI_EIS_TIMEOUT", 90.0)
    eis_retries: int = _int("MI_EIS_RETRIES", 3)
    eis_page_size: int = 50
    eis_max_pages: int = _int("MI_EIS_MAX_PAGES", 40)

    rzn_enabled: bool = _bool("MI_RZN_ENABLED", True)
    rzn_concurrency: int = _int("MI_RZN_CONCURRENCY", 3)
    rzn_timeout: float = _flt("MI_RZN_TIMEOUT", 45.0)

    kind_slice_enabled: bool = _bool("MI_KIND_SLICE", True)
    kind_slice_max: int = _int("MI_KIND_SLICE_MAX", 1200)

    # ИИ выключен, пока пользователь не включит его сам и не введёт свой ключ.
    # Ключ живёт в data/ai.json, зашифрованный средствами Windows (см. _dpapi),
    # в репозиторий не попадает никогда; переменная окружения перебивает файл.
    ai_base: str = os.environ.get("MI_AI_BASE", "") or "https://api.odirouter.ai/v1"
    ai_model: str = os.environ.get("MI_AI_MODEL", "") or "gemini-3.7-flash"
    ai_timeout: float = _flt("MI_AI_TIMEOUT", 60.0)
    ai_concurrency: int = _int("MI_AI_CONCURRENCY", 4)
    # шагов диалога на позицию. На живом прогоне девять позиций упёрлись в
    # прежний предел 6; при 10 две из семи проверенных нашлись — а платим мы
    # только за трудные, которых мало
    ai_steps: int = _int("MI_AI_STEPS", 10)

    idle_hours: float = _flt("MI_IDLE_HOURS", 6.0)

    cache_enabled: bool = _bool("MI_CACHE_ENABLED", True)
    cache_ttl_days: int = _int("MI_CACHE_TTL_DAYS", 30)
    cache_max_mb: int = _int("MI_CACHE_MAX_MB", 400)
    cache_db: Path = CACHE / "cache.sqlite"

    out_dir: Path = OUT
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @property
    def http_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }


settings = Settings()

AI_FILE = DATA / "ai.json"

# Что предлагать в списке моделей. Точность — из замеров на двух наборах КТРУ
# (эндоскопы и 18 кодов пользователя), а не из описания моделей. Цена за
# миллион токенов — чтобы программа сама показывала, во что обошёлся прогон,
# а не отсылала пользователя считать на сайт шлюза.
AI_MODELS: list[dict] = [
    {"id": "gemini-3.7-flash", "note": "точность 98%, дороже",
     "in": 0.225, "out": 1.125},
    {"id": "gemini-3.1-flash-lite", "note": "в пять раз дешевле, точность 90%",
     "in": 0.049, "out": 0.147},
]


def ai_price(model: str) -> tuple[float, float]:
    """Цена за миллион токенов, вход и выход. Ноль — модель незнакомая, и
    стоимость прогона тогда просто не показывается: врать про деньги нельзя."""

    for m in AI_MODELS:
        if m["id"] == model:
            return float(m.get("in") or 0), float(m.get("out") or 0)
    return 0.0, 0.0


def _dpapi(name: str, data: bytes) -> Optional[bytes]:
    """Шифрование средствами самой Windows (DPAPI): ключ, зашифрованный так,
    расшифровывается только под этой учётной записью и на этой машине. Ни
    пароля, ни своего хранилища заводить не нужно, и в сборку не добавляется
    ни одной библиотеки. Не Windows или отказ — возвращаем None, и вызывающий
    сохраняет как есть: программа без ключа полезнее программы с ошибкой."""

    if sys.platform != "win32" or not data:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        source = Blob(len(data),
                      ctypes.cast(ctypes.create_string_buffer(data),
                                  ctypes.POINTER(ctypes.c_char)))
        out = Blob()
        # UI_FORBIDDEN: у программы может не быть окна, и запрос от Windows
        # повис бы невидимым диалогом
        ok = getattr(ctypes.windll.crypt32, name)(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(out))
        if not ok:
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return None


def _lock_key(key: str) -> Optional[str]:
    import base64

    sealed = _dpapi("CryptProtectData", key.encode("utf-8"))
    return base64.b64encode(sealed).decode("ascii") if sealed else None


def _unlock_key(sealed: str) -> str:
    import base64

    try:
        raw = base64.b64decode(sealed.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return ""
    opened = _dpapi("CryptUnprotectData", raw)
    return opened.decode("utf-8", "replace") if opened else ""


def _read_ai_file() -> dict:
    import json

    try:
        saved = json.loads(AI_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _write_ai_file(saved: dict) -> None:
    import json

    AI_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=1),
                       encoding="utf-8")


def ai_options() -> dict:
    """Ключ, модель и адрес шлюза: сначала файл рядом с данными, поверх него —
    переменные окружения. Ключ не попадает ни в репозиторий, ни в exe, ни в
    отчёт, ни на страницу: единственный адрес, куда он уходит, — сам шлюз,
    в заголовке запроса, иначе тот не ответит."""

    saved = _read_ai_file()
    key = str(saved.get("key") or "")
    sealed = str(saved.get("key_protected") or "")
    if sealed:
        key = _unlock_key(sealed)
    elif key:
        # ключ из старой версии лежит открытым — запираем его при первом чтении
        locked = _lock_key(key)
        if locked:
            saved.pop("key", None)
            saved["key_protected"] = locked
            try:
                _write_ai_file(saved)
            except OSError:
                pass
    return {
        "key": os.environ.get("MI_AI_KEY") or key,
        "model": os.environ.get("MI_AI_MODEL") or str(saved.get("model") or "")
        or settings.ai_model,
        "base": os.environ.get("MI_AI_BASE") or str(saved.get("base") or "")
        or settings.ai_base,
        "enabled": bool(saved.get("enabled")),
        "protected": bool(saved.get("key_protected")),
        # чем кончилась последняя проверка ключа: страница показывает это
        # вместо пустого поля ввода, чтобы ключ не вводили второй раз
        "checked_ok": saved.get("checked_ok"),
        "checked_at": str(saved.get("checked_at") or ""),
        "checked_note": str(saved.get("checked_note") or ""),
    }


def save_ai_options(*, key: str | None = None, model: str | None = None,
                    enabled: bool | None = None,
                    checked: tuple[bool, str] | None = None) -> dict:
    """Сохраняет то, что задал пользователь: один раз ввёл — больше не спросят.
    Пустой ключ стирает сохранённый — иначе убрать его можно только удалением
    файла. Новый ключ стирает и отметку о проверке: она была про старый."""

    import time as _time

    saved = _read_ai_file()
    if key is not None:
        saved.pop("key", None)
        saved.pop("key_protected", None)
        for stale in ("checked_ok", "checked_at", "checked_note"):
            saved.pop(stale, None)
        key = key.strip()
        if key:
            locked = _lock_key(key)
            if locked:
                saved["key_protected"] = locked
            else:
                saved["key"] = key
    if model is not None:
        saved["model"] = model.strip()
    if enabled is not None:
        saved["enabled"] = bool(enabled)
    if checked is not None:
        ok, note = checked
        saved["checked_ok"] = bool(ok)
        saved["checked_at"] = _time.strftime("%d.%m.%Y, %H:%M")
        saved["checked_note"] = note
    _write_ai_file(saved)
    return ai_options()

STAGES: dict[str, str] = {
    "0": "Исполнение",
    "1": "Исполнение завершено",
    "2": "Исполнение прекращено",
    "3": "Расторжение",
}
DEFAULT_STAGES = ["0", "1", "2"]
