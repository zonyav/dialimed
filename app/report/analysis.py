

from __future__ import annotations

import statistics
from typing import Iterable, Optional

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
        "median_all": _pct(statistics.median(known)) if known else None,
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


# ── Подсветка выбивающихся цен: расчёт готов, в отчёт пока не выводится ──
# Цену за единицу сравниваем только между строками, где это заведомо одно
# и то же изделие: один код КТРУ, одна единица измерения и один номер РУ.
# Сравнение внутри кода КТРУ бессмысленно — под одним кодом лежат и лампа
# за тысячу, и светильник за триста тысяч.
OUTLIER_MIN = 4          # меньше строк — сравнивать не с чем
OUTLIER_WHISKER = 1.5    # классический размах: четверть ± 1.5 межквартильных
OUTLIER_GAP = 0.20       # и при этом отличие от середины хотя бы на пятую часть


def _unit_key(unit: str) -> str:
    return " ".join((unit or "").lower().replace("ё", "е").split())


def _group_key(pos) -> tuple[str, str, str]:
    return (pos.ktru, _unit_key(pos.unit), (pos.ru_number or "").strip())


def price_bounds(rows: Iterable[Row]) -> dict[tuple[str, str, str], tuple]:
    """Границы обычной цены по каждой группе одинаковых изделий."""

    buckets: dict[tuple[str, str, str], list[float]] = {}
    for r in rows:
        p = r.pos
        if not p.ktru or not p.ru_number or not p.price or p.price <= 0:
            continue
        buckets.setdefault(_group_key(p), []).append(p.price)

    out: dict[tuple[str, str, str], tuple] = {}
    for key, prices in buckets.items():
        if len(prices) < OUTLIER_MIN:
            continue
        prices.sort()
        q1, q3 = _quartiles(prices)
        gap = q3 - q1
        mid = statistics.median(prices)
        out[key] = (q1 - OUTLIER_WHISKER * gap, q3 + OUTLIER_WHISKER * gap, mid)
    return out


def _quartiles(sorted_prices: list[float]) -> tuple[float, float]:
    half = len(sorted_prices) // 2
    low = sorted_prices[:half]
    high = sorted_prices[half + 1:] if len(sorted_prices) % 2 else sorted_prices[half:]
    return statistics.median(low), statistics.median(high)


def price_flag(row: Row, bounds: dict[tuple[str, str, str], tuple]) -> str:
    """'дорого', 'дёшево' или '' — выбивается ли цена из соседних контрактов."""

    p = row.pos
    if not p.price or p.price <= 0:
        return ""
    found = bounds.get(_group_key(p))
    if not found:
        return ""
    low, high, mid = found
    if not mid or abs(p.price / mid - 1) < OUTLIER_GAP:
        return ""
    if p.price > high:
        return "дорого"
    if p.price < low:
        return "дёшево"
    return ""


# ── Сводка по рынку ─────────────────────────────────────────────────────
# Считаем по позициям, а не по контрактам: цена контракта повторяется
# в каждой его строке, складывать её нельзя.

def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def market_summary(rows: list[Row]) -> dict:

    from .. import groups

    titles = groups.assign(r.pos.manufacturer for r in rows)

    contracts = {r.meta.reestr_number for r in rows if r.meta.reestr_number}
    money = sum(r.pos.total or 0 for r in rows)
    units = sum(r.pos.quantity or 0 for r in rows)
    prices = [r.pos.price for r in rows if r.pos.price]
    drops = [r.meta.discount_pct for r in rows if r.meta.discount_pct is not None]
    known = [r for r in rows if r.pos.manufacturer]

    by_maker: dict[str, list[Row]] = {}
    for r in rows:
        by_maker.setdefault(titles.get(r.pos.manufacturer, ""), []).append(r)

    makers = []
    for name, group in by_maker.items():
        got = sum(x.pos.total or 0 for x in group)
        makers.append({
            "name": name or "производитель не определён",
            "unknown": not name,
            "positions": len(group),
            "contracts": len({x.meta.reestr_number for x in group
                              if x.meta.reestr_number}),
            "units": round(sum(x.pos.quantity or 0 for x in group), 2),
            "sum": round(got, 2),
            "share": round(100 * got / money, 1) if money else 0.0,
            "price": _median([x.pos.price for x in group if x.pos.price]),
        })
    makers.sort(key=lambda m: (m["unknown"], -m["sum"]))

    months: dict[str, dict] = {}
    for r in rows:
        day = r.meta.conclusion_date
        if day is None:
            continue
        cell = months.setdefault(f"{day.year}-{day.month:02d}",
                                 {"month": f"{day.month:02d}.{day.year}",
                                  "contracts": set(), "sum": 0.0, "units": 0.0})
        cell["contracts"].add(r.meta.reestr_number)
        cell["sum"] += r.pos.total or 0
        cell["units"] += r.pos.quantity or 0

    timeline = []
    for key in sorted(months):
        cell = months[key]
        timeline.append({
            "month": cell["month"],
            "contracts": len(cell["contracts"]),
            "sum": round(cell["sum"], 2),
            "price": round(cell["sum"] / cell["units"], 2) if cell["units"] else None,
        })

    return {
        "contracts": len(contracts),
        "positions": len(rows),
        "units": round(units, 2),
        "sum": round(money, 2),
        "price": _median(prices),
        "drop": _median(drops),
        "makers_known": sum(1 for m in makers if not m["unknown"]),
        "known_share": round(100 * len(known) / len(rows), 1) if rows else 0.0,
        "makers": makers,
        "timeline": timeline,
    }
