
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Iterable, Optional
from urllib.parse import urlencode

from selectolax.parser import HTMLParser

from ..config import settings, DEFAULT_STAGES
from ..models import ContractMeta
from .client import EisClient

log = logging.getLogger(__name__)

SEARCH_URL = f"{settings.eis_base}/epz/contract/search/results.html"

_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_MONEY_RE = re.compile(r"([\d\s ]+(?:[.,]\d{1,2})?)\s*(?:₽|руб)", re.I)
_KTRU_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{4,8}\b")


def normalize_ktru(raw: str) -> str:
    s = (raw or "").strip().replace(" ", " ")
    m = _KTRU_RE.search(s)
    return m.group(0) if m else ""


def normalize_purchase_number(raw: str) -> str:

    digits = re.sub(r"\D", "", raw or "")
    return digits[:19] if len(digits) >= 19 else ""


def parse_ktru_list(text: str) -> list[str]:

    out: list[str] = []
    seen: set[str] = set()
    for m in _KTRU_RE.finditer(text or ""):
        code = m.group(0)
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def parse_ktru_bad(text: str) -> list[str]:

    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip(" .,;:—–-")
        if not token or not re.search(r"\d", token) or "." not in token:
            continue
        if _KTRU_RE.search(token):
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def build_search_url(
    ktru: str,
    *,
    ktru_name: str = "",
    date_from: str = "01.01.2025",
    date_to: str = "",
    stages: Iterable[str] = DEFAULT_STAGES,
    page: int = 1,
    page_size: int = 50,
) -> str:
    stages = list(stages) or list(DEFAULT_STAGES)
    params: list[tuple[str, str]] = [
        ("morphology", "on"),
        ("search-filter", "Дате размещения"),
        ("fz44", "on"),
    ]
    for s in stages:
        params.append((f"contractStageList_{s}", "on"))
    params.append(("contractStageList", ",".join(stages)))
    if date_from:
        params.append(("contractDateFrom", date_from))
    if date_to:
        params.append(("contractDateTo", date_to))
    params.append(("ktruCodeNameList", f"{ktru}&&&{ktru_name}" if ktru_name else f"{ktru}&&&"))
    params += [
        ("sortBy", "BY_SIGN_DATE"),
        ("pageNumber", str(page)),
        ("sortDirection", "false"),
        ("recordsPerPage", f"_{page_size}"),
        ("showLotsInfoHidden", "false"),
    ]
    return f"{SEARCH_URL}?{urlencode(params)}"


def build_text_search_url(
    query: str,
    *,
    date_from: str = "01.01.2025",
    date_to: str = "",
    stages: Iterable[str] = DEFAULT_STAGES,
    page: int = 1,
    page_size: int = 50,
) -> str:
    """Поиск словами — по тому же реестру контрактов.

    ЕИС ищет строку в номере реестровой записи, ИКЗ, наименовании заказчика,
    номере контракта, предмете контракта и наименовании объекта закупки. Слова
    соединяются «и»: «рускан 70п» — это 30 контрактов против 645 по одному
    «рускан». Морфология оставлена включённой, как в форме сайта: на замерах
    она ничего не портила, а окончания в наименованиях гуляют.

    Порядок параметров повторяет `build_search_url` дословно: URL — это ключ
    кэша, и перестановка обнулила бы весь накопленный кэш.
    """

    stages = list(stages) or list(DEFAULT_STAGES)
    params: list[tuple[str, str]] = [
        ("morphology", "on"),
        ("search-filter", "Дате размещения"),
        ("fz44", "on"),
    ]
    for s in stages:
        params.append((f"contractStageList_{s}", "on"))
    params.append(("contractStageList", ",".join(stages)))
    if date_from:
        params.append(("contractDateFrom", date_from))
    if date_to:
        params.append(("contractDateTo", date_to))
    params.append(("searchString", query))
    params += [
        ("sortBy", "BY_SIGN_DATE"),
        ("pageNumber", str(page)),
        ("sortDirection", "false"),
        ("recordsPerPage", f"_{page_size}"),
        ("showLotsInfoHidden", "false"),
    ]
    return f"{SEARCH_URL}?{urlencode(params)}"


def _to_date(s: str) -> Optional[date]:
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _to_money(s: str) -> Optional[float]:
    m = _MONEY_RE.search(s or "")
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _labelled(text: str, label: str, span: int = 120) -> str:
    i = text.find(label)
    if i < 0:
        return ""
    return text[i + len(label): i + len(label) + span]


def total_found(html: str) -> int:
    tree = HTMLParser(html)
    node = tree.css_first(".search-results__total")
    if node:
        digits = re.sub(r"\D", "", node.text(strip=True))
        if digits:
            return int(digits)
    return 0


