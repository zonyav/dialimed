# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A scraper + enrichment pipeline over the Russian state procurement portal (ЕИС, zakupki.gov.ru, 44-ФЗ).
Given КТРУ catalog codes (or НКМИ device-kind codes), it finds contracts, extracts the medical-device
positions from each contract, works out the **manufacturer / registration certificate (РУ) / РУ holder**
for every position by combining contract text with the Roszdravnadzor (РЗН) device registry, and writes
a single-sheet `.xlsx` report. There is a CLI and a local single-user web UI, and the whole thing ships
to colleagues as one PyInstaller `.exe`.

**There is no LLM anywhere in this project, and no learned-marks knowledge base.** Both were removed
deliberately: everything is rule-based and offline-reproducible. Do not reintroduce either, and do not
add API keys or cloud calls of any kind.

No packaging and no test framework — this is a standalone script tree run in place, with a
GitHub repo used only for history and for building the release exe.

## Commands

```bash
pip install -r requirements.txt
```

```bash
python run.py 27.40.39.110-00000002 --from 01.01.2025
```

```bash
python run.py --web
```

Useful `run.py` flags: `--file codes.txt`, `--to`, `--stages 0,1,2`, `--limit N` (contracts per code),
`--no-specs` (skip .docx attachments), `--no-cache`, `--compact-cache`, `-v` (debug log), `--port`,
`--no-browser`. Bare numeric arguments are treated as НКМИ device-kind codes and expanded to КТРУ codes
before the run.

Build the distributable exe (PyInstaller must be installed in the venv):

```bash
python -m PyInstaller --noconfirm Medizdeliya.spec
```

The result is `dist\Medizdeliya.exe` (~21 MB). It is built windowless (`console=False`) so colleagues
never see a terminal: `run.py::_setup_output` attaches to the parent console when the exe is started
from one, and otherwise sends stdout/stderr to devnull — without that guard every `print` would crash
the frozen app, since `sys.stdout` is None there. Launched with no arguments it starts the web UI and
opens a browser; launched with codes it behaves exactly like the CLI (`run.py` branches on `sys.frozen`).

Because there is no window to close, the page holds the process open: the browser POSTs `/api/alive`
every 10 s, and `api._watch_page` calls `os._exit(0)` once no ping has arrived for 45 s and no job is
running. A page reload is covered by the grace period; closing the tab mid-run lets the run finish and
save the report first.
The Latin file name is deliberate — GitHub mangles non-ASCII characters in release asset names.

Releases are built by `.github/workflows/build.yml` on any `v*` tag: it runs `tests/t_check.py`, writes
`app/_build.py` (VERSION from the tag, REPO from the repo slug — both git-ignored, absent in local
builds), builds the exe and attaches it to the GitHub release. `app/update.py` reads that file and asks
the GitHub API once per process whether a newer tag exists; the UI then shows a link. It never downloads
or launches anything itself — that pattern is what antivirus heuristics flag.

Tests are standalone scripts, not pytest — run them directly; a non-zero exit means failures:

```bash
python tests/t_check.py
```

- `tests/t_check.py` — the main suite: section 1 lints `app/` + `run.py` via `ast` (syntax, unused
  imports, leftover debug), sections 2–3 are unit checks of the parsers, the price/discount logic and
  the registry matching, section 4 runs the print-form parser over any `*.html` in `~/Downloads`
  (skipped if absent — currently 649 forms, and it asserts mark detection stays above 88%), section 5
  hits the network only with `--online`. There is no per-test selector; comment out sections to narrow.
- `tests/t_ratelimit.py` — probes how hard ЕИС can be hit before 429; reports only, changes nothing.

## Architecture

`run.py` (CLI) and `app/api.py` (FastAPI) are two front ends over the same function:
`app/pipeline.py::run_online(SearchParams, progress) -> RunResult`. Everything user-visible goes
through `RunResult.rows` / `.problems` / `.stats`.

Layers:

- `app/eis/` — everything that talks to zakupki.gov.ru. `client.py` is the only HTTP path: it wraps
  httpx with a semaphore, a self-adjusting rate limiter (slows 1.5× on 429, cools down, recovers after
  a 60-request clean streak) and a SQLite blob cache. `search.py` builds/parses search result pages,
  `documents.py` picks the right attachment, `xml_parser.py`/`html_parser.py`/`docx_spec.py` parse the
  three possible sources of positions, `nmck.py` fetches the starting price from the notice (that is
  what makes «Снижение, %» possible), `ktru_card.py` bridges НКМИ kind codes to КТРУ codes.
- `app/enrich/` — turning raw position text into identified devices. `nameparse.py` (rules over the
  contract wording), `rzn.py` (registry lookups by РУ/ТУ/ЕРУЛ number and by name similarity),
  `kindreg.py` + `kindmatch.py` (download every registration of an НКМИ kind, then match a position to
  one of them by rules), `verify.py` (company-name guards), `textutil.py` (type-word and article-token
  helpers shared by the mark passes).
