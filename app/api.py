from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import DATA, settings, STAGES, DEFAULT_STAGES
from .eis.client import EisClient
from .eis.search import parse_ktru_list
from .enrich.nameparse import ERUL_RE
from .enrich.rzn import RznEnricher
from .models import Problem, RunResult
from .pipeline import SearchParams, run_online
from .report.excel import save_report

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"

JOBS_KEEP = 8
PRODUCERS_LIMIT = 500
INDEX_FILE = DATA / "reports.json"

app = FastAPI(title="Поиск медизделий в контрактах ЕИС", docs_url=None,
              redoc_url=None)


@dataclass
class Job:
    id: str
    params: SearchParams
    note: str = ""
    task: Optional[asyncio.Task] = None
    result: Optional[RunResult] = None
    file: Optional[Path] = None
    error: str = ""
    finished: bool = False
    stopped: bool = False
    started: datetime = field(default_factory=datetime.now)
    last: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    subscribers: set = field(default_factory=set)
    seq: int = 0
    save_error: str = ""
    saved_at: Optional[datetime] = None

    def emit(self, kind: str, **data: Any) -> None:
        item = {"kind": kind, **data}
        self.seq += 1
        if kind == "progress":
            self.last = item
        elif kind == "done":
            self.summary = item
        for q in list(self.subscribers):
            q.put_nowait((self.seq, item))

    def subscribe(self, q: asyncio.Queue) -> int:
        self.subscribers.add(q)
        return self.seq

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def save(self) -> str:

        if self.result is None or not self.file:
            return ""
        try:
            await asyncio.to_thread(save_report, self.result, self.file, self.note)
            self.save_error = ""
            self.saved_at = datetime.now()
        except Exception as e:
            self.save_error = _save_error_text(e, self.file)
            log.warning("не удалось записать отчёт %s: %s: %s",
                        self.file.name, type(e).__name__, e)
        return self.save_error


JOBS: dict[str, Job] = {}
RUN_LOCK = asyncio.Lock()
CURRENT: Optional[str] = None

# страница раз в 10 секунд стучится в /api/alive; если стучать перестали,
# значит её закрыли — и программе больше незачем висеть в памяти
ALIVE_TIMEOUT = 45.0
ALIVE_CHECK = 15.0
_last_ping: float = 0.0
_watchdog: Optional[asyncio.Task] = None


def _busy_job() -> Optional[Job]:
    job = JOBS.get(CURRENT or "")
    return job if job is not None and not job.finished else None