def parse_search_page(html: str) -> list[ContractMeta]:
    tree = HTMLParser(html)
    out: list[ContractMeta] = []
    for block in tree.css("div.search-registry-entry-block"):
        raw = block.text(separator="\n", strip=True)
        text = re.sub(r"\n+", "\n", raw)
        flat = text.replace("\n", " ")

        rn = ""
        for a in block.css("a[href]"):
            m = re.search(r"reestrNumber=(\d{15,25})", a.attributes.get("href", ""))
            if m:
                rn = m.group(1)
                break
        if not rn:
            m = re.search(r"№\s*(\d{19})", flat)
            rn = m.group(1) if m else ""
        if not rn:
            continue

        meta = ContractMeta(reestr_number=rn)
        meta.conclusion_date = _to_date(_labelled(flat, "Заключение контракта"))
        meta.execution_end_date = _to_date(_labelled(flat, "Срок исполнения"))
        meta.contract_price = _to_money(_labelled(flat, "Цена контракта"))

        m = re.search(r"Заказчик\s+(.{5,400}?)(?:\s+Контракт\s+№|\s+Объекты закупки|$)", flat)
        if m:
            meta.customer = m.group(1).strip()

        m = re.search(r"Контракт\s+№\s*([\w\-/.]+)", flat)
        if m:
            meta.contract_number = m.group(1)

        # объект закупки виден уже здесь: «Техническое обслуживание…» можно
        # узнать, не скачивая контракт. Показан только первый из них, поэтому
        # это подсказка, а не приговор — по ней ничего не выбрасывают
        m = re.search(r"Объекты закупки\s+(.{3,300}?)\s*"
                      r"(?:Посмотреть все\s*\((\d+)\)|Цена контракта|$)", flat)
        if m:
            meta.first_object = m.group(1).strip(" .,\"'«»")
            meta.objects_total = int(m.group(2) or 1)

        m = re.search(r"\(ИКЗ\)\s*(\d{30,40})", flat) or re.search(r"\b(\d{36})\b", flat)
        if m:
            meta.ikz = m.group(1)

        for line in text.split("\n"):
            s = line.strip()
            if s in {"Исполнение", "Исполнение завершено", "Исполнение прекращено",
                     "Расторжение", "Аннулирован"}:
                meta.stage = s
                break

        meta.source = "eis-search"
        meta.source_ref = meta.url
        out.append(meta)
    return out


async def search_ktru(
    client: EisClient,
    ktru: str,
    *,
    date_from: str = "01.01.2025",
    date_to: str = "",
    stages: Iterable[str] = DEFAULT_STAGES,
    limit: int = 0,
) -> tuple[list[ContractMeta], int]:

    page_size = settings.eis_page_size
    first_url = build_search_url(ktru, date_from=date_from, date_to=date_to,
                                 stages=stages, page=1, page_size=page_size)
    html = await client.fetch_text(first_url)
    total = total_found(html)
    metas = parse_search_page(html)
    if not metas:
        return [], total

    wanted = min(total, limit) if limit else total
    pages = _pages(wanted, page_size, limit)
    if pages <= 1:
        return metas[:wanted] if limit else metas, total

    async def one(p: int) -> list[ContractMeta]:
        url = build_search_url(ktru, date_from=date_from, date_to=date_to,
                               stages=stages, page=p, page_size=page_size)
        try:
            h = await client.fetch_text(url)
            res = parse_search_page(h)
        except Exception as e:
            log.warning("КТРУ %s стр.%d: %s", ktru, p, e)
            res = []
        return res

    rest = await asyncio.gather(*(one(p) for p in range(2, pages + 1)))
    for chunk in rest:
        metas.extend(chunk)

    seen: set[str] = set()
    uniq: list[ContractMeta] = []
    for m in metas:
        if m.reestr_number in seen:
            continue
        seen.add(m.reestr_number)
        uniq.append(m)
    return (uniq[:limit] if limit else uniq), total


async def search_text(
    client: EisClient,
    query: str,
    *,
    date_from: str = "01.01.2025",
    date_to: str = "",
    stages: Iterable[str] = DEFAULT_STAGES,
    limit: int = 0,
) -> tuple[list[ContractMeta], int]:
    """Контракты, где встречается строка. Постранично, как и поиск по КТРУ."""

    page_size = settings.eis_page_size
    first = build_text_search_url(query, date_from=date_from, date_to=date_to,
                                  stages=stages, page=1, page_size=page_size)
    html = await client.fetch_text(first)
    total = total_found(html)
    metas = parse_search_page(html)
    if not metas:
        return [], total

    wanted = min(total, limit) if limit else total
    pages = _pages(wanted, page_size, limit)
    if pages > 1:
        async def one(p: int) -> list[ContractMeta]:
            url = build_text_search_url(query, date_from=date_from, date_to=date_to,
                                        stages=stages, page=p, page_size=page_size)
            try:
                return parse_search_page(await client.fetch_text(url))
            except Exception as e:
                log.warning("поиск «%s» стр.%d: %s", query[:40], p, e)
                return []

        for chunk in await asyncio.gather(*(one(p) for p in range(2, pages + 1))):
            metas.extend(chunk)

    seen: set[str] = set()
    uniq: list[ContractMeta] = []
    for m in metas:
        if m.reestr_number in seen:
            continue
        seen.add(m.reestr_number)
        uniq.append(m)
    return (uniq[:limit] if limit else uniq), total


def _pages(total: int, page_size: int, limit: int) -> int:
    want = min(total, limit) if limit else total
    if want <= 0:
        return 1
    return min(settings.eis_max_pages, (want + page_size - 1) // page_size)
