"""Снимок результата прогона и сверка двух снимков.

Нужен, когда код правят «на чистоту», а результат меняться не должен.
Снимок — это все строки отчёта плюс внутренние поля, которые в отчёт
не идут, но решают, что в нём окажется: обозначение, чем определён
производитель, уверенность, номера. Сверка показывает каждое поле,
которое разошлось.

    python tests/t_same.py before.json 32.50.11.000-00000080 --from=01.01.2025
    ... правки ...
    python tests/t_same.py after.json  32.50.11.000-00000080 --from=01.01.2025
    python tests/t_same.py --diff before.json after.json

Прогон идёт по обычному кэшу, поэтому второй снимок делается быстро.
Счётчик РЗН «cache» после первого прогона законно вырастает: первый
прогон сам наполняет кэш. Всё остальное обязано совпасть.

Возвращает 1, если нашлись расхождения — годится для проверки в CI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.models import COLUMNS                       # noqa: E402
from app.pipeline import SearchParams, run_online    # noqa: E402


def _cell(v):
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else v


def _show(stage: str, text: str, done: int, total: int) -> None:
    print(f"  [{stage}] {done}/{total} {text[:60]}", flush=True)


def _inner(row) -> dict:
    """Поля, которых нет в отчёте, но которые решают, что в него попадёт."""

    p, m = row.pos, row.meta
    return {
        "reestr": m.reestr_number, "mark": p.mark, "mark_source": p.mark_source,
        "manufacturer_source": p.manufacturer_source, "confidence": p.confidence,
        "rzn_id": p.rzn_id, "erul": p.erul, "tu": p.tu_number,
        "status": p.ru_status, "registry_name": p.ru_registry_name,
        "variants": p.ru_variants, "nmck": m.nmck, "nmck_note": m.nmck_note,
        "amended": m.amended, "way": m.placing_way,
    }


def _order(pair) -> tuple:
    d = pair[0]
    return (str(d.get("Ссылка на ЕИС")), str(d.get("Наименование позиции")),
            str(d.get("КТРУ")), str(d.get("Цена за ед., ₽")),
            str(d.get("Характеристики"))[:80])


async def take(codes: list[str], date_from: str, date_to: str, limit: int) -> dict:
    res = await run_online(SearchParams(ktru=codes, date_from=date_from,
                                        date_to=date_to, limit_per_ktru=limit),
                           _show)
    rows = [{k: _cell(v) for k, v in r.as_dict().items()} for r in res.rows]
    pairs = sorted(zip(rows, [_inner(r) for r in res.rows]), key=_order)
    stats = json.loads(json.dumps(res.stats, ensure_ascii=False, default=str))
    stats.pop("секунд", None)          # время прогона совпадать не обязано
    return {
        "columns": COLUMNS,
        "rows": [p[0] for p in pairs],
        "inner": [p[1] for p in pairs],
        "stats": stats,
        "problems": sorted(f"{p.stage}|{p.ref}|{p.message}" for p in res.problems),
    }


def diff(before: str, after: str) -> int:
    a = json.loads(Path(before).read_text(encoding="utf-8"))
    b = json.loads(Path(after).read_text(encoding="utf-8"))
    bad = 0

    if a["columns"] != b["columns"]:
        print("  колонки отчёта различаются")
        print("   было :", a["columns"])
        print("   стало:", b["columns"])
        bad += 1

    for part, what in (("rows", "строка отчёта"), ("inner", "внутренние поля")):
        if len(a[part]) != len(b[part]):
            print(f"  {what}: было {len(a[part])}, стало {len(b[part])}")
            bad += 1
            continue
        for i, (x, y) in enumerate(zip(a[part], b[part])):
            for k in sorted(set(x) | set(y)):
                if x.get(k) != y.get(k):
                    print(f"  {what} {i}, «{k}»: {x.get(k)!r} -> {y.get(k)!r}")
                    bad += 1

    for k in sorted(set(a["stats"]) | set(b["stats"])):
        if a["stats"].get(k) != b["stats"].get(k):
            print(f"  статистика «{k}»: {a['stats'].get(k)!r} -> {b['stats'].get(k)!r}")
            bad += 1

    for p in [p for p in a["problems"] if p not in b["problems"]][:20]:
        print("  замечание пропало:", p)
        bad += 1
    for p in [p for p in b["problems"] if p not in a["problems"]][:20]:
        print("  замечание появилось:", p)
        bad += 1

    print("\n  результат не изменился" if not bad
          else f"\n  расхождений: {bad}")
    return 1 if bad else 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--diff":
        if len(args) < 3:
            print("  нужны два файла снимков")
            return 2
        return diff(args[1], args[2])

    out = args[0]
    codes = [a for a in args[1:] if not a.startswith("--")]
    if not codes:
        print("  укажите хотя бы один код КТРУ")
        return 2
    date_from, date_to, limit = "01.01.2025", "", 0
    for a in args[1:]:
        if a.startswith("--from="):
            date_from = a.split("=", 1)[1]
        elif a.startswith("--to="):
            date_to = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])

    data = asyncio.run(take(codes, date_from, date_to, limit))
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(f"\n  {out}: строк {len(data['rows'])}, "
          f"замечаний {len(data['problems'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
