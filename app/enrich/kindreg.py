

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import settings
from . import nkmi
from .rzn import RznRecord, RznEnricher

log = logging.getLogger(__name__)

PAGE = 200
MAX_RECORDS = settings.kind_slice_max
WHOLE_REGISTRY = 50000


@dataclass(slots=True)
class KindSlice:
    code: str = ""
    name: str = ""
    records: list[RznRecord] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.records)


async def fetch(code: str, enricher: RznEnricher) -> KindSlice:
    code = (code or "").strip()
    kind = await nkmi.lookup(code)
    if not kind.found or not kind.record_id:
        return KindSlice(code=code, name=kind.name,
                         note=kind.note or "у вида нет идентификатора в справочнике")
    items = await _fetch_pages(kind.record_id, enricher)
    if items is None:
        return KindSlice(code=code, name=kind.name,
                         note="реестр медизделий сейчас не отвечает")
    total = len(items)
    records = [RznEnricher._record(it, "nkmi", 1.0) for it in items[:MAX_RECORDS]]
    return KindSlice(code=code, name=kind.name, records=records, total=total,
                     truncated=total > MAX_RECORDS)


async def _fetch_pages(record_id: str, enricher: RznEnricher) -> list[dict] | None:
    out: list[dict] = []
    page = 0
    while True:
        body = await enricher.raw_filter({"nomClassifierMedicalRfIds": [record_id]},
                                         page=page, size=PAGE,
                                         cache_key=f"nkmi:{record_id}:{page}")
        if body is None:
            return None if not out else out
        total = body.get("totalElements")
        if isinstance(total, int) and total >= WHOLE_REGISTRY:
            log.warning("срез по виду %s вернул весь реестр (%s записей) — "
                        "фильтр не сработал", record_id, total)
            return []
        content = body.get("content") or []
        out.extend(content)
        page += 1
        if len(content) < PAGE or len(out) >= MAX_RECORDS or page > 20:
            break
        if isinstance(total, int) and len(out) >= total:
            break
    return out
