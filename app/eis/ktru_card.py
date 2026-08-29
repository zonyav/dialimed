

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlencode

from selectolax.parser import HTMLParser

from ..config import settings, DEFAULT_STAGES
from .client import EisClient
from .search import build_search_url, total_found

log = logging.getLogger(__name__)

CATALOG_URL = f"{settings.eis_base}/epz/ktru/search/results.html"

_UNIT = re.compile(r"Единица\s+измерения\s*:\s*(.+)", re.I)
_EXCLUDED = re.compile(r"Исключено\s*\n?\s*(\d{2}\.\d{2}\.\d{4})", re.I)
_INCLUDED = re.compile(r"Включено\s+в\s+каталог\s*\n?\s*(\d{2}\.\d{2}\.\d{4})", re.I)
_POSITION = re.compile(r"Позиция\s+КТРУ", re.I)


@dataclass(slots=True)
class KtruCard:
    code: str = ""
    name: str = ""
    unit: str = ""
    excluded: str = ""
    included: str = ""
    is_position: bool = True
    found: bool = False
    exact: bool = False
    match: float = 0.0


@dataclass(slots=True)
class KtruCheck:
    code: str = ""
    ok: bool = False
    name: str = ""
    unit: str = ""
    excluded: str = ""
    contracts: int = 0
    contracts_all: int = 0
    note: str = ""
    from_kind: str = ""
    exact: bool = False
    kind_ok: Optional[bool] = None
    kind_found: str = ""
    kind_note: str = ""


def parse_ktru_cards(html: str) -> list[KtruCard]:
    tree = HTMLParser(html or "")
    blocks = tree.css("div.search-registry-entry-block") or tree.css("div.registry-entry")
    return [c for c in (_card_from_block(b) for b in blocks) if c.found]


def parse_ktru_card(html: str, code: str) -> KtruCard:
    for card in parse_ktru_cards(html):
        if not card.code or card.code == code:
            card.code = card.code or code
            return card
    return KtruCard(code=code, found=False)


def _card_from_block(b) -> KtruCard:
    num = b.css_first(".registry-entry__header-mid__number")
    found_code = num.text(strip=True) if num else ""
    text = b.text(separator="\n", strip=True)
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    name = ""
    for sel in (".registry-entry__header-mid__h4", ".registry-entry__header-mid__title"):
        node = b.css_first(sel)
        if node and node.text(strip=True):
            raw = node.text(deep=True, separator="", strip=False)
            name = re.sub(r"\s+", " ", raw.replace("\xa0", " ")).strip()
            break
    if not name and len(lines) > 1:
        name = lines[1]
    if _UNIT.match(name) and len(lines) > 2:
        name = lines[2]
    unit = ""
    for line in lines:
        m = _UNIT.match(line)
        if m:
            unit = m.group(1).strip()
            break
    ex = _EXCLUDED.search(text)
    inc = _INCLUDED.search(text)
    return KtruCard(code=found_code, name=name, unit=unit,
                    excluded=ex.group(1) if ex else "",
                    included=inc.group(1) if inc else "",
                    is_position=bool(_POSITION.search(text)), found=True)


async def fetch_ktru_card(client: EisClient, code: str) -> KtruCard:
    params = {"searchString": code, "morphology": "on", "pageNumber": "1",
              "sortDirection": "false", "recordsPerPage": "_10",
              "showLotsInfoHidden": "false", "sortBy": "UPDATE_DATE"}
    try:
        html = await client.fetch_text(f"{CATALOG_URL}?{urlencode(params)}")
    except Exception as e:
        log.warning("каталог КТРУ %s: %s: %s", code, type(e).__name__, e)
        return KtruCard(code=code, found=False)
    return parse_ktru_card(html, code)


def _same_name(a: str, b: str) -> bool:

    norm = lambda s: re.sub(r"[^a-zа-яё0-9]+", "", (s or "").lower().replace("ё", "е"))
    x, y = norm(a), norm(b)
    return bool(x) and bool(y) and x == y


