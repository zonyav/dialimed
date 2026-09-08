

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Optional

import httpx

from ..config import settings

log = logging.getLogger(__name__)

BASE = "https://elk.roszdravnadzor.gov.ru"
FILTER_EP = "/public-gateway/registered-med-product/api/v1/med-product/filter-public"

DEAD_AFTER = 6

RU_NAME_THRESHOLD = 0.62

# Поиск в реестре идёт по вхождению, поэтому по короткому номеру приходят и
# чужие, более длинные. Со страницей в 5 записей ответ то и дело оказывался
# неполным, и точное совпадение выбрасывалось вместе с ним.
NUMBER_PAGE = 25

# Агентный поиск: сколько записей показывать модели и с какого размера выдачи
# запрос считается слишком общим. Реестр — 81 тысяча записей, и слово вроде
# «аппарат» приводит их тысячами.
SEARCH_PAGE = 12
TOO_BROAD = 400

EMPTY_TTL_DAYS = max(1, settings.cache_ttl_days // 4)


@dataclass(slots=True)
class RznRecord:
    rzn_id: str = ""
    producer: str = ""
    producer_eng: str = ""
    producer_address: str = ""
    ru_number: str = ""
    ru_name: str = ""
    ru_date: str = ""
    status: str = ""
    match: str = ""
    score: float = 0.0
    erul: str = ""
    declarant: str = ""
    declarant_inn: str = ""
    models_description: str = ""
    variants: list[str] = field(default_factory=list)
    name_mismatch: bool = False


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'()\[\]®™]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_OPF_RE = re.compile(r"\b(ооо|оао|зао|пао|ао|нао|ип|фгуп|гуп|муп|ано|нпф|нпо|нпп)\b", re.I)

_BARE_OPF_RE = re.compile(
    r"^(?:ип|ооо|оао|зао|пао|ао|нао|фгуп|гуп|муп|ано)\.?$", re.I)


def _mend_bare_opf(decl: dict, prod: dict) -> str:

    name = (decl.get("name") or "").strip()
    if not _BARE_OPF_RE.match(name):
        return name
    prod_name = (prod.get("name") or "").strip()
    if not prod_name or not prod_name.lower().startswith(name.rstrip(".").lower()):
        return name
    for field in ("actualAddress", "legalAddress"):
        a, b = (decl.get(field) or "").strip(), (prod.get(field) or "").strip()
        if a and a == b:
            return prod_name
    return name


_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
})


def latinize(s: str) -> str:
    return (s or "").translate(_HOMOGLYPHS)


def producer_key(name: str) -> str:

    t = _OPF_RE.sub(" ", _norm(name))
    return re.sub(r"[^a-zа-я0-9]+", "", t)


_NUM_CLEAN = re.compile(r"[^0-9a-zа-я/-]+")


def _num_key(s: str) -> str:

    return _NUM_CLEAN.sub("", latinize(_norm(s)))


def keep_same_number(items: list[dict], wanted: str,
                     fields: tuple[str, ...] = ("noRu", "noErul")) -> list[dict]:

    want = _num_key(wanted)
    if not want:
        return []
    return [it for it in items
            if any(_num_key(it.get(f) or "") == want for f in fields)]


def _key_tokens(s: str) -> set[str]:
    stop = {"с", "и", "для", "по", "в", "на", "из", "принадлежностями", "принадлежности",
            "вариант", "варианты", "исполнения", "исполнение", "модели", "модель",
            "медицинский", "медицинская", "медицинское", "ту", "тип", "виды"}
    return {t for t in re.findall(r"[a-zа-я0-9\-]{3,}", _norm(s)) if t not in stop}


_LATIN = re.compile(r"[A-Za-z]{3,}")
_WITH_DIGIT = re.compile(r"\b[\w\-]*\d[\w\-]*\b")
_CAPS_CYR = re.compile(r"\b[А-ЯЁ]{2,}(?:[-–][А-ЯЁа-яё]+)?\b")
_QUOTED = re.compile(r"[«\"']([^«»\"']{2,40})[»\"']")
_HYPHEN_NAME = re.compile(r"\b[А-ЯЁA-Z][а-яёA-Za-z]*[-–][А-ЯЁA-Z0-9][\w\-]*\b")