def _busy_message(job: Job) -> str:
    mins = int((datetime.now() - job.started).total_seconds() // 60)
    ago = f"{mins} мин назад" if mins else "только что"
    what = ", ".join(job.params.ktru) or "—"
    return (f"Уже идёт прогон по КТРУ {what}, начат в {job.started:%H:%M} ({ago}). "
            "Дождитесь его окончания или остановите кнопкой «Остановить».")


class RunRequest(BaseModel):
    ktru: str = ""
    date_from: str = "01.01.2025"
    date_to: str = ""
    stages: list[str] = Field(default_factory=lambda: list(DEFAULT_STAGES))
    limit_per_ktru: int = 0


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.post("/api/alive")
async def alive() -> dict:

    global _last_ping, _watchdog

    _last_ping = time.monotonic()
    if _watchdog is None or _watchdog.done():
        _watchdog = asyncio.create_task(_watch_page())
    return {"ok": True}


async def _watch_page() -> None:

    while True:
        await asyncio.sleep(ALIVE_CHECK)
        if _busy_job() is not None or RUN_LOCK.locked():
            continue
        if time.monotonic() - _last_ping < ALIVE_TIMEOUT:
            continue
        log.info("страница закрыта — завершаю работу")
        for job in list(JOBS.values()):
            if job.task is not None and not job.task.done():
                job.task.cancel()
        await asyncio.sleep(0.2)
        os._exit(0)


@app.get("/api/status")
async def status() -> dict:
    from .update import check as check_update

    async with RznEnricher() as rzn:
        rzn_ok = await rzn.available()
    busy = _busy_job()
    return {
        "rzn": {"ok": rzn_ok},
        "update": await check_update(),
        "stages": STAGES,
        "busy": RUN_LOCK.locked() or busy is not None,
        "current": busy.id if busy else "",
    }


def _cap(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


class KtruCheckRequest(BaseModel):
    ktru: str = ""
    date_from: str = "01.01.2025"
    date_to: str = ""
    stages: list[str] = Field(default_factory=lambda: list(DEFAULT_STAGES))


@app.post("/api/ktru-check")
async def ktru_check(req: KtruCheckRequest) -> dict:

    from .eis.ktru_card import bridge_kind, check_codes
    from .eis.search import parse_ktru_bad
    from .enrich.nkmi import parse_nkmi_list

    codes = parse_ktru_list(req.ktru)
    bad = parse_ktru_bad(req.ktru)
    kinds = parse_nkmi_list(req.ktru)
    if not codes and not bad and not kinds:
        raise HTTPException(400, "Не найдено ни одного кода. Код КТРУ выглядит "
                                 "так: 32.50.11.000-00000080 или "
                                 "32.50.13.190-00247; код вида медизделия "
                                 "(НКМИ) — это просто число: 191220")
    date_from = req.date_from or "01.01.2025"
    stages = req.stages or list(DEFAULT_STAGES)
    found = []
    nkmi_out: list[dict] = []
    async with EisClient() as client:
        bridges = list(await asyncio.gather(*(
            bridge_kind(client, code, date_from=date_from, date_to=req.date_to,
                        stages=stages) for code in kinds)))
        by_kind: dict[str, object] = {}
        for br in bridges:
            nkmi_out.append({"code": br.code, "name": br.name,
                             "status": br.status, "note": br.note,
                             "codes": [c.code for c in br.checks],
                             "confirmed": br.confirmed})
            for ch in br.checks:
                by_kind.setdefault(ch.code, ch)
        manual = [c for c in codes if c not in by_kind]
        if manual:
            found = await check_codes(client, manual, date_from=date_from,
                                      date_to=req.date_to, stages=stages)
        found = found + [by_kind[c] for c in by_kind]
    return {
        "codes": [
            {"code": c.code, "ok": c.ok, "name": c.name, "unit": c.unit,
             "excluded": c.excluded, "contracts": c.contracts,
             "contracts_all": c.contracts_all, "note": c.note,
             "from_kind": c.from_kind, "exact": c.exact,
             "kind_ok": c.kind_ok, "kind_found": c.kind_found,
             "kind_note": c.kind_note}
            for c in found
        ],
        "nkmi": nkmi_out,
        "bad": bad,
    }


@app.post("/api/run")
async def start(req: RunRequest) -> dict:
    global CURRENT

    codes = parse_ktru_list(req.ktru)
    if not codes:
        raise HTTPException(400, "Не найдено ни одного кода КТРУ. "
                                 "Формат кода: 32.50.11.000-00000080 "
                                 "или 32.50.13.190-00247")
    busy = _busy_job()
    if busy is not None or RUN_LOCK.locked():
        raise HTTPException(409, _busy_message(busy) if busy else
                            "Уже выполняется другой прогон, дождитесь его завершения")

    params = SearchParams(
        ktru=codes,
        date_from=req.date_from or "01.01.2025",
        date_to=req.date_to,
        stages=req.stages or list(DEFAULT_STAGES),
        limit_per_ktru=max(0, req.limit_per_ktru),
    )
    note = (f"КТРУ: {', '.join(codes) or '—'}; период с {params.date_from}"
            f"{' по ' + params.date_to if params.date_to else ''}; "
            f"стадии: {', '.join(STAGES.get(s, s) for s in params.stages)}")

    job = Job(id=uuid.uuid4().hex[:12], params=params, note=note)
    JOBS[job.id] = job
    _forget_old_jobs()
    CURRENT = job.id
    job.task = asyncio.create_task(_run(job))
    return {"job": job.id, "ktru": codes}


def _forget_old_jobs() -> None:
    for old in list(JOBS)[:-JOBS_KEEP]:
        job = JOBS.get(old)
        if job is not None and job.finished:
            JOBS.pop(old, None)


@app.post("/api/stop/{job_id}")
async def stop_job(job_id: str) -> dict:

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Прогон не найден: возможно, программу "
                                 "перезапустили. Запустите поиск заново.")
    if job.finished:
        return {"ok": True, "finished": True,
                "message": "Прогон уже завершён — останавливать нечего."}
    job.stopped = True
    if job.task is not None:
        job.task.cancel()
    return {"ok": True, "finished": False,
            "message": "Останавливаю прогон. Скачанные контракты остались "
                       "в кэше — повторный прогон пройдёт быстрее."}


async def _run(job: Job) -> None:
    global CURRENT

    async with RUN_LOCK:
        def progress(stage: str, text: str, done: int, total: int) -> None:
            job.emit("progress", stage=stage, text=text, done=done, total=total)

        try:
            job.emit("progress", stage="старт", text="подготовка", done=0, total=1)
            res = await run_online(job.params, progress)
            job.result = res

            if res.rows:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                tag = (job.params.ktru[0].replace(".", "_") if job.params.ktru else "папка")
                job.file = settings.out_dir / f"медизделия_{tag}_{stamp}.xlsx"
                await job.save()
                _index_add(job)

            job.emit("done", **_done_payload(job, res))
        except asyncio.CancelledError:
            job.error = ("Прогон остановлен. Всё, что программа успела скачать "
                         "из ЕИС, осталось в кэше: повторный прогон по тем же "
                         "кодам пройдёт заметно быстрее.")
            job.emit("error", message=job.error)
        except Exception as e:
            log.exception("прогон %s упал", job.id)
            job.error = _human_error(e)
            job.emit("error", message=job.error)
        finally:
            job.finished = True
            if CURRENT == job.id:
                CURRENT = None
            job.emit("eof")
            _forget_old_jobs()
            asyncio.create_task(_tidy_cache())


def _done_payload(job: Job, res: RunResult) -> dict:
    return {
        "rows": len(res.rows),
        "problems": len(res.problems),
        "problem_notes": _run_notes(res),
        "stats": _jsonable(res.stats),
        "file": job.file.name if job.file else "",
        "save_error": job.save_error,
        "summary": _summary(res),
        "producers": _producers(res),
        "remarks": _remarks(res),
    }


async def _tidy_cache() -> None:

    from . import maintenance

    try:
        await asyncio.to_thread(maintenance.autocompact)
    except Exception as e:
        log.warning("обслуживание кэша: %s: %s", type(e).__name__, e)


def _human_error(e: BaseException) -> str:

    tech = f"{type(e).__name__}: {e}".strip()
    name = type(e).__name__.lower()
    if isinstance(e, PermissionError):
        head = ("Прогон прервался: не удалось записать файл отчёта. Скорее "
                "всего, он открыт в Excel — закройте его и повторите поиск.")
    elif isinstance(e, MemoryError):
        head = ("Прогон прервался: не хватило оперативной памяти. Попробуйте "
                "сузить период или запускать коды КТРУ по одному.")
    elif isinstance(e, (TimeoutError, ConnectionError, OSError)) or any(
            k in name for k in ("timeout", "connect", "network", "protocol", "ssl")):
        head = ("Прогон прервался. Похоже, пропала связь с ЕИС — проверьте "
                "интернет и запустите поиск заново; уже скачанное сохранено "
                "в кэше, повтор пройдёт быстро.")
    else:
        head = ("Прогон прервался из-за ошибки в программе. Уже скачанное "
                "сохранено в кэше, повтор пройдёт быстро.")
    return f"{head} Подробности: {tech}"


def _save_error_text(e: BaseException, path: Path) -> str:
    if isinstance(e, PermissionError):
        return (f"Не удалось записать отчёт: файл {path.name} открыт в Excel. "
                "Закройте его и запустите прогон заново.")
    if isinstance(e, OSError) and getattr(e, "errno", 0) == 28:
        return "Не удалось записать отчёт: на диске кончилось место."
    return (f"Не удалось записать отчёт {path.name}. "
            f"Подробности: {type(e).__name__}: {e}")


def _run_notes(res: RunResult) -> list[str]:

    notes: list[str] = []
    for p in res.problems:
        if p.ref in ("—", ""):
            notes.append(f"{p.stage}: {p.message}")
        elif p.stage == "вход":
            notes.append(_input_note(p))
        elif p.stage == "поиск":
            notes.append(f"Код КТРУ {p.ref}: поиск в ЕИС не выполнен "
                         f"({p.message}).")
    return notes[:5]


def _input_note(p: Problem) -> str:
    return f"{_cap(p.message)} {p.ref}".strip()


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _contract_key(r) -> str:
    return r.meta.reestr_number or r.meta.source_ref or str(id(r.meta))


def _summary(res: RunResult) -> dict:

    from .report.analysis import discount_note, discount_stats

    rows = res.rows
    total = len(rows)
    with_manuf = sum(1 for r in rows if r.pos.manufacturer)
    with_ru = sum(1 for r in rows if r.pos.ru_number)
    contracts = len({_contract_key(r) for r in rows})
    producers = {r.pos.manufacturer for r in rows if r.pos.manufacturer}
    money = sum(r.pos.total or 0 for r in rows)
    dates = sorted(r.meta.conclusion_date for r in rows if r.meta.conclusion_date)
    d = discount_stats(rows)
    stats = res.stats or {}
    return {
        "positions": total,
        "contracts": contracts,
        "contracts_found": stats.get("контрактов найдено", contracts),
        "with_manufacturer": with_manuf,
        "with_manufacturer_pct": round(100 * with_manuf / total) if total else 0,
        "with_ru": with_ru,
        "with_ru_pct": round(100 * with_ru / total) if total else 0,
        "producers": len(producers),
        "suppliers": len({r.meta.supplier for r in rows if r.meta.supplier}),
        "customers": len({r.meta.customer for r in rows if r.meta.customer}),
        "sum": round(money, 2),
        "first": dates[0].strftime("%d.%m.%Y") if dates else "",
        "last": dates[-1].strftime("%d.%m.%Y") if dates else "",
        "discount": d["median"],
        "discount_note": discount_note(d),
        "seconds": stats.get("секунд"),
    }


def _producers(res: RunResult) -> list[dict]:

    groups: dict[str, list] = defaultdict(list)
    for r in res.rows:
        groups[r.pos.manufacturer or ""].append(r)

    total = len(res.rows)
    out = []
    for name, rs in groups.items():
        prices = [x.pos.price for x in rs if x.pos.price is not None]
        out.append({
            "name": name or "производитель не определён",
            "unknown": not name,
            "positions": len(rs),
            "contracts": len({_contract_key(x) for x in rs}),
            "units": round(sum(x.pos.quantity or 0 for x in rs), 2),
            "sum": round(sum(x.pos.total or 0 for x in rs), 2),
            "share": round(100 * len(rs) / total, 1) if total else 0,
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "declarant": next((x.pos.declarant for x in rs if x.pos.declarant), ""),
            "ru": sorted({x.pos.ru_number for x in rs if x.pos.ru_number})[:5],
        })
    out.sort(key=lambda m: (m["unknown"], -m["positions"], m["name"]))
    return out[:PRODUCERS_LIMIT]


def _remarks(res: RunResult) -> list[str]:

    rows = res.rows
    out: list[str] = []
    no_manuf = sum(1 for r in rows if not r.pos.manufacturer)
    no_ru = sum(1 for r in rows if not r.pos.ru_number)
    no_price = sum(1 for r in rows if r.pos.price is None)
    no_qty = sum(1 for r in rows if r.pos.quantity is None or r.pos.quantity_undefined)
    mismatch = sum(
        1 for r in rows
        if r.pos.price is not None and r.pos.quantity is not None
        and r.pos.total is not None
        and abs(r.pos.price * r.pos.quantity - r.pos.total) > max(
            1.0, 0.01 * abs(r.pos.total)))
    if no_manuf:
        out.append(f"Производитель не определён у {no_manuf} позиций — "
                   f"в файле такие ячейки выделены красным.")
    if no_ru:
        out.append(f"Номер регистрационного удостоверения не найден "
                   f"у {no_ru} позиций.")
    if no_price:
        out.append(f"Цена за единицу отсутствует в контракте у {no_price} позиций.")
    if no_qty:
        out.append(f"Количество не определено контрактом у {no_qty} позиций.")
    if mismatch:
        out.append(f"У {mismatch} позиций сумма не сходится с ценой × количеством — "
                   f"так в самом контракте.")
    return out


@app.get("/api/job/{job_id}")
async def job_state(job_id: str) -> dict:

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Прогон не найден: возможно, программу "
                                 "перезапустили. Готовый отчёт, если он успел "
                                 "сохраниться, лежит в папке out — его видно "
                                 "в разделе «Последние отчёты».")
    return {
        "job": job.id,
        "finished": job.finished,
        "error": job.error,
        "progress": job.last,
        "summary": job.summary,
        "file": job.file.name if job.file else "",
        "started": job.started.isoformat(timespec="seconds"),
        "stopped": job.stopped,
        "note": job.note,
        "rows": len(job.result.rows) if job.result else 0,
        "save_error": job.save_error,
        "saved_at": job.saved_at.isoformat(timespec="seconds") if job.saved_at else "",
    }