_NAME_STOP = {"для", "или", "и", "с", "со", "из", "в", "на", "от", "по", "до",
              "к", "при", "их", "не", "общего", "назначения"}

MATCH_MIN = 0.5


def _name_words(s: str) -> list[str]:
    s = re.sub(r"[^a-zа-я0-9]+", " ", (s or "").lower().replace("ё", "е"))
    return [w for w in s.split() if len(w) > 2 and w not in _NAME_STOP]


def _same_word(a: str, b: str) -> bool:

    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 5:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(5, n - 3)


def name_match(kind_name: str, card_name: str) -> float:

    kw, cw = _name_words(kind_name), _name_words(card_name)
    if not kw or not cw:
        return 0.0
    hits = [w for w in kw if any(_same_word(w, x) for x in cw)]
    if not hits or hits[0] != kw[0]:
        return 0.0
    back = sum(1 for w in cw if any(_same_word(w, x) for x in kw))
    return max(len(hits) / len(kw), back / len(cw))


def _query_variants(name: str) -> list[str]:

    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = q.strip(" ,;./")
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    add(name)
    head = name
    while True:
        m = re.search(r"[,/][^,/]*$", head)
        if not m:
            break
        head = head[:m.start()].strip()
        add(head)
    words = _name_words(head)
    if len(words) > 2:
        add(" ".join(words[:2]))
    return out


async def _search_catalog(client: EisClient, query: str) -> list[KtruCard]:
    params = {"searchString": query, "morphology": "on", "pageNumber": "1",
              "sortDirection": "false", "recordsPerPage": "_50",
              "showLotsInfoHidden": "false", "sortBy": "UPDATE_DATE",
              "active": "on", "terminated": "on", "activeESCKLP": "on"}
    try:
        html = await client.fetch_text(f"{CATALOG_URL}?{urlencode(params)}")
    except Exception as e:
        log.warning("каталог КТРУ по наименованию %r: %s: %s", query[:60],
                    type(e).__name__, e)
        return []
    return [c for c in parse_ktru_cards(html) if c.is_position and c.code]


async def find_by_name(client: EisClient, name: str, *,
                       limit: int = 40) -> list[KtruCard]:

    keep: dict[str, KtruCard] = {}
    for query in _query_variants(name):
        for c in await _search_catalog(client, query):
            if c.code in keep:
                continue
            c.exact = _same_name(c.name, name)
            c.match = 1.0 if c.exact else name_match(name, c.name)
            if c.match >= MATCH_MIN:
                keep[c.code] = c
    out = sorted(keep.values(), key=lambda c: (not c.exact, -c.match, c.code))
    return out[:limit]


async def _count_contracts(client: EisClient, code: str, *, date_from: str,
                           date_to: str, stages: Iterable[str]) -> int:
    url = build_search_url(code, date_from=date_from, date_to=date_to,
                           stages=stages, page=1, page_size=settings.eis_page_size)
    try:
        return total_found(await client.fetch_text(url))
    except Exception as e:
        log.warning("счёт контрактов по %s: %s: %s", code, type(e).__name__, e)
        return -1


async def check_one(client: EisClient, code: str, *, date_from: str = "01.01.2025",
                    date_to: str = "", stages: Iterable[str] = DEFAULT_STAGES,
                    card: Optional[KtruCard] = None) -> KtruCheck:

    if card is not None:
        n = await _count_contracts(client, code, date_from=date_from,
                                   date_to=date_to, stages=stages)
    else:
        card, n = await asyncio.gather(
            fetch_ktru_card(client, code),
            _count_contracts(client, code, date_from=date_from, date_to=date_to,
                             stages=stages),
        )
    res = KtruCheck(code=code, ok=card.found, name=card.name, unit=card.unit,
                    excluded=card.excluded, contracts=max(0, n))

    if not card.found:
        res.note = ("такого кода нет в каталоге ЕИС — проверьте, не потерялась ли цифра"
                    if n <= 0 else
                    "в каталоге ЕИС кода нет, но контракты по нему есть — "
                    "позиция могла быть удалена из каталога")
        res.ok = n > 0
        return res

    if n < 0:
        res.note = "ЕИС не ответил на запрос числа контрактов — попробуйте ещё раз"
        return res

    if n == 0:
        res.contracts_all = max(0, await _count_contracts(
            client, code, date_from="", date_to="", stages=stages))
        res.note = (f"за выбранный период контрактов нет, а всего по коду "
                    f"{res.contracts_all} — измените дату «с»"
                    if res.contracts_all else
                    "контрактов по этому коду нет вовсе")
    return res