_TOKEN_STOP = {"ту", "ру", "гост", "исо", "iso", "тип", "мм", "см", "шт", "кг",
               "мл", "гц", "вт", "ip", "n", "no", "рзн", "фсз", "фср"}


def _tokens(text: str, patterns) -> list[str]:
    s = " ".join((text or "").split())
    from .nameparse import RU_NUMBER_RE
    s = RU_NUMBER_RE.sub(" ", s)
    s = _TU.sub("", s)
    out: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in pat.finditer(s):
            t = (m.group(1) if pat is _QUOTED else m.group(0)).strip("-– ")
            low = t.lower()
            if len(t) < 2 or low in _TOKEN_STOP or low in seen:
                continue
            seen.add(low)
            out.append(t)
    return out


def strong_tokens(text: str) -> list[str]:
    return _tokens(text, (_QUOTED, _LATIN, _CAPS_CYR, _WITH_DIGIT))


def distinctive_tokens(text: str) -> list[str]:

    return _tokens(text, (_QUOTED, _LATIN, _CAPS_CYR, _WITH_DIGIT, _HYPHEN_NAME))


def similarity(a: str, b: str) -> float:

    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _key_tokens(a), _key_tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    contain = 0.0
    if na in nb or nb in na:
        shorter = a if len(na) <= len(nb) else b
        if strong_tokens(shorter):
            contain = 0.85
    return max(seq, jac, contain)


_PAGE_SIZES = (5, 10, 25)


@dataclass(slots=True)
class _Answer:

    items: list[dict] = field(default_factory=list)
    complete: bool = True
    answered: bool = True
    total: Optional[int] = None


@dataclass(slots=True)
class NameSearch:
    """Ответ реестра на поиск по наименованию: записи, сколько их всего,
    и две причины пустоты — «слишком общий запрос» и «реестр не ответил»."""

    items: list[dict] = field(default_factory=list)
    total: Optional[int] = None
    too_broad: bool = False
    answered: bool = True


def _complete(items: list[dict], size: Optional[int], total: Optional[int]) -> bool:

    if total is not None:
        return total <= len(items)
    if size is not None:
        return len(items) < size
    return len(items) not in _PAGE_SIZES


class RznCache:
    """Ответы реестра на диске. Соединение одно на прогон — см. eis.client.Cache."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._db: Optional[sqlite3.Connection] = None
        db = self._conn()
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS rzn (
                          key TEXT PRIMARY KEY, payload TEXT, ts INTEGER)""")
        db.commit()

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @staticmethod
    def _key(kind: str, value: str) -> str:
        return hashlib.sha1(f"{kind}\x00{_norm(value)}".encode("utf-8")).hexdigest()

    async def get(self, kind: str, value: str, size: int) -> Optional[_Answer]:
        async with self._lock:
            return await asyncio.to_thread(self._get, kind, value, size)

    def _get(self, kind: str, value: str, size: int) -> Optional[_Answer]:
        row = self._conn().execute("SELECT payload, ts FROM rzn WHERE key=?",
                                   (self._key(kind, value),)).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            items, saved_size, total = data, None, None
        else:
            items = data.get("c") or []
            saved_size = data.get("s")
            total = data.get("t")
        if not items and (row[1] or 0) + EMPTY_TTL_DAYS * 86400 < time.time():
            return None
        if saved_size is not None and saved_size < size:
            return None
        return _Answer(items=items, complete=_complete(items, saved_size, total),
                       total=total)

    async def put(self, kind: str, value: str, items: list, size: int,
                  total: Optional[int]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._put, kind, value, items, size, total)

    def _put(self, kind: str, value: str, items: list, size: int,
             total: Optional[int]) -> None:
        payload = {"c": items, "s": size, "t": total}
        db = self._conn()
        db.execute("INSERT OR REPLACE INTO rzn(key, payload, ts) VALUES (?,?,?)",
                   (self._key(kind, value), json.dumps(payload, ensure_ascii=False),
                    int(time.time())))
        db.commit()


