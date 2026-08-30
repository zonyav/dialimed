
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def _setup_output() -> None:

    # В собранном exe окна консоли нет: sys.stdout там None, и любой print
    # уронил бы программу. Если exe запущен из уже открытой консоли —
    # подключаемся к ней, иначе выводим в никуда.
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            try:
                import ctypes

                if ctypes.windll.kernel32.AttachConsole(-1):
                    sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                                      errors="replace", buffering=1)
                    sys.stderr = sys.stdout
                    return
            except Exception:
                pass
        if sys.stdout is None or sys.stderr is None:
            null = open(os.devnull, "w")
            sys.stdout = sys.stdout or null
            sys.stderr = sys.stderr or null
        return

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


_setup_output()

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings, STAGES, DEFAULT_STAGES
from app.eis.search import parse_ktru_list
from app.enrich.nkmi import parse_nkmi_list
from app.pipeline import SearchParams, run_online
from app.report.excel import save_report


async def expand_kinds(kinds: list[str], *, date_from: str, date_to: str,
                       stages: list[str]) -> tuple[list[str], list[str]]:
    from app.eis.client import EisClient
    from app.eis.ktru_card import bridge_kind

    codes: list[str] = []
    notes: list[str] = []
    async with EisClient() as client:
        print(f"  Разворачиваю коды вида: {', '.join(kinds)} …")
        for br in await asyncio.gather(*(
                bridge_kind(client, k, date_from=date_from, date_to=date_to,
                            stages=stages) for k in kinds)):
            if not br.found:
                notes.append(f"НКМИ {br.code}: {br.note or 'вида нет в номенклатуре'}")
                continue
            if br.confirmed:
                codes += br.confirmed
                notes.append(f"НКМИ {br.code} ({br.name}): "
                             f"{', '.join(br.confirmed)}")
            else:
                notes.append(f"НКМИ {br.code} ({br.name}): "
                             f"{br.note or 'подходящих позиций каталога нет'}")
    return codes, notes