async def check_codes(client: EisClient, codes: Iterable[str], *,
                      date_from: str = "01.01.2025", date_to: str = "",
                      stages: Iterable[str] = DEFAULT_STAGES,
                      cards: Optional[dict] = None,
                      progress: Optional[callable] = None) -> list[KtruCheck]:
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return []
    stages = list(stages)
    cards = cards or {}
    done = 0
    lock = asyncio.Lock()

    async def one(code: str) -> KtruCheck:
        nonlocal done
        res = await check_one(client, code, date_from=date_from,
                              date_to=date_to, stages=stages,
                              card=cards.get(code))
        if progress:
            async with lock:
                done += 1
                progress(done, len(codes))
        return res

    return list(await asyncio.gather(*(one(c) for c in codes)))


CONFIRM_TRIES = 2


async def confirm_kind(client: EisClient, code: str, nkmi_code: str, *,
                       date_from: str = "01.01.2025", date_to: str = "",
                       stages: Iterable[str] = DEFAULT_STAGES) -> KtruCheck:

    from .documents import fetch_contract_xml
    from .search import search_ktru
    from .xml_parser import parse_contract_xml

    res = KtruCheck(code=code, from_kind=nkmi_code)
    try:
        metas, _total = await search_ktru(client, code, date_from=date_from,
                                          date_to=date_to, stages=list(stages),
                                          limit=CONFIRM_TRIES)
    except Exception as e:
        log.warning("подтверждение вида %s по коду %s: %s: %s",
                    nkmi_code, code, type(e).__name__, e)
        res.kind_note = "проверить вид не вышло: ЕИС не ответил"
        return res

    seen: dict[str, str] = {}
    for meta in metas:
        data, _note = await fetch_contract_xml(client, meta.reestr_number)
        if data is None:
            continue
        try:
            _m, poss = parse_contract_xml(data, meta)
        except Exception as e:
            log.debug("подтверждение вида: контракт %s не разобран: %s",
                      meta.reestr_number, e)
            continue
        for p in poss:
            if p.ktru == code and p.nkmi_code:
                seen.setdefault(p.nkmi_code, p.nkmi_name or "")
        if nkmi_code in seen:
            break

    if nkmi_code in seen:
        res.kind_ok = True
        res.kind_found = nkmi_code
        res.kind_note = "вид подтверждён контрактом"
    elif seen:
        other = sorted(seen.items(), key=lambda kv: kv[0])[0]
        res.kind_ok = False
        res.kind_found = other[0]
        res.kind_note = (f"в контрактах по этому коду стоит вид {other[0]}"
                         + (f": {other[1]}" if other[1] else ""))
    else:
        res.kind_note = "в контрактах код вида не указан — проверить не по чему"
    return res


CODES_FROM_CONTRACTS = 10