class RznEnricher:

    def __init__(self, *, enabled: bool | None = None, concurrency: int | None = None):
        self.enabled = settings.rzn_enabled if enabled is None else enabled
        self.concurrency = concurrency or settings.rzn_concurrency
        self.cache = RznCache(settings.cache_db) if settings.cache_enabled else None
        self._sem = asyncio.Semaphore(self.concurrency)
        self._client: Optional[httpx.AsyncClient] = None
        self.stats = {"by_ru": 0, "by_erul": 0, "by_tu": 0, "cache": 0,
                      "miss": 0, "errors": 0, "low_score": 0, "too_generic": 0,
                      "ambiguous": 0, "name_mismatch": 0, "skipped": 0,
                      "wrong_number": 0,
                      "truncated": 0,
                      "lost_positions": 0, "crashed": 0}
        self._fails = 0
        self._dead = False
        self._unanswered: set[tuple[str, str]] = set()

    async def __aenter__(self) -> "RznEnricher":
        if self.enabled:
            self._client = httpx.AsyncClient(
                base_url=BASE,
                headers={
                    "User-Agent": settings.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": BASE,
                    "Referer": f"{BASE}/widget/mi",
                },
                timeout=httpx.Timeout(settings.rzn_timeout, connect=15.0),
                verify=False,
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self.cache:
            self.cache.close()

    async def available(self) -> bool:

        if not self.enabled or self._client is None:
            return False
        for attempt in range(2):
            try:
                r = await self._client.post(FILTER_EP, json={"noRu": "ФСР 2011/09988"},
                                            params={"page": 0, "size": 1}, timeout=25.0)
                r.raise_for_status()
                if isinstance(r.json().get("content"), list):
                    return True
            except Exception as e:
                log.warning("реестр РЗН, проба %d: %s: %s", attempt + 1, type(e).__name__, e)
                if attempt == 0:
                    await asyncio.sleep(2.0)
        return False

    async def _query(self, field: str, value: str, size: int = 20) -> _Answer:
        if not value:
            return _Answer()
        if self.cache:
            hit = await self.cache.get(field, value, size)
            if hit is not None:
                self.stats["cache"] += 1
                return hit
        assert self._client is not None
        async with self._sem:
            if self._dead:
                self.stats["skipped"] += 1
                self.stats["errors"] += 1
                self._unanswered.add((field, value))
                return _Answer(answered=False)
            for attempt in range(3):
                try:
                    r = await self._client.post(FILTER_EP, json={field: value},
                                                params={"page": 0, "size": size})
                    r.raise_for_status()
                    body = r.json()
                    content = body.get("content") or []
                    total = body.get("totalElements")
                    total = total if isinstance(total, int) else None
                    if self.cache:
                        await self.cache.put(field, value, content, size, total)
                    self._fails = 0
                    return _Answer(items=content, complete=_complete(content, size, total))
                except Exception as e:
                    log.debug("РЗН %s=%r попытка %d: %s: %s", field, value[:40],
                              attempt + 1, type(e).__name__, e)
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
        self.stats["errors"] += 1
        self._unanswered.add((field, value))
        self._fails += 1
        if self._fails >= DEAD_AFTER:
            self._dead = True
            log.warning("реестр РЗН не отвечает подряд %d раз — прекращаю запросы, "
                        "остаток прогона идёт по кэшу и справочнику", self._fails)
        return _Answer(answered=False)

    async def raw_filter(self, body: dict, *, page: int = 0, size: int = 200,
                         cache_key: str = "") -> Optional[dict]:

        if not self.enabled or self._client is None:
            return None
        key = cache_key or json.dumps(body, sort_keys=True, ensure_ascii=False)
        if self.cache:
            hit = await self.cache.get("raw", key, size)
            if hit is not None:
                self.stats["cache"] += 1
                return {"content": hit.items, "totalElements": hit.total}
        async with self._sem:
            if self._dead:
                self.stats["skipped"] += 1
                self.stats["errors"] += 1
                return None
            for attempt in range(3):
                try:
                    r = await self._client.post(FILTER_EP, json=body,
                                                params={"page": page, "size": size})
                    r.raise_for_status()
                    data = r.json()
                    content = data.get("content") or []
                    total = data.get("totalElements")
                    total = total if isinstance(total, int) else None
                    if self.cache:
                        await self.cache.put("raw", key, content, size, total)
                    self._fails = 0
                    return {"content": content, "totalElements": total}
                except Exception as e:
                    log.debug("РЗН фильтр %s стр. %d попытка %d: %s: %s",
                              key[:50], page, attempt + 1, type(e).__name__, e)
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
        self.stats["errors"] += 1
        self._fails += 1
        if self._fails >= DEAD_AFTER:
            self._dead = True
        return None

    async def search_by_name(self, phrase: str,
                             size: int = SEARCH_PAGE) -> "NameSearch":
        """Поиск по наименованию регистрации — вхождение подстроки. Нужен
        агентному поиску (`enrich/aimatch.py`), где запросы придумывает модель.

        Слишком широкая выдача — это отказ, а не ответ: одно общее слово
        приводит половину реестра, и показывать её модели бессмысленно."""

        phrase = (phrase or "").strip()
        if not phrase or not self.enabled or self._client is None:
            return NameSearch(answered=False)
        data = await self.raw_filter({"medProductName": phrase}, size=size,
                                     cache_key=f"agent:{_norm(phrase)}")
        if data is None:
            return NameSearch(answered=False)
        total = data.get("totalElements")
        total = total if isinstance(total, int) else None
        if total is not None and total > TOO_BROAD:
            return NameSearch(total=total, too_broad=True)
        return NameSearch(items=data.get("content") or [], total=total)

    async def confirm_number(self, ru_number: str) -> Optional[RznRecord]:
        """Проверка номера, названного ИИ: запись существует, номер совпадает
        посимвольно, и производитель под ним один. Счётчики поиска по контракту
        сюда не идут — это другой источник, и путать их в сводке нельзя."""

        ru_number = (ru_number or "").strip()
        if not ru_number or not self.enabled or self._client is None:
            return None
        ans = await self._query("noRu", ru_number, size=NUMBER_PAGE)
        if not ans.answered or not ans.complete:
            return None
        items = keep_same_number(ans.items, ru_number)
        if not items:
            return None
        producers = {producer_key((it.get("producer") or {}).get("name") or "")
                     for it in items}
        producers.discard("")
        if len(producers) > 1:
            return None
        return self._record(items[0], "ai", 1.0)

    @staticmethod
    def _record(item: dict, match: str, score: float,
                type_hints: list[str] | None = None) -> RznRecord:
        from .nameparse import variants_from_registry

        prod = item.get("producer") or {}
        decl = item.get("declarant") or {}
        decl_name = _mend_bare_opf(decl, prod)
        status = item.get("status") or {}
        desc = (item.get("modelsDescription") or "").strip()
        sites = item.get("productionSites") or []
        eng = next((s.get("engName") or "" for s in sites if s.get("engName")), "").strip()
        return RznRecord(
            rzn_id=str(item.get("id") or "").strip(),
            producer=(prod.get("name") or "").strip(),
            producer_eng=eng,
            producer_address=(prod.get("actualAddress") or prod.get("legalAddress") or "").strip(),
            ru_number=(item.get("noRu") or "").strip(),
            ru_name=(item.get("name") or "").strip(),
            ru_date=(item.get("dateRu") or "").strip(),
            status=(status.get("name") or "").strip(),
            erul=(item.get("noErul") or "").strip(),
            declarant=decl_name,
            declarant_inn=(decl.get("inn") or "").strip(),
            models_description=desc,
            variants=variants_from_registry(desc, type_hints),
            match=match,
            score=round(score, 3),
        )

    def _same_number(self, items: list[dict], wanted: str) -> list[dict]:
        keep = keep_same_number(items, wanted)
        if items and not keep:
            self.stats["wrong_number"] += 1
        return keep

    def _one_producer(self, items: list[dict], ans: _Answer) -> bool:

        if not ans.complete:
            self.stats["truncated"] += 1
            return False
        producers = {producer_key((it.get("producer") or {}).get("name") or "")
                     for it in items}
        producers.discard("")
        if len(producers) > 1:
            self.stats["ambiguous"] += 1
            return False
        return True

    async def lookup_by_tu(self, tu_number: str, ru_name: str = "",
                           type_hints: list[str] | None = None) -> Optional[RznRecord]:

        if not self.enabled or self._client is None or not tu_number:
            return None
        ans = await self._query("medProductName", tu_number, size=10)
        items = ans.items
        if not items:
            return None
        if len(items) == 1 and ans.complete:
            self.stats["by_tu"] += 1
            return self._record(items[0], "tu", 1.0, type_hints)
        if not ans.complete:
            self.stats["truncated"] += 1
            return None
        scored = [(similarity(ru_name, it.get("name") or "") if ru_name else 0.0, it)
                  for it in items]
        score, best = max(scored, key=lambda x: x[0])
        producers = {producer_key((it.get("producer") or {}).get("name") or "")
                     for _s, it in scored}
        producers.discard("")
        if len(producers) > 1:
            self.stats["ambiguous"] += 1
            return None
        self.stats["by_tu"] += 1
        return self._record(best, "tu", max(score, 0.9), type_hints)

    async def lookup(self, *, ru_number: str = "", ru_name: str = "",
                     tu_number: str = "", erul: str = "",
                     type_hints: list[str] | None = None) -> Optional[RznRecord]:
        if not self.enabled or self._client is None:
            return None

        if erul:
            ans = await self._query("noErul", erul, size=NUMBER_PAGE)
            items = self._same_number(ans.items, erul)
            if items and self._one_producer(items, ans):
                self.stats["by_erul"] += 1
                return self._record(items[0], "noErul", 1.0, type_hints)

        if ru_number:
            ans = await self._query("noRu", ru_number, size=NUMBER_PAGE)
            items = self._same_number(ans.items, ru_number)
            if items and not self._one_producer(items, ans):
                items = []
            if items:
                best, score = items[0], 1.0
                if ru_name and len(items) > 1:
                    scored = [(similarity(ru_name, it.get("name") or ""), it) for it in items]
                    score, best = max(scored, key=lambda x: x[0])
                best_name = best.get("name") or ""
                kind_ok = not type_hints or _same_device_kind(best_name, type_hints)
                name_ok = not ru_name or similarity(ru_name, best_name) >= RU_NAME_THRESHOLD
                rec = self._record(best, "noRu", score, type_hints)
                # Номер выписан в контракте дословно и совпал посимвольно,
                # производитель под ним один — этого довольно. Реестр называет
                # изделие своими словами («Система эндоскопическая» там, где
                # КТРУ пишет «Видеогастроскоп гибкий»), и раньше такая запись
                # выбрасывалась целиком. Теперь она берётся, но помечается.
                rec.name_mismatch = not kind_ok and not name_ok
                if rec.name_mismatch:
                    self.stats["name_mismatch"] += 1
                self.stats["by_ru"] += 1
                return rec

        if tu_number:
            rec = await self.lookup_by_tu(tu_number, ru_name, type_hints)
            if rec is not None:
                return rec

        if {("noErul", erul), ("noRu", ru_number),
                ("medProductName", tu_number)} & self._unanswered:
            return None
        self.stats["miss"] += 1
        return None

    async def lookup_many(
        self,
        queries: Iterable[tuple],
        progress: callable | None = None,
    ) -> list[Optional[RznRecord]]:

        norm = [(q[0] or "", q[1] or "", tuple(q[2] or ()),
                 (q[3] if len(q) > 3 else "") or "",
                 (q[4] if len(q) > 4 else "") or "") for q in queries]
        uniq = list(dict.fromkeys(norm))
        done = 0
        lock = asyncio.Lock()

        async def one(q):
            nonlocal done
            try:
                res = await self.lookup(ru_number=q[0], ru_name=q[1],
                                        tu_number=q[3], erul=q[4],
                                        type_hints=list(q[2]))
            except Exception as e:
                log.debug("РЗН %s: %s", q[:2], e)
                self.stats["crashed"] += 1
                res = None
            if progress:
                async with lock:
                    done += 1
                    progress(done, len(uniq))
            return q, res

        got = await asyncio.gather(*(one(q) for q in uniq))
        by_q = dict(got)
        out = [by_q.get(q) for q in norm]
        if self._unanswered:
            self.stats["lost_positions"] = sum(
                1 for q, rec in zip(norm, out)
                if rec is None and ({("noErul", q[4]), ("noRu", q[0]),
                                     ("medProductName", q[3])} & self._unanswered))
        return out


def _same_device_kind(registry_name: str, hints: list[str]) -> bool:

    t = _norm(registry_name)
    if not t:
        return False
    for h in hints:
        words = [w for w in _norm(h).split() if len(w) > 4]
        if not words:
            continue
        stems = [w[:-2] if len(w) > 6 else w for w in words]
        if len(stems) <= 3:
            if all(s in t for s in stems):
                return True
            continue
        if stems[0] in t or sum(1 for s in stems[1:] if s in t) >= 2:
            return True
    return False


_TU = re.compile(r"\s*(?:по\s+)?\bТУ\s*[-–]?\s*№?\s*[\w\-.]+.*$", re.I)
