

import asyncio, sys, io, time, statistics, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx
from app.config import settings
from app.eis.client import EisClient
from app.eis.search import search_ktru

DOC = "https://zakupki.gov.ru/epz/contract/contractCard/document-info.html?reestrNumber={}"


async def numbers(n: int) -> list[str]:
    async with EisClient(concurrency=2) as c:
        metas, _ = await search_ktru(c, "27.40.39.110-00000002",
                                     date_from="01.01.2025", limit=n)
    return [m.reestr_number for m in metas][:n]


async def burst(nums: list[str], conc: int) -> None:
    sem = asyncio.Semaphore(conc)
    limits = httpx.Limits(max_connections=conc + 4, max_keepalive_connections=conc)
    async with httpx.AsyncClient(headers=settings.http_headers, timeout=60.0,
                                 limits=limits, follow_redirects=True,
                                 verify=False) as cli:
        async def one(i, rn):
            async with sem:
                t = time.perf_counter()
                try:
                    r = await cli.get(DOC.format(rn))
                    return i, time.perf_counter() - t, (
                        "ok" if r.status_code == 200 else f"HTTP {r.status_code}")
                except Exception as e:
                    return i, time.perf_counter() - t, type(e).__name__

        t0 = time.perf_counter()
        res = sorted(await asyncio.gather(*(one(i, r) for i, r in enumerate(nums))))
        total = time.perf_counter() - t0

    print(f"\n--- всплеск: {len(nums)} запросов, {conc} потока ---")
    print(f"{'запросы':>13} | успех | медиана | ошибки")
    print("-" * 62)
    step = 40
    for s in range(0, len(res), step):
        ch = res[s:s + step]
        ok = [d for _, d, st in ch if st == "ok"]
        errs = collections.Counter(st for _, _, st in ch if st != "ok")
        print(f"{s + 1:5d}–{s + len(ch):<7d} | {len(ok):2d}/{len(ch):<2d} | "
              f"{round(statistics.median(ok) * 1000) if ok else '—':>6} мс | {dict(errs) or '—'}")
    ok_total = sum(1 for _, _, st in res if st == "ok")
    print(f"прошло {ok_total} из {len(res)} за {total:.0f}с")
    return ok_total


async def recovery() -> None:
    print("\n--- восстановление после отказа ---")
    async with httpx.AsyncClient(headers=settings.http_headers, timeout=40.0,
                                 follow_redirects=True, verify=False) as cli:
        t0 = time.perf_counter()
        for _ in range(30):
            await asyncio.sleep(5)
            try:
                code = (await cli.get(DOC.format("2180870026025000389"))).status_code
            except Exception as e:
                code = type(e).__name__
            el = time.perf_counter() - t0
            if code == 200:
                print(f"   восстановилось через {el:.0f} с")
                return
        print("   за 150 с не восстановилось")


async def steady(nums: list[str], rate: float) -> None:
    interval = 1.0 / rate
    ok = bad = 0
    async with httpx.AsyncClient(headers=settings.http_headers, timeout=40.0,
                                 follow_redirects=True, verify=False) as cli:
        t0 = time.perf_counter()
        for rn in nums:
            try:
                code = (await cli.get(DOC.format(rn))).status_code
                ok += code == 200
                bad += code == 429
            except Exception:
                bad += 1
            if bad >= 3:
                break
            await asyncio.sleep(interval)
        dt = time.perf_counter() - t0
    verdict = "держит" if bad == 0 else "УПИРАЕТСЯ"
    print(f"   {rate:.1f} зап/с: успешно {ok}, отказов {bad} за {dt:.0f}с — {verdict}")


async def main():
    nums = await numbers(200)
    print(f"контрактов для замера: {len(nums)}")
    await burst(nums[:200], conc=4)
    await recovery()
    print("\n--- ровный темп ---")
    await steady(nums[:50], settings.eis_rate)
    print(f"\nтекущая настройка MI_EIS_RATE = {settings.eis_rate} зап/с, "
          f"потоков {settings.eis_concurrency}")

if __name__ == "__main__":
    asyncio.run(main())