async def codes_from_contracts(client: EisClient, nkmi_code: str, *,
                               date_from: str = "01.01.2025", date_to: str = "",
                               stages: Iterable[str] = DEFAULT_STAGES) -> dict[str, str]:
    from .documents import fetch_contract_xml
    from .search import parse_search_page
    from .xml_parser import parse_contract_xml

    params: list[tuple[str, str]] = [
        ("searchString", f'"код НКМИ: {nkmi_code}"'),
        ("morphology", "on"), ("fz44", "on"),
        ("contractStageList", ",".join(stages)),
        ("sortBy", "BY_SIGN_DATE"), ("pageNumber", "1"),
        ("sortDirection", "false"), ("recordsPerPage", "_10"),
        ("showLotsInfoHidden", "false"),
    ]
    if date_from:
        params.append(("contractDateFrom", date_from))
    if date_to:
        params.append(("contractDateTo", date_to))
    try:
        html = await client.fetch_text(
            f"{settings.eis_base}/epz/contract/search/results.html?{urlencode(params)}")
    except Exception as e:
        log.warning("поиск контрактов по коду вида %s: %s: %s",
                    nkmi_code, type(e).__name__, e)
        return {}

    out: dict[str, str] = {}
    for meta in parse_search_page(html)[:CODES_FROM_CONTRACTS]:
        data, _note = await fetch_contract_xml(client, meta.reestr_number)
        if data is None:
            continue
        try:
            _m, poss = parse_contract_xml(data, meta)
        except Exception as e:
            log.debug("поиск кода КТРУ по виду: контракт %s не разобран: %s",
                      meta.reestr_number, e)
            continue
        for p in poss:
            if p.nkmi_code == nkmi_code and p.ktru:
                out.setdefault(p.ktru, p.ktru_name or p.name)
    return out


CONFIRM_MAX = 8


@dataclass(slots=True)
class KindBridge:
    code: str = ""
    name: str = ""
    status: str = ""
    found: bool = False
    note: str = ""
    checks: list[KtruCheck] = field(default_factory=list)

    @property
    def confirmed(self) -> list[str]:

        ok = [c for c in self.checks
              if c.kind_ok or (c.kind_ok is None and c.exact and c.contracts)]
        ok.sort(key=lambda c: -c.contracts)
        return [c.code for c in ok]


async def bridge_kind(client: EisClient, nkmi_code: str, *,
                      date_from: str = "01.01.2025", date_to: str = "",
                      stages: Iterable[str] = DEFAULT_STAGES,
                      confirm: bool = True) -> KindBridge:

    from ..enrich.nkmi import lookup as nkmi_lookup

    stages = list(stages)
    kind = await nkmi_lookup(nkmi_code)
    br = KindBridge(code=nkmi_code, name=kind.name, status=kind.status,
                    found=kind.found, note=kind.note)
    if not kind.found or not kind.name:
        return br

    by_text = await codes_from_contracts(client, nkmi_code, date_from=date_from,
                                         date_to=date_to, stages=stages)

    cards = await find_by_name(client, kind.name)
    by_code = {c.code: c for c in cards}
    candidates = list(dict.fromkeys(list(by_text) + [c.code for c in cards]))
    if not candidates:
        br.note = "ни в контрактах, ни в каталоге ЕИС позиций с таким видом нет"
        return br

    checks = await check_codes(client, candidates, date_from=date_from,
                               date_to=date_to, stages=stages, cards=by_code)
    for ch in checks:
        card = by_code.get(ch.code)
        ch.from_kind = nkmi_code
        ch.exact = bool(card and card.exact)
        if ch.code in by_text:
            ch.kind_ok = True
            ch.kind_found = nkmi_code
            ch.kind_note = "вид указан в контрактах по этому коду"

    # вид берём только из самих контрактов: похожее наименование ничего не значит
    unproven = sorted((c for c in checks if c.kind_ok is None and c.contracts),
                      key=lambda c: (not c.exact, -c.contracts))[:CONFIRM_MAX]
    if confirm and unproven:
        for ch, res in zip(unproven, await asyncio.gather(
                *(confirm_kind(client, c.code, nkmi_code, date_from=date_from,
                               date_to=date_to, stages=stages) for c in unproven))):
            ch.kind_ok, ch.kind_found = res.kind_ok, res.kind_found
            ch.kind_note = res.kind_note

    br.checks = sorted((c for c in checks if c.kind_ok),
                       key=lambda c: (-c.contracts, c.code))

    if not br.checks:
        others = sorted({c.kind_found for c in checks if c.kind_found})
        br.note = ("ни одна позиция каталога не подтвердила этот вид контрактами"
                   + (f"; в контрактах по близким кодам стоят виды "
                      f"{', '.join(others)}" if others else ""))
    return br
