

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

AREAS: dict[str, dict] = {
    "http": {
        "title": "Страницы и файлы из ЕИС",
        "what": "Карточки контрактов, электронные контракты, печатные формы, "
                "страницы извещений. Повторный прогон по тем же кодам без них "
                "пойдёт заново по сети — это десятки минут.",
        "why": "Стоит стереть, если контракт в ЕИС изменили, а программа "
               "показывает прежнее.",
    },
    "rzn": {
        "title": "Ответы реестра Росздравнадзора",
        "what": "Записи о регистрационных удостоверениях. Стираются быстро "
                "и восстанавливаются сами при следующем прогоне.",
        "why": "Стоит стереть, если изделие зарегистрировали недавно и реестр "
               "тогда ответил пустотой.",
    },
}


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def status() -> dict:
    path = Path(settings.cache_db)
    out = {"file": str(path), "size_mb": 0.0, "areas": []}
    if not path.exists():
        return out
    out["size_mb"] = round(path.stat().st_size / 1048576, 1)
    with closing(sqlite3.connect(path)) as db:
        have = _tables(db)
        for name, meta in AREAS.items():
            n = db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] \
                if name in have else 0
            out["areas"].append({"name": name, "rows": n, **meta})
    return out


def clear(areas: list[str]) -> dict[str, int]:
    names = [a for a in areas if a in AREAS]
    done: dict[str, int] = {}
    if not names:
        return done
    with closing(sqlite3.connect(settings.cache_db)) as db:
        have = _tables(db)
        for name in names:
            if name not in have:
                done[name] = 0
                continue
            done[name] = db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            db.execute(f"DELETE FROM {name}")
        db.commit()
    return done


def clear_all() -> dict[str, int]:

    return clear(list(AREAS))


def size_mb() -> float:
    path = Path(settings.cache_db)
    return round(path.stat().st_size / 1048576, 1) if path.exists() else 0.0


def autocompact() -> dict:

    # Кэш обслуживается сам: сначала выбрасываем просроченное, а если и после
    # этого он больше отведённого размера — самые давние страницы ЕИС.
    # Ответы реестра почти ничего не весят, а запрашиваются долго: их не режем.
    before = size_mb()
    if before < settings.cache_max_mb:
        return {"done": False, "before_mb": before}

    cutoff = int(time.time()) - settings.cache_ttl_days * 86400
    excess = int((before - settings.cache_max_mb * 0.8) * 1048576)
    removed = 0
    freed = 0
    with closing(sqlite3.connect(settings.cache_db)) as db:
        have = _tables(db)
        if "http" in have:
            freed += db.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM http "
                                "WHERE ts < ?", (cutoff,)).fetchone()[0]
        for name in ("http", "rzn"):
            if name in have:
                removed += db.execute(f"DELETE FROM {name} WHERE ts < ?",
                                      (cutoff,)).rowcount
        db.commit()

        if "http" in have and freed < excess:
            drop: list[str] = []
            for key, size in db.execute("SELECT key, LENGTH(body) FROM http ORDER BY ts"):
                if freed >= excess:
                    break
                drop.append(key)
                freed += size or 0
            for i in range(0, len(drop), 400):
                chunk = drop[i:i + 400]
                db.execute(
                    f"DELETE FROM http WHERE key IN ({','.join('?' * len(chunk))})",
                    chunk)
            removed += len(drop)
            db.commit()

    if not removed:
        return {"done": False, "before_mb": before}

    packed = compact()
    log.info("кэш обслужен: удалено записей %d, было %.0f МБ, стало %.0f МБ",
             removed, before, packed["after_mb"])
    return {"done": True, "removed": removed, "before_mb": before,
            "after_mb": packed["after_mb"], "freed_mb": packed["freed_mb"]}


def compact() -> dict:

    from .eis.client import Cache

    packed, before, after = Cache(settings.cache_db, settings.cache_ttl_days).compact()
    return {"packed": packed,
            "before_mb": round(before / 1048576, 1),
            "after_mb": round(after / 1048576, 1),
            "freed_mb": round(max(0, before - after) / 1048576, 1)}
