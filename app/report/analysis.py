

from __future__ import annotations

import statistics
from typing import Iterable

from ..models import DASH, NO_BIDDING, NO_NMCK, SMALL_VOLUME, Row


def discount_stats(rows: Iterable[Row]) -> dict:

    metas = {}
    for r in rows:
        metas[r.meta.reestr_number or r.meta.source_ref or id(r.meta)] = r.meta

    known = [m.discount_pct for m in metas.values() if m.discount_pct is not None]
    lowered = [v for v in known if v > 0]
    no_bidding = sum(1 for m in metas.values()
                     if m.no_bidding_note in (NO_BIDDING, SMALL_VOLUME))
    incomparable = sum(1 for m in metas.values()
                       if m.no_bidding_note not in (DASH, NO_BIDDING, NO_NMCK,
                                                    SMALL_VOLUME))
    return {
        "median": _pct(statistics.median(lowered)) if lowered else None,
        "max": _pct(max(known)) if known else None,
        "lowered": len(lowered),
        "known": len(known),
        "contracts": len(metas),
        "no_bidding": no_bidding,
        "incomparable": incomparable,
    }


def _pct(v: float) -> float:
    return round(v, 1) if abs(v) >= 0.1 else round(v, 2)


def _contracts(n: int) -> str:
    return "контракта" if n % 10 == 1 and n % 100 != 11 else "контрактов"


def _contracts_nom(n: int) -> str:

    if n % 100 in (11, 12, 13, 14):
        return "контрактов"
    last = n % 10
    if last == 1:
        return "контракт"
    if last in (2, 3, 4):
        return "контракта"
    return "контрактов"


def discount_note(d: dict) -> str:
    tails: list[str] = []
    if not d["known"] and d["no_bidding"]:
        head = "торгов не было"
    elif not d["known"]:
        head = "начальная цена неизвестна"
    elif not d["lowered"]:
        head = ("контракт заключён по начальной цене" if d["known"] == 1
                else f"все {d['known']} {_contracts_nom(d['known'])} — по начальной цене")
    else:
        head = (f"снижали в {d['lowered']} из {d['known']} {_contracts(d['known'])}"
                + (f", максимум −{d['max']}%" if d["max"] else ""))
    if d["no_bidding"] and d["known"]:
        tails.append(f"ещё {d['no_bidding']} без торгов")
    if d.get("incomparable"):
        tails.append(f"у {d['incomparable']} {_contracts(d['incomparable'])}"
                     " цену сравнить не с чем")
    return "; ".join([head] + tails)