class Bar:

    def __init__(self) -> None:
        self.stage = ""
        self.t0 = time.time()

    def __call__(self, stage: str, text: str, done: int, total: int) -> None:
        if stage != self.stage:
            if self.stage:
                print()
            self.stage = stage
        pct = int(100 * done / total) if total else 0
        width = 26
        fill = int(width * pct / 100)
        bar = "█" * fill + "·" * (width - fill)
        line = f"  [{stage:10s}] {bar} {pct:3d}%  {text[:52]}"
        print(f"\r{line:<108}", end="", flush=True)

    def done(self) -> None:
        if self.stage:
            print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сбор данных о поставках медизделий из контрактов ЕИС (44-ФЗ)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("ktru", nargs="*",
                    help="коды КТРУ через пробел; можно коды вида по НКМИ "
                         "(просто числа) — программа найдёт позиции каталога "
                         "и проверит их по контрактам")
    ap.add_argument("--file", "-f", help="файл со списком кодов КТРУ (по одному в строке)")
    ap.add_argument("--from", dest="date_from", default="01.01.2025",
                    help="дата заключения с (ДД.ММ.ГГГГ), по умолчанию 01.01.2025")
    ap.add_argument("--to", dest="date_to", default="", help="дата заключения по (ДД.ММ.ГГГГ)")
    ap.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                    help="стадии контракта: " + "; ".join(f"{k}={v}" for k, v in STAGES.items()))
    ap.add_argument("--limit", type=int, default=0,
                    help="максимум контрактов на один код КТРУ (0 — без ограничения)")
    ap.add_argument("--out", "-o", help="путь к файлу отчёта .xlsx")
    ap.add_argument("--no-cache", action="store_true", help="игнорировать дисковый кэш")
    ap.add_argument("--compact-cache", action="store_true",
                    help="сжать кэш и вернуть место на диске, затем выйти")
    ap.add_argument("--web", action="store_true", help="запустить веб-интерфейс на localhost")
    ap.add_argument("--port", type=int, default=8123, help="порт веб-интерфейса")
    ap.add_argument("--no-browser", action="store_true",
                    help="не открывать браузер при запуске веб-интерфейса")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.web or (getattr(sys, "frozen", False) and not args.ktru
                    and not args.file and not args.compact_cache):
        import socket
        import uvicorn

        port = args.port
        for candidate in range(args.port, args.port + 20):
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", candidate))
                    port = candidate
                    break
                except OSError:
                    continue
        else:
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                except OSError:
                    print(f"  Порты {args.port}–{args.port + 19} заняты, "
                          f"свободный тоже не нашёлся. Укажите свой: --port 9000")
                    return 2
        if port != args.port:
            print(f"  Порт {args.port} занят, беру {port}")
        url = f"http://127.0.0.1:{port}"
        print(f"\n  Веб-интерфейс: {url}\n  Ctrl+C — остановить\n")
        if not args.no_browser:
            import threading
            import webbrowser

            def _open() -> None:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass

            threading.Timer(1.5, _open).start()
        from app.api import app as web_app

        uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="warning")
        return 0

    if args.no_cache:
        settings.cache_enabled = False

    if args.compact_cache:
        from app.eis.client import Cache

        cache = Cache(settings.cache_db, settings.cache_ttl_days)
        packed, before, after = cache.compact()
        print(f"\n  Сжато записей: {packed}")
        print(f"  Размер кэша  : {before/1024/1024:.0f} МБ -> {after/1024/1024:.0f} МБ\n")
        return 0

    raw = " ".join(args.ktru)
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8") + " " + raw
    codes = list(dict.fromkeys(parse_ktru_list(raw)))
    kinds = parse_nkmi_list(raw)
    if kinds:
        found, notes = asyncio.run(expand_kinds(
            kinds, date_from=args.date_from, date_to=args.date_to,
            stages=[s.strip() for s in args.stages.split(",") if s.strip()]))
        for line in notes:
            print(" ", line)
        codes = list(dict.fromkeys(codes + found))

    if not codes:
        ap.error("укажите хотя бы один код КТРУ или код вида по НКМИ")

    params = SearchParams(
        ktru=codes,
        date_from=args.date_from,
        date_to=args.date_to,
        stages=[s.strip() for s in args.stages.split(",") if s.strip()],
        limit_per_ktru=max(0, args.limit),
    )

    print("\n  Коды КТРУ:", ", ".join(codes))
    print(f"  Период   : с {params.date_from}" + (f" по {params.date_to}" if params.date_to else ""))
    print("  Стадии   :", ", ".join(STAGES.get(s, s) for s in params.stages))
    if params.limit_per_ktru:
        print("  Лимит    :", params.limit_per_ktru, "контрактов на код")
    print()

    bar = Bar()
    t0 = time.time()
    result = asyncio.run(run_online(params, bar))
    bar.done()

    if not result.rows:
        period = f"с {params.date_from}" + (f" по {params.date_to}"
                                            if params.date_to else "")
        print(f"\n  Ничего не найдено: по коду {', '.join(codes)} "
              f"контрактов {period} нет.")
        print("  Проверьте код в каталоге ТРУ и период — опечатка в одну "
              "цифру даёт\n  существующий, но совсем другой код.")
        for p in result.problems[:10]:
            print(f"    - [{p.stage}] {p.ref}: {p.message}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    tag = codes[0].replace(".", "_")
    out = Path(args.out) if args.out else settings.out_dir / f"медизделия_{tag}_{stamp}.xlsx"
    note = (f"КТРУ: {', '.join(codes) or '—'}; период с {params.date_from}"
            f"{' по ' + params.date_to if params.date_to else ''}; "
            f"стадии: {', '.join(STAGES.get(s, s) for s in params.stages)}")
    save_report(result, out, note)

    from app import maintenance

    maintenance.autocompact()

    known = sum(1 for r in result.rows if r.pos.manufacturer)
    print(f"\n  Готово за {time.time() - t0:.1f} с")
    print(f"  Позиций в отчёте     : {len(result.rows)}")
    print(f"  Производитель найден : {known} ({100 * known / len(result.rows):.0f}%)")
    kind = (result.stats or {}).get("срез по виду")
    if isinstance(kind, dict) and kind.get("совпало по правилам"):
        print(f"  Срез по коду вида    : совпало {kind['совпало по правилам']}"
              f" · видов {kind.get('видов', 0)}, "
              f"записей {kind.get('записей в срезах', 0)}")
    if result.problems:
        print(f"  Замечаний при разборе: {len(result.problems)}")
        for p in result.problems[:5]:
            print(f"    - [{p.stage}] {p.ref}: {p.message}")
    print(f"  Файл: {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
