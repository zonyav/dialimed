
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

_ZLIB_MAGIC = b"\x78\x9c"


def _compress(body: bytes) -> bytes:
    import zlib
    return zlib.compress(body, 6)


def _decompress(body: bytes) -> bytes:
    if not body[:2] == _ZLIB_MAGIC:
        return body
    import zlib
    try:
        return zlib.decompress(body)
    except zlib.error:
        return body


class Cache:

    def __init__(self, db_path: Path, ttl_days: int):
        self.db_path = db_path
        self.ttl = ttl_days * 86400
        self._lock = asyncio.Lock()
        self._init()

    def _init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS http (
                       key TEXT PRIMARY KEY,
                       url TEXT NOT NULL,
                       filename TEXT,
                       body BLOB NOT NULL,
                       ts INTEGER NOT NULL
                   )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_http_ts ON http(ts)")
            for legacy in ("llm", "marks"):
                db.execute(f"DROP TABLE IF EXISTS {legacy}")
            db.commit()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    async def get(self, url: str) -> Optional[tuple[bytes, str]]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, url)

    def _get_sync(self, url: str) -> Optional[tuple[bytes, str]]:
        cutoff = int(time.time()) - self.ttl
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT body, filename FROM http WHERE key=? AND ts>=?",
                (self._key(url), cutoff),
            ).fetchone()
        if not row:
            return None
        return _decompress(row[0]), (row[1] or "")

    async def put(self, url: str, body: bytes, filename: str = "") -> None:
        async with self._lock:
            await asyncio.to_thread(self._put_sync, url, body, filename)

    def _put_sync(self, url: str, body: bytes, filename: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT OR REPLACE INTO http(key, url, filename, body, ts) VALUES (?,?,?,?,?)",
                (self._key(url), url, filename, _compress(body), int(time.time())),
            )
            db.commit()

    def clear(self) -> int:
        with closing(sqlite3.connect(self.db_path)) as db:
            n = db.execute("SELECT COUNT(*) FROM http").fetchone()[0]
            db.execute("DELETE FROM http")
            db.commit()
        return n

    def compact(self) -> tuple[int, int, int]:

        before = self.db_path.stat().st_size if self.db_path.exists() else 0
        packed = 0
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute("SELECT key, body FROM http").fetchall()
            for key, body in rows:
                if body[:2] == _ZLIB_MAGIC:
                    continue
                db.execute("UPDATE http SET body=? WHERE key=?", (_compress(body), key))
                packed += 1
            db.commit()
            db.execute("VACUUM")
        after = self.db_path.stat().st_size if self.db_path.exists() else 0
        return packed, before, after


class EisClient:

    def __init__(
        self,
        concurrency: int | None = None,
        use_cache: bool | None = None,
        on_request: callable | None = None,
    ):
        self.concurrency = concurrency or settings.eis_concurrency
        self.use_cache = settings.cache_enabled if use_cache is None else use_cache
        self.cache = Cache(settings.cache_db, settings.cache_ttl_days) if self.use_cache else None
        self._sem = asyncio.Semaphore(self.concurrency)
        self._client: Optional[httpx.AsyncClient] = None
        self.on_request = on_request
        self.stats = {"hits": 0, "misses": 0, "errors": 0, "bytes": 0,
                      "429": 0, "темп/с": round(settings.eis_rate, 2)}
        self._base_interval = 1.0 / max(0.05, settings.eis_rate)
        self._interval = self._base_interval
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._rate_lock = asyncio.Lock()
        self._streak = 0

    async def __aenter__(self) -> "EisClient":
        limits = httpx.Limits(
            max_connections=self.concurrency + 4,
            max_keepalive_connections=self.concurrency,
        )
        self._client = httpx.AsyncClient(
            headers=settings.http_headers,
            timeout=httpx.Timeout(settings.eis_timeout, connect=20.0),
            limits=limits,
            follow_redirects=True,
            verify=False,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str, *, cacheable: bool = True) -> tuple[bytes, str]:
        if self.cache and cacheable:
            hit = await self.cache.get(url)
            if hit is not None:
                self.stats["hits"] += 1
                if self.on_request:
                    self.on_request(url, True)
                return hit

        async with self._sem:
            body, filename = await self._fetch_raw(url)

        self.stats["misses"] += 1
        self.stats["bytes"] += len(body)
        if self.cache and cacheable:
            await self.cache.put(url, body, filename)
        if self.on_request:
            self.on_request(url, False)
        return body, filename

    async def _fetch_raw(self, url: str) -> tuple[bytes, str]:
        assert self._client is not None, "EisClient используется вне async with"
        last: Exception | None = None
        for attempt in range(settings.eis_retries):
            try:
                await self._throttle()
                r = await self._client.get(url)
                if r.status_code == 429:
                    self._on_429()
                    raise httpx.HTTPStatusError(
                        "HTTP 429", request=r.request, response=r)
                if r.status_code in _RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                self._ok()
                return r.content, _filename_of(r)
            except (httpx.HTTPError, httpx.StreamError) as e:
                last = e
                self._streak = 0
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.6)
                log.debug("ЕИС %s попытка %d/%d: %s: %s", url[:90], attempt + 1,
                          settings.eis_retries, type(e).__name__, e)
                if attempt + 1 < settings.eis_retries:
                    await asyncio.sleep(backoff)
        self.stats["errors"] += 1
        kind = type(last).__name__ if last else "?"
        raise RuntimeError(
            f"ЕИС не ответил после {settings.eis_retries} попыток ({kind}: {last}): {url}"
        ) from last

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            if now < self._blocked_until:
                await asyncio.sleep(self._blocked_until - now)
                now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_slot = max(now, self._next_slot) + self._interval * (
                0.85 + random.random() * 0.3)

    def _on_429(self) -> None:
        self._blocked_until = max(self._blocked_until,
                                  time.monotonic() + settings.eis_cooldown)
        self._interval = min(self._interval * 1.5, 5.0)
        self.stats["429"] += 1
        self.stats["темп/с"] = round(1 / self._interval, 2)
        log.warning("ЕИС ограничил темп, пауза %.0f с, снижаю до %.2f зап/с",
                    settings.eis_cooldown, 1 / self._interval)

    def _ok(self) -> None:
        self._streak += 1
        if self._streak >= 60 and self._interval > self._base_interval:
            self._interval = max(self._base_interval, self._interval * 0.8)
            self._streak = 0
            self.stats["темп/с"] = round(1 / self._interval, 2)

    async def fetch_text(self, url: str, *, cacheable: bool = True) -> str:
        body, _ = await self.fetch(url, cacheable=cacheable)
        return body.decode("utf-8", errors="replace")


def _filename_of(r: httpx.Response) -> str:
    cd = r.headers.get("content-disposition", "")
    if not cd:
        return ""
    import re
    from urllib.parse import unquote

    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1)).strip('"')
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    if not m:
        return ""
    raw = m.group(1).strip().strip('"')
    if raw.isascii():
        return raw
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.encode("latin-1").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return raw