- `app/report/` — `excel.py` writes the single «Позиции» sheet; `analysis.py` computes the discount
  statistics used by the web summary.
- `app/models.py` — `ContractMeta` / `Position` / `Row`. **`Row.as_dict()` is the report schema**: the
  21 columns, their order, and `COLUMNS` all come from it, so changing the report means editing
  `as_dict` plus the widths/formats in `report/excel.py`.
- `app/web/` — two hand-written HTML files with inline vanilla JS, no build step, no dependencies.
  `api.py` serves them as strings.

### Marks are internal, not reported

`Position.mark` (the device model/обозначение parsed out of the contract text) is still computed and
still matters — it feeds the registry matching and the transfer of a known manufacturer to sibling
rows. It is deliberately **not** a report column: the parse is often wrong, and the raw «Текст позиции
из контракта» column lets the user filter for a model themselves. Keep it that way.

### The enrichment order matters

`_enrich()` in `pipeline.py` runs a fixed sequence of passes and several of them run twice on purpose
(`_refine_marks`, `_tidy_marks`) because later passes create new material for earlier ones: specs →
rule parsing → registry by number → kind slice → mark cleanup → transfer of a known manufacturer to
other rows with the same mark/number → bare-mark extension. Passes are idempotent and only ever *fill*
empty fields or *replace* a value with a better-sourced one; `Position.manufacturer_source` and
`.confidence` record which pass won. Reordering or dropping a pass changes results silently — compare
the summary's «с производителем» share before and after.

### Report conventions

The «Позиции» sheet paints «Цена за ед., ₽» pale yellow, and paints an empty «Производитель» or «№ РУ»
cell pale red (each cell independently). «Снижение, %» is a mixed column on purpose: a number when the
discount is computable, otherwise the plain-Russian reason from `ContractMeta.discount_cell`
(«торгов не было», «цена изменена доп. соглашением», …), so the reason is visible where the number
would be.

### Caching and paths

One SQLite file, `data/cache/cache.sqlite`, with two tables: `http` (zlib-compressed ЕИС responses) and
`rzn` (registry answers). `app/maintenance.py` is the single place describing them to the user and
clearing them; add new tables there too. `Cache._init` drops the legacy `llm` and `marks` tables left
over from older caches.

`maintenance.autocompact()` keeps the cache bounded without asking: it runs after every finished run
(background thread), does nothing under `MI_CACHE_MAX_MB` (400), and above it drops expired rows and
then the oldest `http` rows until the file would fall to 80% of the limit, then VACUUMs. `rzn` rows are
never dropped by size — they are tiny and slow to refetch. The UI has one «Очистить кэш» button that
shows the size and what is lost before clearing everything.

`app/config.py` decides where `data/` and `out/` live: next to the exe when frozen (next to the source
tree otherwise), falling back to `%LOCALAPPDATA%\Медизделия ЕИС` when that location is not writable —
so an exe in Program Files or on a network share still works.

Finished reports stay in `out/` as `.xlsx` only; there is no editable snapshot and no in-app editing.
`data/reports.json` is a small index (note, row count) used to label the «Последние отчёты» list.

## Conventions

- All user-facing text — CLI output, web UI, `Problem` messages, Excel headers, `stats` dict keys — is
  **Russian**, and `stats`/summary keys are read by name in `run.py`, `api.py` and `app/web/index.html`.
  Renaming a key is a cross-file change. Code identifiers stay Latin, log messages Russian.
- Failures are collected, never raised to the user: append `Problem(ref, stage, message)` to
  `RunResult.problems` with a stage name that already exists (`поиск`, `загрузка`, `разбор`, `РЗН`,
  `отбор`, `вход`) and let the run finish. Messages are written for a non-technical operator and
  usually say what to do next.
- Everything tunable is an `MI_*` environment variable read once in `app/config.py` (rate limits,
  concurrency, cache TTL, `MI_RZN_ENABLED`, `MI_KIND_SLICE`). Add new knobs there rather than
  hardcoding.
- Modules start with `from __future__ import annotations`, use `@dataclass(slots=True)` for data
  carriers, and keep blocking SQLite work off the loop with `asyncio.to_thread` under an
  `asyncio.Lock`. Heavy or optional imports are done inside the function that needs them.
- Windows is the primary platform: entry points reconfigure stdout/stderr to UTF-8, and paths come from
  `app/config.py` (`DATA`, `CACHE`, `OUT`), which creates the directories on import. Note that ports
  8123+ can be reserved by Windows on some machines — the web launcher scans 20 ports and then falls
  back to any free one.
- Progress reporting is one callback signature everywhere: `progress(stage, text, done, total)` — the
  CLI renders a bar, the web pushes it over SSE and ticks off the step list in `index.html`
  (`STEP_NAMES` there must match the stage names used in `pipeline.py`).
