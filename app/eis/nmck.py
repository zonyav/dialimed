

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from ..config import settings
from .client import EisClient

log = logging.getLogger(__name__)

NOTICE_URL = (settings.eis_base +
              "/epz/order/notice/ea44/view/common-info.html?regNumber={pn}")

_NMCK = re.compile(
    r'section__title[^>]*>\s*Начальная \(максимальная\) цена контракта\s*</span>\s*'
    r'<span[^>]*section__info[^>]*>\s*([\d\s ]+[,.]\d{2})', re.S)

MANY_PRICES = "совместная закупка: начальных цен несколько"


@dataclass(slots=True)
class Nmck:

    value: Optional[float] = None
    note: str = ""

    def __bool__(self) -> bool:
        return self.value is not None or bool(self.note)


def _money(s: str) -> Optional[float]:
    try:
        return round(float(s.replace(" ", "").replace(" ", "")
                           .replace(" ", "").replace(",", ".")), 2)
    except ValueError:
        return None


def parse_nmck(html: str) -> Nmck:

    values: list[float] = []
    for m in _NMCK.finditer(html or ""):
        v = _money(m.group(1))
        if v and v not in values:
            values.append(v)
    if not values:
        return Nmck()
    if len(values) > 1:
        return Nmck(note=MANY_PRICES)
    return Nmck(value=values[0])


async def fetch_nmck(client: EisClient, purchase_number: str) -> Nmck:
    pn = (purchase_number or "").strip()
    if not re.fullmatch(r"\d{19}", pn):
        return Nmck()
    try:
        html = await client.fetch_text(NOTICE_URL.format(pn=pn))
    except Exception as e:
        log.debug("извещение %s: %s: %s", pn, type(e).__name__, e)
        return Nmck()
    return parse_nmck(html)


async def fetch_nmck_many(
    client: EisClient,
    purchase_numbers: Iterable[str],
    concurrency: int = 4,
    progress: callable | None = None,
) -> dict[str, Nmck]:

    uniq = [p for p in dict.fromkeys(x for x in purchase_numbers if x)]
    out: dict[str, Nmck] = {}
    if not uniq:
        return out
    sem = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()

    async def one(pn: str) -> None:
        nonlocal done
        async with sem:
            value = await fetch_nmck(client, pn)
        if value:
            out[pn] = value
        if progress:
            async with lock:
                done += 1
                progress(done, len(uniq))

    await asyncio.gather(*(one(p) for p in uniq))
    return out