@app.get("/api/current")
async def current_job() -> dict:

    running = _busy_job() or next(
        (j for j in reversed(list(JOBS.values())) if not j.finished), None)
    last = next((j for j in reversed(list(JOBS.values()))
                 if j.finished and j.result is not None), None)
    return {
        "job": running.id if running else "",
        "note": running.note if running else "",
        "started": running.started.isoformat(timespec="seconds") if running else "",
        "last": last.id if last else "",
        "last_note": last.note if last else "",
        "last_file": last.file.name if last and last.file else "",
    }


@app.get("/api/events/{job_id}")
async def events(job_id: str) -> StreamingResponse:

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Прогон не найден: возможно, программу "
                                 "перезапустили. Запустите поиск заново.")

    q: asyncio.Queue = asyncio.Queue()
    seen = job.subscribe(q)

    async def gen():
        try:
            if job.last:
                yield _sse(job.last)
            if job.finished:
                if job.summary:
                    yield _sse(job.summary)
                elif job.error:
                    yield _sse({"kind": "error", "message": job.error})
                yield "event: eof\ndata: {}\n\n"
                return
            while True:
                try:
                    seq, item = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if seq <= seen:
                    continue
                if item.get("kind") == "eof":
                    yield "event: eof\ndata: {}\n\n"
                    return
                yield _sse(item)
        finally:
            job.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(item: dict) -> str:
    return f"data: {json.dumps(item, ensure_ascii=False)}\n\n"


XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/api/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    job = JOBS.get(job_id)
    if job is None or not job.file:
        raise HTTPException(404, "Файл отчёта не найден. Либо прогон не нашёл "
                                 "ни одной позиции и файл не создавался, либо "
                                 "программу перезапустили — тогда отчёт лежит "
                                 "в папке out.")
    if job.save_error:
        raise HTTPException(409, job.save_error)
    if not job.file.exists():
        raise HTTPException(404, f"Файл отчёта {job.file.name} не найден "
                                 "в папке out — возможно, его переместили.")
    return FileResponse(job.file, media_type=XLSX_TYPE, filename=job.file.name)


@app.get("/api/file/{name}")
async def download_file(name: str) -> FileResponse:

    safe = Path(name).name
    path = settings.out_dir / safe
    if not safe.endswith(".xlsx") or not path.exists():
        raise HTTPException(404, f"Файл {safe} не найден в папке отчётов.")
    return FileResponse(path, media_type=XLSX_TYPE, filename=safe)


def _index_load() -> list[dict]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _index_add(job: Job) -> None:

    if job.file is None or job.result is None:
        return
    items = [x for x in _index_load() if x.get("file") != job.file.name]
    items.insert(0, {
        "file": job.file.name,
        "note": job.note,
        "rows": len(job.result.rows),
        "with_manufacturer": sum(1 for r in job.result.rows if r.pos.manufacturer),
        "saved": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        INDEX_FILE.write_text(json.dumps(items[:50], ensure_ascii=False),
                              encoding="utf-8")
    except OSError as e:
        log.warning("список отчётов не записан: %s", e)


@app.get("/api/reports")
async def reports(limit: int = 20) -> dict:

    index = {x.get("file"): x for x in _index_load()}
    items = []
    for xlsx in sorted(settings.out_dir.glob("*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        meta = index.get(xlsx.name) or {}
        items.append({
            "file": xlsx.name,
            "note": meta.get("note", ""),
            "rows": meta.get("rows"),
            "with_manufacturer": meta.get("with_manufacturer"),
            "saved": meta.get("saved") or datetime.fromtimestamp(
                xlsx.stat().st_mtime).isoformat(timespec="seconds"),
            "size_kb": round(xlsx.stat().st_size / 1024),
        })
    return {"reports": items}


RZN_WIDGET = "https://elk.roszdravnadzor.gov.ru/widget/"


@app.get("/api/rzn/{number:path}")
async def rzn_card(number: str) -> dict:
    number = " ".join((number or "").split())
    erul_only = ""
    if number.upper().startswith("ЕРУЛ"):
        number = number[4:].strip(" :-–")
        erul_only = number
    if not number:
        raise HTTPException(400, "Не указан номер регистрационного удостоверения")
    if not settings.rzn_enabled:
        raise HTTPException(503, "Обращение к реестру Росздравнадзора выключено "
                                 "настройкой MI_RZN_ENABLED")
    async with RznEnricher() as rzn:
        if not await rzn.available():
            raise HTTPException(503, "Реестр Росздравнадзора сейчас не отвечает. "
                                     "Попробуйте позже — или откройте реестр "
                                     "сами, кнопкой ниже.")
        rec = (await rzn.lookup(erul=erul_only) if erul_only
               else await rzn.lookup(ru_number=number))
        if rec is None and not erul_only and (
                re.fullmatch(r"\d{6,}", number) or ERUL_RE.fullmatch(number)):
            rec = await rzn.lookup(erul=number)
    if rec is None:
        return {"found": False, "number": number, "widget": RZN_WIDGET}
    return {
        "found": True, "number": rec.ru_number or number,
        "name": rec.ru_name, "producer": rec.producer,
        "producer_eng": rec.producer_eng, "address": rec.producer_address,
        "declarant": rec.declarant, "inn": rec.declarant_inn,
        "date": rec.ru_date, "status": rec.status, "erul": rec.erul,
        "variants": rec.variants, "models": rec.models_description,
        "widget": RZN_WIDGET,
    }


@app.get("/rzn/{number:path}", response_class=HTMLResponse)
async def rzn_page(number: str) -> str:
    return (WEB / "rzn.html").read_text(encoding="utf-8")


class CacheClearRequest(BaseModel):
    areas: list[str] = Field(default_factory=list)


@app.get("/api/cache")
async def cache_status() -> dict:
    from . import maintenance
    st = await asyncio.to_thread(maintenance.status)
    busy = _busy_job()
    st["busy"] = bool(busy)
    return st


@app.post("/api/cache/clear")
async def cache_clear(req: CacheClearRequest) -> dict:
    from . import maintenance

    busy = _busy_job()
    if busy is not None:
        raise HTTPException(409, _busy_message(busy))
    done = await asyncio.to_thread(
        maintenance.clear, req.areas or list(maintenance.AREAS))
    await asyncio.to_thread(maintenance.compact)
    return {"ok": True, "removed": done, "size_mb": maintenance.size_mb()}
