from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


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

STAGES: dict[str, str] = {
    "0": "Исполнение",
    "1": "Исполнение завершено",
    "2": "Исполнение прекращено",
    "3": "Расторжение",
}
DEFAULT_STAGES = ["0", "1", "2"]
