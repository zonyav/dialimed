
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from . import timing
from .config import settings, DEFAULT_STAGES, STAGES
from .eis.client import TOTALS as EIS_TOTALS, EisClient
from .eis.documents import (fetch_contract_xml, fetch_print_form,
                            is_amended)
from .eis.html_parser import parse_print_form
from .eis.nmck import fetch_nmck_many
from .eis.search import search_ktru, search_text
from .eis.xml_parser import parse_contract_xml
from .enrich.textutil import articles, is_measure, is_type_word
from .enrich.nameparse import (COUNTRY_RE, DESCRIPTIVE_RE, ERUL_RE,
                               extract_ru_numbers, extract_tu,
                               is_opf_only, is_plain_word, looks_like_mark,
                               parse_name, pick_main_ru, quoted_marks,
                               variants_in_registry_name)
from .enrich.verify import (OPF_TAIL, clean_company, company_key,
                            company_keys, is_company_name, is_initialism,
                            same_company)
from .enrich.rzn import RznEnricher
from .models import ContractMeta, Position, Problem, Row, RunResult

log = logging.getLogger(__name__)

Progress = Callable[[str, str, int, int], None]


def _noop(*_a, **_k) -> None:
    pass


@dataclass(slots=True)
class SearchParams:
    """Запрос: чем искать контракты и что оставить в отчёте.

    Три поля задают поиск и они же служат фильтром. Заполненные поля
    пересекаются как «что оставить»: код КТРУ плюс бренд — это позиции этого
    кода с этим брендом, а один бренд — все его позиции, под какими бы кодами
    они ни лежали.

    А вот «где искать» решает `only_codes`, и по умолчанию код задаёт область:
    если код назван, ЕИС спрашивается только кодами, а бренд и номер РУ
    работают отбором внутри. Так человек и рассуждает — «все стоматологические
    установки, из них AJ15», — и так находится то, что словом не находится
    вовсе: модель, записанная только в наименовании по РУ. Снятая галочка
    возвращает объединение: ЕИС спрашивается ещё и словами, и в отчёт попадают
    позиции под другими кодами и вовсе без кода (в разобранном прогоне по
    «Ajax AJ15» таких две из двадцати)."""

    ktru: list[str] = field(default_factory=list)
    date_from: str = "01.01.2025"
    date_to: str = ""
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    limit_per_ktru: int = 0
    use_ai: bool = False
    ru: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    sweep_codes: bool = False
    only_codes: bool = True
    drop_services: bool = True

    @property
    def empty(self) -> bool:
        return not (self.ktru or self.ru or self.text)

    @property
    def inside_codes(self) -> bool:
        """Ищем ли мы только внутри кодов. Без кодов вопрос не стоит."""

        return bool(self.ktru) and self.only_codes


def describe(params: SearchParams) -> str:
    """Одной строкой: что искали. Подпись прогона в отчёте и в списке.

    Строка попадает и в CLI, и в веб — держим её в одном месте, иначе
    два списка отчётов начинают выглядеть по-разному.
    """

    parts = []
    if params.ktru:
        parts.append("КТРУ: " + ", ".join(params.ktru))
    if params.ru:
        parts.append("№ РУ: " + ", ".join(params.ru))
    if params.text:
        parts.append("бренд или модель: " + "; ".join(params.text))
    if not parts:
        parts.append("КТРУ: —")
    if params.ktru and (params.ru or params.text):
        parts.append("только внутри кодов" if params.only_codes
                     else "и словами по всему ЕИС")
    if not params.drop_services:
        parts.append("с обслуживанием и ремонтом")
    return ("; ".join(parts) + f"; период с {params.date_from}"
            f"{' по ' + params.date_to if params.date_to else ''}; "
            f"стадии: {', '.join(STAGES.get(s, s) for s in params.stages)}"
            f"{'; с добором по кодам' if params.sweep_codes else ''}"
            f"{'; с поиском через ИИ' if params.use_ai else ''}")


@dataclass(slots=True)
class _Query:
    """Один запрос к ЕИС: чем спрашиваем и как это назвать пользователю."""

    kind: str          # «КТРУ», «№ РУ» или «бренд»
    value: str         # что уходит в ЕИС
    label: str = ""    # что показать в сводке (для написаний — исходное слово)

    @property
    def title(self) -> str:
        """Как назвать запрос в сводке: что спросили и ради чего.

        Спрашиваем словом («рускан»), а ищет человек модель («рускан 70п») —
        в статистике должно стоять и то, и другое, иначе непонятно, почему
        контрактов больше, чем строк в отчёте."""

        if self.label and self.label.lower() != self.value.lower():
            return f"{self.kind} «{self.value}» для «{self.label}»"
        return f"{self.kind} «{self.value}»"


def plan_queries(params: SearchParams) -> list[_Query]:
    """Из запроса пользователя — список запросов к ЕИС.

    Бренд спрашивается несколькими написаниями (`spelling.spellings`): поиск
    ЕИС ищет буквальное вхождение, и «ESTEN» не находит «ЭСТЕН». Номер РУ
    пишется одинаково всегда, ему варианты не нужны.
    """

    from .spelling import search_words, spellings

    out: list[_Query] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, label: str = "") -> None:
        value = (value or "").strip()
        key = (kind, value.lower())
        if value and key not in seen:
            seen.add(key)
            out.append(_Query(kind=kind, value=value, label=label))

    for code in params.ktru:
        add("КТРУ", code)
    if params.inside_codes:
        # код назван и он задаёт область: спрашивать ЕИС ещё и словами незачем,
        # бренд с номером остаются отбором внутри кодов
        return out
    for number in params.ru:
        add("№ РУ", number)
    for phrase in params.text:
        for variant in spellings(phrase):
            for word in search_words(variant):
                add("бренд", word, label=phrase)
    return out


async def _run_query(client: EisClient, q: _Query, params: SearchParams):
    if q.kind == "КТРУ":
        return await search_ktru(client, q.value, date_from=params.date_from,
                                 date_to=params.date_to, stages=params.stages,
                                 limit=params.limit_per_ktru)
    return await search_text(client, q.value, date_from=params.date_from,
                             date_to=params.date_to, stages=params.stages,
                             limit=params.limit_per_ktru)


async def run_online(params: SearchParams, progress: Progress = _noop) -> RunResult:
    fetched_at_start = dict(EIS_TOTALS)
    result = RunResult()
    wanted = {k for k in params.ktru if k}
    if params.empty:
        result.problems.append(Problem(
            "—", "вход", "не задано ни одного условия: нужен код КТРУ или НКМИ, "
                         "номер РУ либо название бренда или модели"))
        return result

    t0 = time.time()
    metas: dict[str, ContractMeta] = {}
    totals: dict[str, int] = {}
    by_query: dict[str, int] = {}
    queries = plan_queries(params)

    async with EisClient() as client:
        progress("поиск", "поиск контрактов в ЕИС", 0, len(queries))
        for done_k, q in enumerate(queries, 1):
            try:
                found, total = await _run_query(client, q, params)
                if q.kind == "КТРУ":
                    totals[q.value] = total
                else:
                    by_query[q.title] = total
                for m in found:
                    metas.setdefault(m.reestr_number, m)
                # молча недобрать — худшее, что может сделать поиск: отчёт
                # выглядит полным и врёт. Страниц у ЕИС берётся ограниченное
                # число, и если запрос шире, об этом надо сказать вслух
                if not params.limit_per_ktru and found and total > len(found) + 2:
                    result.problems.append(Problem(
                        q.value, "поиск",
                        f"по запросу «{q.value}» в ЕИС {total} контрактов, "
                        f"а взято {len(found)}: за один запрос программа "
                        "просматривает ограниченное число страниц. Сузьте "
                        "период или задайте условие точнее — иначе часть "
                        "контрактов в отчёт не попала"))
            except Exception as e:
                result.problems.append(Problem(q.value, "поиск", f"{type(e).__name__}: {e}"))
                log.warning("поиск %s %s: %s", q.kind, q.value, e)
            progress("поиск", f"{q.kind} {q.value}: найдено "
                              f"{totals.get(q.value, by_query.get(q.title, 0))}",
                     done_k, len(queries))

        if not metas:
            result.stats = {"контрактов": 0, "по КТРУ": totals}
            if by_query:
                result.stats["по запросу"] = by_query
            return result

        items = list(metas.values())
        parsed = await _load_many(client, items, result, progress, "контракты")
        result.stats["разбор контрактов"] = _source_stats(parsed)
        await _fill_nmck(client, [m for m, _ in parsed], result, progress)

        rows = _collect_rows(parsed, wanted, result,
                             ru=params.ru, text=params.text,
                             drop_services=params.drop_services)
        if params.sweep_codes:
            rows += await _sweep_by_codes(client, rows, params, result,
                                          progress, set(metas))

    if params.inside_codes and (params.text or params.ru):
        result.problems.append(Problem(
            "—", "отбор",
            "искали только внутри заданных кодов: позиции под другими кодами "
            "и без кода КТРУ в отчёт не попали. Чтобы добрать и их, снимите "
            "галочку «искать только внутри кодов» — тогда ЕИС спросят ещё и "
            "словами, но там находится лишь то, что написано в наименовании "
            "объекта закупки или в товарном знаке"))
    _note_services(rows, result, dropped=params.drop_services)
    await _enrich(rows, result, progress, use_ai=params.use_ai,
                  text=params.text)

    result.rows = rows
    result.stats.update({
        "контрактов найдено": len(metas),
        "контрактов обработано": len(parsed),
        "позиций в отчёте": len(rows),
        "по КТРУ": totals,
        "секунд": round(time.time() - t0, 1),
    })
    if by_query:
        result.stats["по запросу"] = by_query

    # запоминаем темп: сколько секунд ушло на контракт. Прогон, целиком
    # взятый из кэша, для оценки не годится — он всегда быстрый.
    downloaded = EIS_TOTALS["misses"] - fetched_at_start["misses"]
    from_cache = EIS_TOTALS["hits"] - fetched_at_start["hits"]
    asked = downloaded + from_cache
    timing.record(len(parsed), time.time() - t0,
                  downloaded / asked if asked else 1.0)
    return result


async def _load_contract(client: EisClient, meta: ContractMeta,
                         result: RunResult) -> Optional[tuple[ContractMeta, list[Position]]]:
    data, note = await fetch_contract_xml(client, meta.reestr_number)
    if data is not None:
        try:
            m, poss = parse_contract_xml(data, meta)
            if poss:
                m.amended = is_amended(note)
                return m, poss
            note = "в XML нет позиций объекта закупки"
        except Exception as e:
            note = f"XML не разобран ({type(e).__name__}: {e})"

    body, note2 = await fetch_print_form(client, meta.reestr_number)
    if body is None:
        result.problems.append(Problem(meta.reestr_number, "загрузка", f"{note}; {note2}"))
        return None
    try:
        html = body.decode("utf-8", errors="replace")
        m2, poss2 = parse_print_form(html, source_ref=meta.url)
        m2.reestr_number = meta.reestr_number
        m2.conclusion_date = meta.conclusion_date or m2.conclusion_date
        m2.stage = meta.stage or m2.stage
        m2.customer = meta.customer or m2.customer
        m2.contract_price = meta.contract_price if meta.contract_price is not None \
            else m2.contract_price
        m2.source = "eis-html"
        if not poss2:
            result.problems.append(
                Problem(meta.reestr_number, "разбор", f"{note}; в печатной форме нет позиций"))
            return None
        return m2, poss2
    except Exception as e:
        result.problems.append(
            Problem(meta.reestr_number, "разбор", f"{note}; ПФ: {type(e).__name__}: {e}"))
        return None


async def _load_many(client: EisClient, items: list[ContractMeta],
                     result: RunResult, progress: Progress,
                     stage: str) -> list[tuple[ContractMeta, list[Position]]]:
    """Скачать и разобрать пачку контрактов, не роняя прогон на одном плохом."""

    done = 0
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(settings.eis_concurrency)
    out: list[tuple[ContractMeta, list[Position]]] = []
    progress(stage, "загрузка контрактов", 0, len(items))

    async def one(meta: ContractMeta):
        nonlocal done
        async with sem:
            got = await _load_contract(client, meta, result)
        async with lock:
            done += 1
            progress(stage, f"{done} из {len(items)}", done, len(items))
        return got

    for meta, chunk in zip(items, await asyncio.gather(
            *(one(m) for m in items), return_exceptions=True)):
        if isinstance(chunk, BaseException):
            result.problems.append(Problem(
                meta.reestr_number, "загрузка",
                f"{type(chunk).__name__}: {chunk}"))
            log.warning("контракт %s: %s", meta.reestr_number, chunk)
        elif chunk:
            out.append(chunk)
    return out


# Коды ищем с этой даты, как бы узко ни был заказан отчёт: код КТРУ у изделия
# не меняется от года, а вот в одном отдельно взятом году поставок может не
# оказаться вовсе. Разбираем не больше SWEEP_DISCOVER контрактов — коды
# повторяются, и десятого контракта обычно хватает.
SWEEP_FROM = "01.01.2025"
SWEEP_DISCOVER = 60


def _earlier(a: str, b: str) -> bool:
    """Дата «ДД.ММ.ГГГГ» строго раньше другой такой же."""

    def key(s: str) -> tuple:
        parts = (s or "").split(".")
        return tuple(int(x) for x in reversed(parts)) if len(parts) == 3 else ()

    ka, kb = key(a), key(b)
    return bool(ka and kb and ka < kb)


async def _codes_from_earlier(client: EisClient, params: SearchParams,
                              result: RunResult, progress: Progress,
                              seen: set[str]) -> list[str]:
    """Коды КТРУ из контрактов, которые лежат раньше заказанного периода.

    Строки оттуда в отчёт не попадают: нужны только коды, чтобы было что
    обходить. Отбор тот же, что и в отчёте, — код чужого изделия из того же
    контракта не возьмётся."""

    if not params.text or not _earlier(SWEEP_FROM, params.date_from):
        return []

    plan = [q for q in plan_queries(SearchParams(
        text=params.text, ru=params.ru, stages=params.stages))
        if q.kind != "КТРУ"]
    metas: dict[str, ContractMeta] = {}
    for q in plan:
        try:
            found, _total = await search_text(
                client, q.value, date_from=SWEEP_FROM, date_to=params.date_from,
                stages=params.stages, limit=0)
        except Exception as e:
            result.problems.append(Problem(q.value, "поиск",
                                           f"{type(e).__name__}: {e}"))
            continue
        for m in found:
            if m.reestr_number not in seen:
                metas.setdefault(m.reestr_number, m)
    if not metas:
        return []

    # поставки вперёд обслуживания: объект закупки написан прямо на странице
    # поиска, и у ремонта кода КТРУ обычно нет вовсе
    items = sorted(metas.values(),
                   key=lambda m: (_SERVICE_RE.match(m.first_object or "") is not None,
                                  -(m.conclusion_date.toordinal()
                                    if m.conclusion_date else 0)))[:SWEEP_DISCOVER]
    progress("добор", f"ищу коды КТРУ в контрактах с {SWEEP_FROM}: "
                      f"{len(items)} из {len(metas)}", 0, len(items))
    scratch = RunResult()
    parsed = await _load_many(client, items, scratch, progress, "добор")
    got = _collect_rows(parsed, set(), scratch, ru=params.ru, text=params.text,
                        drop_services=True)
    codes = sorted({r.pos.ktru for r in got if r.pos.ktru})
    result.stats["коды из контрактов раньше периода"] = {
        "контрактов посмотрено": len(items), "кодов найдено": len(codes),
        "с даты": SWEEP_FROM}
    return codes


async def _sweep_by_codes(client: EisClient, rows: list[Row],
                          params: SearchParams, result: RunResult,
                          progress: Progress,
                          seen: set[str]) -> list[Row]:
    """Второй заход: по кодам КТРУ, которые нашлись в первой волне.

    Поиск словом упирается в то, что индексирует ЕИС. Замерено: из ста
    контрактов кода 26.60.12.132-00000036, которых поиск по «рускан» не вернул,
    два всё-таки про РуСкан 70П — модель там записана только в «наименовании по
    РУ», а его ЕИС не ищет, и товарный знак пуст. Достать такие можно одним
    способом: взять код целиком и отфильтровать самим.

    Это дорого — у кода бывают тысячи контрактов, — поэтому только по просьбе
    («Дособрать по кодам»), и каждый код называется вслух вместе с числом
    контрактов, чтобы прогон можно было остановить.

    Откуда берутся сами коды — отдельный вопрос, и узкий период на нём
    спотыкается. Замерено на «МАИА-01» за 2026 год: семь контрактов, все до
    одного — обслуживание и ремонт, и ни в одном нет кода КТРУ, так что
    добирать было нечего. Те же слова с 01.01.2025 дают пятнадцать контрактов,
    среди них настоящие поставки с кодами 32.50.21.121-00000119 и -00000102.
    Поэтому коды ищутся за весь период с `SWEEP_FROM`, даже когда отчёт
    заказан за один год: разбираются только первые `SWEEP_DISCOVER` контрактов
    (коды повторяются быстро), строки из них в отчёт не идут — берутся
    исключительно коды.
    """

    from collections import Counter

    codes = [c for c, _n in Counter(
        r.pos.ktru for r in rows if r.pos.ktru).most_common()
        if c not in set(params.ktru)]
    codes += [c for c in await _codes_from_earlier(client, params, result,
                                                   progress, seen)
              if c not in codes and c not in set(params.ktru)]
    if not codes:
        return []

    extra: dict[str, ContractMeta] = {}
    for i, code in enumerate(codes, 1):
        try:
            found, total = await search_ktru(
                client, code, date_from=params.date_from, date_to=params.date_to,
                stages=params.stages, limit=params.limit_per_ktru)
        except Exception as e:
            result.problems.append(Problem(code, "поиск", f"{type(e).__name__}: {e}"))
            continue
        fresh = [m for m in found if m.reestr_number not in seen]
        for m in fresh:
            extra.setdefault(m.reestr_number, m)
        progress("добор", f"КТРУ {code}: {total} контрактов, новых {len(fresh)}",
                 i, len(codes))
    if not extra:
        result.stats["добор по кодам"] = {"кодов": len(codes), "контрактов": 0,
                                          "строк": 0}
        return []

    items = list(extra.values())
    parsed = await _load_many(client, items, result, progress, "добор")
    await _fill_nmck(client, [m for m, _ in parsed], result, progress)
    more = _collect_rows(parsed, set(), result, ru=params.ru, text=params.text,
                         drop_services=params.drop_services)
    result.stats["добор по кодам"] = {"кодов": len(codes),
                                      "контрактов": len(items),
                                      "строк": len(more)}
    return more


def _source_stats(parsed: list[tuple[ContractMeta, list[Position]]]) -> dict:

    poss = [p for _m, ps in parsed for p in ps]
    n = len(poss)
    if not n:
        return {"позиций": 0}

    def pct(field: str) -> str:
        got = sum(1 for p in poss if getattr(p, field))
        return f"{got} ({100 * got / n:.0f}%)"

    return {
        "контрактов из XML": sum(1 for m, _ps in parsed if m.source == "eis-xml"),
        "контрактов из печатной формы": sum(1 for m, _ps in parsed
                                            if m.source != "eis-xml"),
        "позиций": n,
        "с кодом КТРУ": pct("ktru"),
        "с кодом НКМИ": pct("nkmi_code"),
        "с наименованием по РУ": pct("ru_name"),
        "с товарным знаком": pct("trademark"),
        "с ценой": pct("price"),
        "количество не определено контрактом": sum(
            1 for _m, ps in parsed for p in ps if p.quantity_undefined),
    }


def _name_key(s: str) -> str:

    return " ".join(re.findall(r"[a-zа-яё0-9]+", (s or "").lower().replace("ё", "е")))


def _collect_rows(parsed: Iterable[tuple[ContractMeta, list[Position]]],
                  wanted: set[str], result: RunResult,
                  ru: Iterable[str] = (), text: Iterable[str] = (),
                  drop_services: bool = False) -> list[Row]:
    """Отбор позиций под запрос.

    Условия пересекаются: код КТРУ, номер РУ и бренд, если заполнены, должны
    сойтись все. Внутри одного поля строки складываются — два бренда значат
    «или», иначе двумя названиями сразу искать было бы нельзя.

    Сверяемся с тем, что попадёт в отчёт: текст позиции и характеристики — оба
    видны в файле, так что любую строку можно объяснить. Характеристики нужны:
    на прогоне по «рускан 70п» они добавили 4 строки из 228, где модель названа
    только там. Невидимой строки для сверки нет ни одной.

    Бренд с моделью сверяет `app.query`: он различает, где написан артикул, и
    не требует имени рядом с приметным артикулом — «AJ15» без слова «Ajax»
    написан в десяти контрактах из десяти, которые старый отбор терял."""

    from .spelling import any_match
    from .query import judge_any

    ru = [x for x in ru if str(x).strip()]
    text = [x for x in text if str(x).strip()]
    rows: list[Row] = []
    skipped = 0
    no_match = 0
    no_code = 0
    off_query = 0
    services = 0
    by_name: list[tuple[str, str]] = []
    only_matching = bool(wanted)

    def wanted_here(p: Position) -> bool:
        if not (ru or text):
            return True
        if ru and not any_match(ru, p.contract_text() + " " + (p.specs_text or "")):
            return False
        if text:
            # то, что заказчик выбрал, и то, как названа регистрация, — разные
            # вещи: в первом модель написана прямо, во втором бывает перечень
            # всех исполнений сразу
            chosen = " ".join(x for x in (p.name, p.ktru_name, p.trademark,
                                          p.mark, p.specs_text) if x)
            family = " ".join(x for x in (p.ru_name, p.ru_variants) if x)
            v = judge_any(text, chosen, family)
            if not v.ok:
                return False
            p.match_note = v.note
            p.match_doubt = v.doubt
        return True
    for meta, poss in parsed:
        matched = [p for p in poss if p.matches_ktru(wanted)] if wanted else list(poss)
        if only_matching:
            take = matched
            skipped += len(poss) - len(matched)
            no_code += sum(1 for p in poss if not p.ktru)
            if matched:
                names = {_name_key(p.name) for p in matched if p.name}
                names |= {_name_key(p.ktru_name) for p in matched if p.ktru_name}
                names.discard("")
                for p in poss:
                    if p.ktru or _name_key(p.name) not in names:
                        continue
                    take.append(p)
                    by_name.append((meta.reestr_number, p.name))
        else:
            take = list(poss)
        if wanted and not matched:
            no_match += 1
        if ru or text:
            fits = [p for p in take if wanted_here(p)]
            off_query += len(take) - len(fits)
            take = fits
        if drop_services:
            # обслуживание, ремонт и поверка приходят вместе с поставками —
            # модель в них названа честно, но цена там за работу, а не за
            # изделие. Убираем до обогащения: незачем искать производителя
            # для строки, которой в отчёте не будет
            keep = []
            for p in take:
                if _looks_like_service(p):
                    services += 1
                else:
                    keep.append(p)
            take = keep
        for p in take:
            if not p.ru_number:
                src = p.ru_name or p.name
                nums = extract_ru_numbers(src)
                if nums:
                    p.ru_number = pick_main_ru(src, nums, _type_hints(p))
            rows.append(Row(meta=meta, pos=p))
    # счётчики накапливаются: добор по кодам зовёт отбор второй раз, и
    # перезапись показала бы только вторую волну
    def count(key: str, n: int) -> None:
        if n:
            result.stats[key] = result.stats.get(key, 0) + n

    count("позиций отфильтровано", skipped)
    count("позиций без кода КТРУ", no_code)
    if by_name:
        result.stats["взято по совпадению наименования"] = len(by_name)
        for reestr, name in by_name:
            result.problems.append(Problem(
                reestr, "отбор",
                f"позиция «{name}» взята в отчёт по дословному совпадению "
                f"наименования: заказчик не заполнил код КТРУ"))
    count("контрактов без искомого КТРУ", no_match)
    count("позиций мимо запроса", off_query)
    count("строк обслуживания и ремонта исключено", services)
    count("модель только в перечне исполнений РУ",
          sum(1 for r in rows if r.pos.match_doubt == "исполнение"))
    count("бренд в контракте не назван",
          sum(1 for r in rows if r.pos.match_doubt == "бренд"))
    return rows


# Поиск по модели приводит не только поставки: половина контрактов с моделью
# в тексте — это её обслуживание и ремонт. Строки честные, но цена в них не
# про цену изделия, и сводка обязана сказать это вслух, иначе средний чек
# по модели окажется ценой годового сервиса.
_SERVICE_RE = re.compile(
    r"^\s*(?:оказание\s+услуг|услуг[аи]?\b|техническо[ем]\s+обслуживани|"
    r"обслуживани|ремонт|контроль\s+технического|поверк|калибровк|монтаж|"
    r"пусконаладк|демонтаж|утилизац|аренд|поставка\s+запасных|"
    # «Текущий ремонт:: Аппарат …» — ремонт, названный прилагательным вперёд:
    # на прогоне по «МАИА-01» такая строка единственная прошла как поставка
    r"(?:текущ|капитальн|планов|внепланов|аварийн|срочн)\w*\s+ремонт)", re.I)


def _looks_like_service(pos: Position) -> bool:
    return bool(_SERVICE_RE.match(pos.name or ""))


def _note_services(rows: list[Row], result: RunResult,
                   dropped: bool = False) -> None:

    if dropped:
        n = result.stats.get("строк обслуживания и ремонта исключено", 0)
        if not n:
            return
        result.problems.append(Problem(
            "—", "отбор",
            f"{n} строк — обслуживание, ремонт, поверка или калибровка — в "
            f"отчёт не попали: цена там за работу, а не за изделие. Если "
            f"нужны и они, снимите галочку «только поставки»"
            + (". Поставок по запросу не нашлось вовсе — за этот период "
               "изделие только обслуживали" if not rows else "")))
        return
    n = sum(1 for r in rows if _looks_like_service(r.pos))
    if not n or not rows:
        return
    result.stats["строк про услуги, а не поставку"] = n
    result.problems.append(Problem(
        "—", "отбор",
        f"{n} строк из {len(rows)} — это обслуживание, ремонт или поверка, "
        "а не поставка изделия. Модель в них названа верно, но цена в таких "
        "строках — цена работы, и в средние цены изделия её брать нельзя"))


def _type_hints(pos: Position) -> list[str]:

    return [h for h in (pos.ktru_name, pos.nkmi_name) if h]


def _parse_names(rows: list[Row], result: RunResult | None = None) -> None:

    from collections import Counter

    dropped: Counter = Counter()
    for r in rows:
        p = r.pos
        hints = _type_hints(p)
        if p.ru_number:
            own = extract_ru_numbers(p.ru_number)
            if own:
                p.ru_number = own[0]
            elif len(p.ru_number.split()) > 4 or not any(c.isdigit() for c in p.ru_number):
                if not p.ru_name:
                    p.ru_name = p.ru_number
                p.ru_number = ""
        src = p.ru_name or p.name
        parts = parse_name(src, hints)

        if parts.ru_numbers and not p.ru_number:
            p.ru_number = pick_main_ru(src, parts.ru_numbers, hints)
        p.tu_number = parts.tu_number or extract_tu(p.name) or extract_tu(p.trademark)
        if parts.erul and not p.erul:
            p.erul = parts.erul

        p.mark = parts.full
        p.mark_source = "правила" if p.mark else ""
        # размер обозначением не считается: «Эндоскоп d=10мм (исп.3)» — это не
        # модель, а диаметр, и товарный знак рядом сказал бы куда больше
        if p.mark and is_measure(p.mark):
            dropped["это размер изделия, а не обозначение"] += 1
            p.mark = ""
            p.mark_source = ""
        if p.trademark and not p.mark and not _is_spec_sheet(p.trademark):
            tm = parse_name(p.trademark, hints)
            got = "" if is_opf_only(tm.full) else tm.full
            p.mark = got or (p.trademark
                             if looks_like_mark(p.trademark, type_hints=hints) else "")
            p.mark_source = "товарный знак" if p.mark else ""

    if result is not None and dropped:
        result.stats["обозначение снято правилами"] = dict(dropped.most_common())


async def _enrich(rows: list[Row], result: RunResult,
                  progress: Progress, use_ai: bool = False,
                  text: Iterable[str] = ()) -> None:

    if not rows:
        return

    _parse_names(rows, result)

    async with RznEnricher() as rzn:
        probe_ok = await rzn.available()
        await _enrich_from_registry(rows, rzn, progress)
        await _enrich_from_kind(rows, rzn, result, progress)
        _transfer_by_number(rows, result)
        result.stats["РЗН"] = dict(rzn.stats)
        found = rzn.stats["by_ru"] + rzn.stats["by_tu"] + rzn.stats["by_erul"]
        if not found and rzn.stats["errors"]:
            result.problems.append(
                Problem("—", "РЗН", "реестр Росздравнадзора не отвечает: "
                                    f"{rzn.stats['errors']} запросов с ошибкой, "
                                    "производителя определить не удалось. "
                                    "Стоит повторить прогон позже — данные из кэша "
                                    "сохранятся, и он пройдёт быстрее"))
        elif rzn.stats["errors"]:
            result.problems.append(
                Problem("—", "РЗН", "реестр Росздравнадзора отвечал с перебоями: "
                                    f"{rzn.stats['errors']} запросов с ошибкой, "
                                    f"производитель найден для {found} позиций. "
                                    "Если пустых ячеек больше обычного — повторите прогон"))
        elif not probe_ok:
            result.problems.append(
                Problem("—", "РЗН", "реестр Росздравнадзора отвечал с перебоями; "
                                    f"производитель найден для {found} позиций"))

    _refine_marks(rows, result)
    _mark_from_registry(rows, result)
    _tidy_marks(rows, result)
    _transfer_by_mark(rows, result)
    _refine_marks(rows, result)
    _extend_bare_marks(rows, result)
    _tidy_marks(rows, result)
    for r in rows:
        if not r.pos.manufacturer:
            r.pos.manufacturer_source = ""
            r.pos.confidence = r.pos.confidence or "low"
    await _enrich_by_firm(rows, result, progress)
    # ИИ спрашиваем последним: всё, что находится бесплатно, к этому моменту
    # уже найдено, и платить за эти строки незачем
    if use_ai:
        await _enrich_by_ai(rows, result, progress)
    _drop_holder_without_ru(rows, result)
    _confirm_brand(rows, list(text), result)
    if use_ai:
        rows[:] = await _ai_pick_variant(rows, list(text), result, progress)
    _note_registry_gaps(rows, result)


def _confirm_brand(rows: list[Row], text: list[str], result: RunResult) -> None:
    """Бренд, которого нет в контракте, но так зовут завод.

    Отбор идёт до реестра, поэтому строку, где написано «AJ15» и не написано
    «Ajax», он берёт с оговоркой. К этому моменту производитель уже известен —
    «Guangzhou Ajax Medical Equipment», — и оговорка снимается: в десяти
    потерянных контрактах из десяти именно так. Строку это не добавляет и не
    убирает, меняется только подпись в колонке «Совпадение с запросом»."""

    if not text or not rows:
        return
    from .query import judge_any

    n = 0
    for r in rows:
        p = r.pos
        if p.match_doubt != "бренд":
            continue
        known = " ".join(x for x in (p.manufacturer, p.declarant,
                                     p.ru_registry_name) if x)
        if not known:
            continue
        chosen = " ".join(x for x in (p.name, p.ktru_name, p.trademark,
                                      p.mark, p.specs_text) if x)
        family = " ".join(x for x in (p.ru_name, p.ru_variants) if x)
        v = judge_any(text, chosen, family, known)
        if v.ok and v.brand_known:
            p.match_note = v.note
            p.match_doubt = v.doubt
            n += 1
    if n:
        result.stats["бренд подтверждён производителем"] = n
        result.stats["бренд в контракте не назван"] = max(
            0, result.stats.get("бренд в контракте не назван", 0) - n)


# Сколько сомнительных строк отдавать модели за один прогон. Вопрос дешёвый —
# один запрос без поисков по реестру, — но и строк таких обычно единицы.
AI_MAX_VARIANTS = 60


async def _ai_pick_variant(rows: list[Row], text: list[str], result: RunResult,
                           progress: Progress) -> list[Row]:
    """Какое исполнение закуплено, когда правила этого не решают.

    Правила видят три случая: исполнение названо в выбранном заказчиком,
    названо соседнее, или в наименовании по РУ перечислены все сразу. Третий
    они разрешить не могут — «варианты исполнения: AJ11, AJ12, AJ15, AJ16,
    AJ18» и товарный знак «AJAX» не говорят, что поставлено. Здесь модель
    читает текст целиком и называет исполнение, если оно там всё-таки есть —
    «в комплектации AJ-15», «исп. 15», строка в характеристиках.

    Ни одного поля отчёта она не заполняет: ответ принимается, только если
    цитата дословно нашлась в тексте позиции и обозначение стоит в самой
    цитате. Что искал человек, модели не говорят — сравнивает программа."""

    from .config import ai_options
    from .enrich.aimatch import (AiAnswer, AiCache, Gateway, VARIANT_KIND,
                                 VariantAnswer, pick_variant, question,
                                 variant_supported)
    from .query import norm, parse

    doubtful = [r for r in rows if r.pos.match_doubt == "исполнение"]
    if not doubtful or not text:
        return rows
    opts = ai_options()
    if not opts["key"]:
        return rows

    wanted: set[str] = set()
    for phrase in text:
        wanted |= {a.key for a in parse(phrase).articles}
    if not wanted:
        return rows

    todo: dict[str, list[Position]] = {}
    for r in doubtful:
        todo.setdefault(question(r.pos.contract_text(), r.pos.specs_text),
                        []).append(r.pos)
    asked = list(todo)[:AI_MAX_VARIANTS]
    stats = {"сомнительных строк": len(doubtful), "спрошено": len(asked),
             "исполнение подтверждено": 0, "поставлено другое": 0,
             "модель промолчала": 0, "цитата не подтвердилась": 0,
             "ошибок шлюза": 0}

    progress("ИИ", "спрашиваю ИИ, какое исполнение закуплено", 0, len(asked))
    verdicts: dict[str, str] = {}
    async with Gateway(opts["key"], opts["model"], opts["base"]) as gw:
        if gw.problem:
            return rows
        cache = AiCache(settings.cache_db) if settings.cache_enabled else None
        try:
            async def one(q: str):
                if cache is not None:
                    hit = await cache.get(gw.model, q, VARIANT_KIND)
                    if hit is not None:
                        return q, VariantAnswer(variant=hit.ru_number,
                                                quote=hit.quote, why=hit.why)
                ans = await pick_variant(q, gw.ask)
                if cache is not None and not ans.error:
                    await cache.put(gw.model, q,
                                    AiAnswer(ru_number=ans.variant,
                                             quote=ans.quote, why=ans.why),
                                    VARIANT_KIND)
                return q, ans

            done = 0
            for coro in asyncio.as_completed([one(q) for q in asked]):
                q, ans = await coro
                done += 1
                progress("ИИ", f"{done} из {len(asked)}", done, len(asked))
                if ans.error:
                    stats["ошибок шлюза"] += 1
                    continue
                if not ans.answered:
                    stats["модель промолчала"] += 1
                    continue
                if not variant_supported(ans, q):
                    stats["цитата не подтвердилась"] += 1
                    continue
                key = norm(ans.variant)
                verdicts[q] = key
        finally:
            if cache is not None:
                cache.close()
        stats.update(gw.spent())

    drop: set[int] = set()
    for q, key in verdicts.items():
        ours = any(w == key or w in key or key in w for w in wanted)
        for p in todo.get(q, ()):
            if ours:
                p.match_doubt = ""
                p.match_note = (f"{p.match_note.split(':')[0]}: исполнение "
                                f"{_as_written(key, q)} названо в тексте (ИИ)")
                stats["исполнение подтверждено"] += 1
            else:
                drop.add(id(p))
                stats["поставлено другое"] += 1
    result.stats["ИИ: исполнение"] = stats
    if stats["поставлено другое"]:
        result.problems.append(Problem(
            "—", "отбор",
            f"{stats['поставлено другое']} строк убрано: в наименовании по РУ "
            "перечислены все исполнения регистрации, а в тексте контракта "
            "названо другое — не то, что искали. Проверено дословной цитатой "
            "из самого контракта"))
    if stats["исполнение подтверждено"]:
        result.problems.append(Problem(
            "—", "отбор",
            f"у {stats['исполнение подтверждено']} строк исполнение "
            "подтверждено по тексту контракта: оговорка «только в перечне "
            "исполнений РУ» с них снята"))
    return [r for r in rows if id(r.pos) not in drop]


def _as_written(key: str, question_text: str) -> str:
    """Как исполнение написано в самом контракте — для подписи в отчёте.

    В ответе модели оно может быть записано иначе («AJ-15»), а в колонке
    должно стоять то, что человек найдёт в тексте позиции."""

    from .query import norm

    for word in re.findall(r"[0-9A-Za-zА-Яа-яЁё-]+", question_text or ""):
        if norm(word) == key:
            return word
    return key.upper()


def _note_registry_gaps(rows: list[Row], result: RunResult) -> None:
    """Красные ячейки в отчёте — это не «в данных пусто», а отброшенная или
    ненайденная запись реестра. Сколько их и почему — оператор должен видеть
    в сводке, а не догадываться, глядя на строку с номером РУ."""

    rzn = result.stats.get("РЗН") or {}

    def positions(n: int) -> str:
        return "позиции" if n % 10 == 1 and n % 100 != 11 else "позициям"

    blind = sum(1 for r in rows if r.pos.ru_number and not r.pos.manufacturer)
    if blind:
        result.stats["РУ есть, производитель не найден"] = blind
        reasons = []
        if rzn.get("ambiguous"):
            reasons.append("под одним номером в реестре разные производители")
        if rzn.get("wrong_number"):
            reasons.append("реестр записывает номер иначе, чем контракт")
        if rzn.get("truncated"):
            reasons.append("реестр вернул неполный ответ")
        if rzn.get("errors"):
            reasons.append("реестр отвечал с ошибками")
        why = "; ".join(reasons) or ("записи с таким номером в реестре нет "
                                     "или в ней не назван производитель")
        result.problems.append(Problem(
            "—", "РЗН",
            f"по {blind} {positions(blind)} № РУ в контракте есть, но запись "
            f"реестра не взята: {why}. В отчёте эти ячейки окрашены жёлтым"))

    weak = sum(1 for r in rows
               if r.pos.manufacturer_source == "реестр РЗН (наименование не совпало)")
    if weak:
        result.stats["взято по номеру, наименование не совпало"] = weak
        result.problems.append(Problem(
            "—", "РЗН",
            f"по {weak} {positions(weak)} производитель взят по точному номеру РУ, "
            "хотя реестр называет изделие иначе, чем контракт — "
            "такие строки стоит просмотреть глазами"))


# Пороги самопроверки. Проверочные позиции берутся из самого прогона: там, где
# номер РУ написан в контракте, правильный ответ известен заранее — прячем номер
# и смотрим, попадёт ли модель. Правила ломались на новых кодах молча; здесь
# программа меряет себя на тех данных, которые пользователь считает сейчас.
# Проверка стоит столько же, сколько работа, поэтому проверочных задач берём
# не больше, чем самой работы. Тридцать, а не двадцать пять: считаются не
# задачи, а полученные ответы, и на живом прогоне двадцать пять задач дали
# девятнадцать ответов — порог не сработал, хотя должен был.
AI_MAX_CHECKS = 30
AI_ENOUGH_CHECKS = 20       # меньше — судить не о чем, но и запрещать не за что
AI_MIN_ACCURACY = 0.95
AI_CODE_CHECKS = 10         # по отдельному коду хватает и десяти проверок,
AI_CODE_ACCURACY = 0.90     # чтобы увидеть, что именно на нём ИИ не работает


def _ai_check_tasks(rows: list[Row], work: int = 0) -> dict[str, tuple[str, str]]:
    """Задачи с известным ответом: строки, где номер РУ написан в контракте и
    реестр по нему ответил. Берём поровну от каждого кода КТРУ — точность важно
    видеть по коду, а не в среднем по больнице."""

    from .enrich.aimatch import mask_number, question

    by_code: dict[str, dict[str, tuple[str, str]]] = {}
    for r in rows:
        p = r.pos
        if not (p.ru_number and p.manufacturer and p.from_contract_number):
            continue
        text = question(mask_number(p.contract_text(), p.ru_number),
                        mask_number(p.specs_text, p.ru_number))
        by_code.setdefault(p.ktru, {}).setdefault(text, (p.manufacturer, p.ktru))

    limit = min(AI_MAX_CHECKS, max(AI_ENOUGH_CHECKS, work)) if work else AI_MAX_CHECKS
    picked: dict[str, tuple[str, str]] = {}
    queues = [list(v.items()) for v in by_code.values()]
    while len(picked) < limit and any(queues):
        for q in queues:
            if not q or len(picked) >= limit:
                continue
            text, meta = q.pop()
            picked.setdefault(text, meta)
    return picked


async def _enrich_by_ai(rows: list[Row], result: RunResult,
                        progress: Progress) -> None:
    """Спрашивает модель о строках, оставшихся без производителя, и на том же
    прогоне проверяет её на строках, где ответ известен. Ни одно поле отчёта не
    заполняется словами модели: она называет номер, а данные берутся из реестра."""

    from .config import ai_options
    from .enrich.aimatch import (SOURCE, AiCache, Gateway, identify, question,
                                 quote_supported, record_text)

    opts = ai_options()
    if not opts["key"]:
        result.problems.append(Problem(
            "—", "ИИ", "поиск через ИИ включён, но ключ не задан. Откройте "
                       "«Поиск через ИИ» на странице и введите ключ — без него "
                       "прогон прошёл как обычно"))
        return

    # Спрашиваем только там, где есть за что зацепиться. Позиция из одних
    # общих слов КТРУ («Система электрохирургическая», и всё) не даёт модели
    # ничего для поиска: на живом прогоне 27 таких вопросов из 66 не принесли
    # ни одного ответа, а платить пришлось за каждый.
    todo: dict[str, list[Position]] = {}
    пропущено = 0
    for r in rows:
        p = r.pos
        if p.manufacturer:
            continue
        if not p.has_clue:
            пропущено += 1
            continue
        todo.setdefault(question(p.contract_text(), p.specs_text), []).append(p)
    if not todo:
        return
    checks = _ai_check_tasks(rows, len(todo))
    texts = list(dict.fromkeys(list(todo) + list(checks)))

    stats = {"спрошено позиций": len(texts), "из них проверочных": len(checks),
             "не о чем спрашивать": пропущено,
             "модель промолчала": 0, "цитата не подтвердилась": 0,
             "номер не подтверждён реестром": 0, "ошибок шлюза": 0,
             "заполнено строк": 0}
    answers: dict[str, object] = {}
    done = 0

    progress("ИИ", "спрашиваю ИИ о позициях без производителя", 0, len(texts))
    async with RznEnricher() as rzn:
        async with Gateway(opts["key"], opts["model"], opts["base"]) as gw:
            if gw.problem:
                result.problems.append(Problem(
                    "—", "ИИ", f"поиск через ИИ не запущен: {gw.problem} "
                               "Прогон прошёл как обычно"))
                return
            cache = AiCache(settings.cache_db) if settings.cache_enabled else None
            try:
                async def one(text: str):
                    if cache is not None:
                        hit = await cache.get(gw.model, text)
                        if hit is not None:
                            return text, hit
                    ans = await identify(text, gw.ask, rzn.search_by_name,
                                         rzn.firm_records)
                    if cache is not None and not ans.error:
                        await cache.put(gw.model, text, ans)
                    return text, ans

                for coro in asyncio.as_completed([one(t) for t in texts]):
                    text, ans = await coro
                    answers[text] = ans
                    done += 1
                    progress("ИИ", f"{done} из {len(texts)}", done, len(texts))
            finally:
                if cache is not None:
                    cache.close()

            # запись под названным номером — единственный источник данных;
            # слова модели дальше этой строки не идут
            records: dict[str, object] = {}
            last_error = ""
            for text, ans in answers.items():
                if ans.error:
                    stats["ошибок шлюза"] += 1
                    last_error = ans.error
                    continue
                if not ans.answered:
                    stats["модель промолчала"] += 1
                    continue
                rec = await rzn.confirm_number(ans.ru_number)
                if rec is None or not rec.producer:
                    stats["номер не подтверждён реестром"] += 1
                    continue
                if not quote_supported(ans.quote, record_text(rec)):
                    stats["цитата не подтвердилась"] += 1
                    continue
                records[text] = rec
            stats.update(gw.spent())

    # Точность считаем только по ответам, которые программа действительно
    # написала бы в отчёт. Молчание и отсев проверками ошибкой не считаются:
    # ячейка от них не появляется, а гнать модель к ответу любой ценой —
    # ровно то, чего мы от неё не хотим.
    checked: dict[str, list[int]] = {}
    silent = 0
    for text, (want, code) in checks.items():
        rec = records.get(text)
        seen = checked.setdefault(code, [0, 0])
        if rec is None:
            silent += 1
            continue
        seen[0] += 1
        if same_company(rec.producer, want):
            seen[1] += 1

    total = sum(v[0] for v in checked.values())
    right = sum(v[1] for v in checked.values())
    accuracy = right / total if total else None
    blocked = {code for code, (n, ok) in checked.items()
               if n >= AI_CODE_CHECKS and ok / n < AI_CODE_ACCURACY}
    stop_all = bool(total >= AI_ENOUGH_CHECKS and accuracy < AI_MIN_ACCURACY)

    for text, positions in todo.items():
        rec = records.get(text)
        if rec is None or stop_all:
            continue
        for p in positions:
            if p.ktru in blocked:
                continue
            _apply_registry(p, rec, source=SOURCE)
            stats["заполнено строк"] += 1

    if checks:
        stats["проверочных позиций"] = len(checks)
        stats["без ответа на проверке"] = silent
    if total:
        stats["проверок"] = total
        stats["из них верно"] = right
        stats["точность"] = f"{round(100 * accuracy)}%"
    result.stats["ИИ"] = stats
    _note_ai(result, stats, total, right, accuracy, blocked, stop_all, last_error)


def _note_ai(result: RunResult, stats: dict, total: int, right: int,
             accuracy: Optional[float], blocked: set[str], stop_all: bool,
             last_error: str = "") -> None:
    """Сводка про ИИ пишется всегда, даже когда он не дал ничего: молчащий
    ИИ и выключенный ИИ выглядят в отчёте одинаково, а это разные вещи."""

    from .enrich.aimatch import Gateway

    filled = stats["заполнено строк"]
    if stop_all:
        result.problems.append(Problem(
            "—", "ИИ", f"ИИ проверен на {total} позициях с известным ответом, "
                       f"верно {right} ({stats['точность']}) — это ниже порога, "
                       "поэтому его ответы в отчёт не пошли. Строки остались "
                       "пустыми, всё остальное в отчёте посчитано как обычно"))
    elif filled:
        checked = (f"; проверен на {total} позициях с известным ответом, "
                   f"точность {stats['точность']}" if total
                   else "; проверить его на этом прогоне было не на чем — "
                        "в контрактах почти нет позиций с номером РУ")
        result.problems.append(Problem(
            "—", "ИИ", f"производитель подобран ИИ для {filled} строк{checked}. "
                       "Эти ячейки в отчёте залиты жёлтым: номер РУ нашла "
                       "программа, а не прочитала в контракте"))
    elif stats["ошибок шлюза"] >= max(3, stats["спрошено позиций"] // 2):
        # причину знает шлюз, а не программа: пересказываем её словами,
        # по которым видно, что делать — ключ, деньги или прокси
        why = Gateway.explain(last_error)
        result.problems.append(Problem(
            "—", "ИИ", f"шлюз ИИ не отвечал ({stats['ошибок шлюза']} запросов "
                       f"с ошибкой) — прогон прошёл без него. {why}"))
    else:
        result.problems.append(Problem(
            "—", "ИИ", f"ИИ спросили о {stats['спрошено позиций']} позициях, "
                       "подтверждённых ответов нет — все строки остались "
                       "как были"))
    for code in sorted(blocked):
        result.problems.append(Problem(
            code, "ИИ", "на этом коде КТРУ ИИ ошибался на проверочных позициях — "
                        "его ответы по коду в отчёт не пошли"))


def _drop_holder_without_ru(rows: list[Row], result: RunResult) -> None:
    """Держатель РУ и его ИНН бывают только у конкретной регистрации: без
    номера подтвердить их нечем, и в отчёт они не идут. Сам производитель
    остаётся — он вычислен по обозначению из строки, где номер был, и пустая
    ячейка вместо него полезнее не делает. Дальше по отчёту такая догадка не
    расходится: донором переноса служат только строки с номером."""

    kept = dropped = 0
    for r in rows:
        p = r.pos
        if p.ru_number:
            continue
        if p.declarant or p.declarant_inn:
            dropped += 1
        p.declarant = ""
        p.declarant_inn = ""
        p.rzn_id = ""
        if p.manufacturer:
            kept += 1
    if kept:
        result.stats["производитель без номера РУ"] = kept
    if dropped:
        result.stats["держатель снят без номера РУ"] = dropped


def _extends(short: str, long: str) -> bool:

    if len(long) <= len(short) or not long.lower().startswith(short.lower()):
        return False
    return not long[len(short)].isalpha()


def _addition_listed(short: str, long: str, listed: str) -> bool:

    if not listed:
        return False
    norm = _mark_key(listed)
    if not norm:
        return False
    short_keys = {_mark_key(w) for w in (short or "").split()}
    added = [_mark_key(w) for w in (long or "").split()
             if _mark_key(w) and _mark_key(w) not in short_keys]
    if not added:
        return _mark_key(long) in norm
    return all(k in norm for k in added)


def _extend_bare_marks(rows: list[Row], result: RunResult) -> None:

    from collections import defaultdict


    by_ru: dict[str, set[str]] = defaultdict(set)
    listed_ru: set[str] = set()
    known_variants: dict[str, str] = {}
    for r in rows:
        if r.pos.ru_number and len(_registry_variants(r.pos)) > 1:
            listed_ru.add(r.pos.ru_number)
        if r.pos.ru_number:
            listed = "; ".join(_registry_variants(r.pos))
            if listed:
                known_variants[r.pos.ru_number] = (
                    known_variants.get(r.pos.ru_number, "") + "; " + listed)
        if r.pos.ru_number and r.pos.mark:
            by_ru[r.pos.ru_number].add(r.pos.mark)

    full_of: dict[tuple[str, str], str] = {}
    for ru, marks in by_ru.items():
        if ru in listed_ru:
            continue
        for short in marks:
            if articles(short):
                continue
            longer = [m for m in marks if _extends(short, m)]
            if len(longer) == 1 and _addition_listed(short, longer[0],
                                                     known_variants.get(ru, "")):
                full_of[(ru, short)] = longer[0]

    if not full_of:
        return
    changed = 0
    for r in rows:
        p = r.pos
        full = full_of.get((p.ru_number, p.mark))
        if full:
            p.mark = full
            p.mark_source = (p.mark_source + " + достроено по № РУ").strip(" +")
            changed += 1
    result.stats["обозначений достроено по № РУ"] = changed


async def _fill_nmck(client: EisClient, metas: list[ContractMeta],
                     result: RunResult, progress: Progress) -> None:

    todo = [m for m in metas if m.purchase_number and m.nmck is None]
    if not todo:
        return
    progress("НМЦК", "начальные цены", 0, len(todo))
    found = await fetch_nmck_many(
        client, [m.purchase_number for m in todo],
        concurrency=settings.eis_concurrency,
        progress=lambda d, t: progress("НМЦК", f"{d} из {t}", d, t))
    for m in todo:
        got = found.get(m.purchase_number)
        if got is None:
            continue
        m.nmck = got.value
        m.nmck_note = got.note
    with_price = sum(1 for m in todo if m.nmck is not None)
    with_drop = sum(1 for m in todo if (m.discount_pct or 0) > 0)
    result.stats["НМЦК"] = {
        "запрошено": len(todo), "найдено": with_price,
        "со снижением": with_drop,
        "совместных закупок": sum(1 for m in todo if m.nmck_note),
        "цена изменена доп. соглашением": sum(1 for m in metas if m.amended),
    }


def _company_head_words(name: str) -> set[str]:

    out: set[str] = set()
    for chunk in [re.sub(r"\([^)]*\)", " ", name or "")] + re.findall(r"\(([^)]+)\)", name or ""):
        for w in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", chunk):
            if company_key(w)[1]:
                continue
            if w.lower() in OPF_TAIL:
                continue
            out.add(_mark_key(w))
            break
    out.discard("")
    return out


def _cut_company_tail(mark: str, names: list[str]) -> str:

    heads: set[str] = set()
    for n in names:
        heads |= _company_head_words(n)
    if not heads or not mark:
        return mark
    cut = len(mark)
    for m in re.finditer(r"[A-Za-zА-Яа-яЁё][\w]*", mark):
        if m.start() and _mark_key(m.group(0)) in heads:
            cut = min(cut, m.start())
    if cut >= len(mark):
        return mark
    return mark[:cut].strip(" -–,;\"'«»")


def _refine_marks(rows: list[Row], result: RunResult) -> None:

    fixed = 0
    for r in rows:
        p = r.pos
        names = [x for x in (p.manufacturer, p.declarant) if x]
        if not names or not (p.ru_name or p.name) or not p.mark:
            continue
        keys = set()
        for x in names:
            keys |= company_keys(x)
        mark_key = company_key(p.mark)[0]
        if not (mark_key in keys or any(k and k in mark_key for k in keys)):
            continue
        def clean_of_company(value: str) -> str:
            v = _cut_company_tail(value, names)
            core = company_key(v)[0]
            if not v or core in keys or any(k in core for k in keys):
                return ""
            return v if looks_like_mark(v) else ""

        cut = clean_of_company(p.mark)
        if not cut:
            for source in (p.ru_name or p.name, p.trademark, p.ru_registry_name):
                if not source:
                    continue
                again = parse_name(source, _type_hints(p), exclude=names)
                cut = clean_of_company(again.full)
                if cut:
                    break
        if not cut:
            cut = _brand_named_after_firm(p)
        if cut != p.mark:
            p.mark = cut
            p.mark_source = "правила (после реестра)" if cut else p.mark_source
            fixed += 1
    if fixed:
        result.stats["обозначение уточнено по производителю"] = fixed + int(
            result.stats.get("обозначение уточнено по производителю", 0))


def _brand_named_after_firm(p: Position) -> str:

    from .enrich.verify import ABBREV

    core = company_key(p.manufacturer)[0] if p.manufacturer else ""
    if not core:
        return ""
    for source in (p.ru_name, p.trademark, p.ru_registry_name, p.name):
        for q in quoted_marks(source or ""):
            q = q.strip()
            if not q or len(q.split()) != 1 or is_plain_word(q):
                continue
            if any(part.lower() in ABBREV for part in re.split(r"[-\s]", q)):
                continue
            if is_company_name(q, q) or company_key(q)[0] != core:
                continue
            return q
    return ""


_ACCESSORY_RE = re.compile(
    r"(?:пара|комплект|набор\s+электрод|электрод|манжет|датчик|кабел|зонд|"
    r"насадк|держател|штатив|сумк|чехол|адаптер|шланг|тележк|принадлежност)",
    re.I)

_MODEL_LIST_RE = re.compile(r"модел[ияей]{1,2}\s+[^,;.]{1,24},", re.I)
_ARTICLE_RE = re.compile(r"\b[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё]*-?\d[\w-]*")
_TU_TAIL_RE = re.compile(r"\s*(?:по\s+)?(?:[A-ZА-Я.\d]+\s*)?\b(?:ТУ|ГОСТ)\b.*$",
                         re.I | re.S)


def _registry_lists_several(name: str) -> bool:

    if _MODEL_LIST_RE.search(name or ""):
        return True
    head = _TU_TAIL_RE.sub("", name or "")
    quoted = {q.strip().lower() for q in quoted_marks(head) if q.strip()}
    if len(quoted) > 1:
        return True
    arts = {a.strip().lower() for a in _ARTICLE_RE.findall(head)}
    return len(arts) > 1


def _mark_from_registry(rows: list[Row], result: RunResult) -> None:

    filled = 0
    skipped_family = 0
    for r in rows:
        p = r.pos
        if p.mark or not p.ru_registry_name:
            continue
        if not p.manufacturer_source.startswith("реестр"):
            continue
        if _registry_lists_several(p.ru_registry_name):
            skipped_family += 1
            continue
        exclude = [x for x in (p.manufacturer, p.declarant) if x]
        got = parse_name(p.ru_registry_name, _type_hints(p), exclude=exclude).full
        if is_plain_word(got) or not got:
            quoted = [q for q in quoted_marks(p.ru_registry_name)
                      if q and not is_company_name(q, q)]
            if quoted:
                got = quoted[0]
        if got and not is_plain_word(got) and not is_company_name(got, got):
            p.mark = got
            p.mark_source = "реестр РЗН (наименование)"
            filled += 1
    if filled:
        result.stats["обозначение из наименования реестра"] = filled
    if skipped_family:
        result.stats["обозначение не взято: реестр называет несколько"] = \
            skipped_family


def _tidy_marks(rows: list[Row], result: RunResult | None = None) -> None:

    from collections import Counter, defaultdict

    dropped: Counter = Counter()
    samples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    manuf_variants: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in rows:
        for field in ("manufacturer", "declarant"):
            val = getattr(r.pos, field)
            if val:
                clean = clean_company(val)
                setattr(r.pos, field, clean)
                manuf_variants[company_key(clean)][clean] += 1
    manuf_canon = _merge_company_groups(manuf_variants)
    for r in rows:
        for field in ("manufacturer", "declarant"):
            val = getattr(r.pos, field)
            if val:
                setattr(r.pos, field, manuf_canon.get(company_key(val), val))

    for r in rows:
        p = r.pos
        if not p.mark:
            continue
        def drop(why: str, _p=p, _r=r) -> None:
            dropped[why] += 1
            if len(samples[why]) < 40:
                samples[why].append((_r.meta.reestr_number, _p.mark))
            _p.mark = ""
            _p.mark_source = ""

        if is_company_name(p.mark, p.mark) or (
                p.manufacturer and _mark_key(p.mark) == _mark_key(p.manufacturer)):
            if p.manufacturer_source == "перенос по обозначению":
                p.manufacturer_source = "имя завода в тексте контракта"
            drop("это название завода, а не обозначение")
            continue
        p.mark = _tidy_one_mark(_cut_tail_junk(
            p.mark, {v.lower() for v in _registry_variants(p)}))
        if not p.mark:
            drop("после вычистки названия прибора не осталось ничего")
            continue
        if DESCRIPTIVE_RE.match(p.mark) or _NOISE_MARK.match(p.mark):
            drop("это описание изделия или канцелярия, а не марка")
            continue
        if is_measure(p.mark):
            drop("это размер изделия, а не обозначение")
            continue
        if COUNTRY_RE.match(p.mark.strip(" .,\"'«»")):
            drop("это страна происхождения")
            continue
        if p.mark.count(",") >= 2 or p.mark.count("(") != p.mark.count(")"):
            drop("перечень через запятую или оборванная скобка")

    if result is not None and dropped:
        agg = Counter(result.stats.get("обозначение снято правилами") or {})
        agg.update(dropped)
        result.stats["обозначение снято правилами"] = dict(agg.most_common())
        for why, items in samples.items():
            for reestr, value in items:
                result.problems.append(Problem(
                    reestr, "обозначение",
                    f"обозначение «{value}» снято: {why}"))

    variants: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.pos.mark:
            variants[_mark_key(r.pos.mark)][r.pos.mark] += 1
    canon = {k: c.most_common(1)[0][0] for k, c in variants.items()}
    for r in rows:
        if r.pos.mark:
            r.pos.mark = canon.get(_mark_key(r.pos.mark), r.pos.mark)
    _canon_mark_case(rows)


def _letters(word: str) -> tuple[int, int]:
    lat = sum(1 for ch in word if "a" <= ch.lower() <= "z")
    cyr = sum(1 for ch in word if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    return lat, cyr


def _mixed_script(word: str) -> int:
    return min(_letters(word))


def _latin_share(word: str) -> int:
    lat, cyr = _letters(word)
    return int(lat > cyr)


def _canon_mark_case(rows: list[Row]) -> None:

    from collections import Counter, defaultdict

    from .enrich.rzn import latinize

    spellings: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for word in (r.pos.mark or "").split():
            spellings[latinize(word).lower()][word] += 1
    best = {k: max(c.items(), key=lambda x: (x[1], -_mixed_script(x[0]),
                                             _latin_share(x[0]),
                                             -sum(ch.isupper() for ch in x[0])))[0]
            for k, c in spellings.items()}
    for r in rows:
        if not r.pos.mark:
            continue
        fixed = " ".join(best.get(latinize(w).lower(), w) for w in r.pos.mark.split())
        if fixed != r.pos.mark:
            r.pos.mark = fixed


_NOISE_MARK = re.compile(r"^\d+(?:[.,]\d+)?\s+[А-ЯЁа-яё]{4,}|^(?:поз|стр|№)\b", re.I)

_SPEC_SHEET = re.compile(r"[<>]\s*=|\bне\s+(?:менее|более)\b|\bда\b\s*[А-ЯЁ]", re.I)


def _is_spec_sheet(text: str) -> bool:
    s = text or ""
    return bool(_SPEC_SHEET.search(s)) or (s.count(":") >= 3 and len(s) > 120)

_DEVICE_WORD = re.compile(
    r"^(?:(?=[А-ЯЁа-яё]{6,}$)[А-ЯЁа-яё]+(?:тор|ник|льник|арат|ема|граф|метр|скоп|стика)|"
    r"комплекс|прибор|система|устройство|блок|лампа|облучатель|"
    r"(?=[А-ЯЁа-яё]{7,}$)[А-ЯЁа-яё]+(?:ая|ый|ое|ой|ые|ий))$", re.I)


_TAIL_JUNK = re.compile(
    r"\b(?:вариант\w*|исполнени\w*|модел[ьия]\w*|сери[ияй])\b|"
    r"(?<![\w-])[IVX]{1,4}\.(?![\w-])", re.I)


_TAIL_ROMAN = re.compile(r"\s+[IVX]{1,4}\.?$")


def _cut_tail_junk(mark: str, known_variants: set[str] = frozenset()) -> str:

    mark = (mark or "").strip()
    m = _TAIL_JUNK.search(mark)
    if m and m.start():
        mark = mark[:m.start()].strip(" -–.,;:\"'«»")
    while True:
        m = _TAIL_ROMAN.search(mark)
        if not m or m.group(0).strip(" .").lower() in known_variants:
            return mark
        mark = mark[:m.start()].strip()


def _registry_variants(pos: Position) -> list[str]:

    out = [v.strip() for v in (pos.ru_variants or "").split(";") if v.strip()]
    named = variants_in_registry_name(pos.ru_registry_name)
    return named if len(named) > len(out) else out


def _tidy_one_mark(mark: str) -> str:

    mark = re.sub(r"^(?:\d{1,2}\s*[.)]\s*|[1-9]\s+)(?=[^\W\d])", "",
                  (mark or "").strip())
    words = mark.split()
    if not words:
        return mark
    half = len(words) // 2
    if len(words) % 2 == 0 and [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
        words = words[:half]
    words = [w for i, w in enumerate(words)
             if i == 0 or w.lower() != words[i - 1].lower() or w.isdigit()]
    while len(words) > 1 and _DEVICE_WORD.match(words[0]):
        rest = " ".join(words[1:])
        if not looks_like_mark(rest) or len(rest) > 40:
            break
        words = words[1:]
    return " ".join(words)


def _mark_key(s: str) -> str:
    from .enrich.rzn import latinize
    return re.sub(r"[^a-zа-я0-9]+", "", latinize(s or "").lower().replace("ё", "е"))


def _merge_company_groups(variants: dict[tuple[str, str], "Counter"]) -> dict[tuple[str, str], str]:

    from collections import Counter, defaultdict

    by_core: dict[str, dict[str, Counter]] = defaultdict(dict)
    for (core, opf), c in variants.items():
        by_core[core][opf] = c
    canon: dict[tuple[str, str], str] = {}
    for core, groups in by_core.items():
        blank = groups.pop("", None)
        host = max(groups, key=lambda o: sum(groups[o].values())) if groups else ""
        if blank is not None:
            if groups:
                groups[host] = groups[host] + blank
            else:
                groups[""] = blank
        for opf, c in groups.items():
            canon[(core, opf)] = c.most_common(1)[0][0]
        if blank is not None and host:
            canon[(core, "")] = canon[(core, host)]

    for short_key, short_name in list(canon.items()):
        for full_key, full_name in canon.items():
            if short_key[1] == full_key[1] and is_initialism(short_name, full_name):
                canon[short_key] = full_name
                break
    return canon


def _mark_keys(mark: str, hints: Iterable[str] = ()) -> list[str]:

    out: list[str] = []
    whole = _mark_key(mark or "")
    if len(whole) >= 4:
        out.append(whole)
    words = (mark or "").split()
    if len(words) > 1 and not is_type_word(words[0], hints):
        head = _mark_key(words[0])
        if len(head) >= 4 and head not in out:
            out.append(head)
    for w in words[1:] if len(words) > 1 else []:
        k = _mark_key(w)
        if (len(k) >= 4 and k not in out
                and any(c.isdigit() for c in k)
                and sum(c.isalpha() for c in k) >= 2):
            out.append(k)
    return out


def _transfer_by_number(rows: list[Row], result: RunResult) -> None:

    donors: dict[tuple[str, str, str], list[Position]] = {}
    for r in rows:
        p = r.pos
        if not p.manufacturer or not p.from_contract_number:
            continue
        for num in (p.ru_number, p.erul, p.tu_number):
            if not num:
                continue
            for kind in (p.nkmi_code, p.ktru):
                if kind:
                    donors.setdefault((num, kind, ""), []).append(p)

    moved = 0
    for r in rows:
        p = r.pos
        if p.manufacturer:
            continue
        for num in (p.ru_number, p.erul, p.tu_number):
            if not num:
                continue
            group = [d for kind in (p.nkmi_code, p.ktru) if kind
                     for d in donors.get((num, kind, ""), [])]
            if not group:
                continue
            if len({company_key(d.manufacturer)[0] for d in group}) != 1:
                continue
            src = group[0]
            p.manufacturer = src.manufacturer
            p.manufacturer_source = "реестр РЗН (номер из соседней строки)"
            p.confidence = "medium"
            for field in ("declarant", "declarant_inn", "ru_registry_name",
                          "ru_variants", "ru_status"):
                if not getattr(p, field) and getattr(src, field):
                    setattr(p, field, getattr(src, field))
            if not p.ru_number and src.ru_number:
                p.ru_number = src.ru_number
            moved += 1
            break
    if moved:
        result.stats["перенос по номеру"] = moved


def _transfer_by_mark(rows: list[Row], result: RunResult) -> None:

    from collections import Counter, defaultdict

    def has_key(p: Position) -> bool:
        return bool(p.ru_number or p.tu_number or p.erul)

    known: dict[str, dict[tuple, Counter]] = defaultdict(lambda: defaultdict(Counter))
    variants: dict[str, str] = {}
    numbered: set[str] = set()
    declarant: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for r in rows:
        p = r.pos
        if not (p.mark and p.manufacturer and has_key(p)
                and p.manufacturer_source.startswith("реестр")):
            continue
        for i, key in enumerate(_mark_keys(p.mark, _type_hints(p))):
            known[key][company_key(p.manufacturer)][p.manufacturer] += 1
            if p.declarant:
                declarant[key][company_key(p.declarant)[0]][p.declarant] += 1
            if i == 0:
                listed = "; ".join(x for x in (p.ru_variants, p.ru_registry_name) if x)
                if listed:
                    variants[key] = (variants.get(key, "") + " " + listed).strip()
                if p.from_contract_number:
                    numbered.add(key)

    moved = conflicts = dec_refused = 0
    for r in rows:
        p = r.pos
        if p.manufacturer or not p.mark:
            continue
        for i, key in enumerate(_mark_keys(p.mark, _type_hints(p))):
            group = known.get(key)
            if not group:
                continue
            if len(group) > 1:
                conflicts += 1
                break
            if i == 0:
                listed = variants.get(key, "")
                if (listed and key not in numbered
                        and not _listed_in_registry(p.mark, key, listed)):
                    break
            spellings = next(iter(group.values()))
            p.manufacturer = spellings.most_common(1)[0][0]
            p.manufacturer_source = "перенос по обозначению"
            p.confidence = "medium"
            if not p.declarant:
                dgroup = declarant.get(key)
                if dgroup and len(dgroup) == 1:
                    p.declarant = next(iter(dgroup.values())).most_common(1)[0][0]
                elif dgroup:
                    dec_refused += 1
            moved += 1
            break

    if moved or conflicts:
        result.stats["перенос по обозначению"] = {
            "строк закрыто": moved,
            "отказов из-за спорной марки": conflicts,
            "отказов по держателю РУ": dec_refused,
        }


def _listed_in_registry(mark: str, key: str, listed: str) -> bool:

    norm = _mark_key(listed)
    if key in norm:
        return True
    for w in (mark or "").split():
        k = _mark_key(w)
        if len(k) >= 4 and k in norm:
            return True
    return False


async def _enrich_by_firm(rows: list[Row], result: RunResult,
                          progress: Progress) -> None:
    """Ищет изделие по заводу, а не по наименованию.

    Срез по виду промахивается там, где изделие зарегистрировано под чужим
    видом: цистоскоп Bissinger описан внутри «Резектоскоп биполярный
    PLASMALOOP», и ни в срезе «Цистоскоп жесткий», ни в поиске по
    наименованию его нет. Зато реестр умеет отбирать по названию завода — а
    название завода в контракте написано, это и есть товарный знак.

    Одного совпадения имени завода мало: оно говорит «где-то у этой фирмы», а
    в отчёт пойдут номер РУ, держатель и ИНН конкретной регистрации. Поэтому
    запись принимается только тогда, когда артикул из контракта нашёлся в её
    перечне исполнений (`Match.strong`) — то же доказательство, по которому
    отбирается запись в срезе по виду.
    """

    from collections import defaultdict
    from .enrich.kindmatch import build_index, match_rules

    if not settings.rzn_enabled:
        return
    todo: dict[str, list[Position]] = defaultdict(list)
    spelling: dict[str, str] = {}
    for r in rows:
        p = r.pos
        if p.manufacturer:
            continue
        for brand in _brand_tokens(p):
            key = brand.lower()
            spelling.setdefault(key, brand)
            todo[key].append(p)
    if not todo:
        return

    stats = {"знаков спрошено": len(todo), "заводов найдено": 0,
             "строк закрыто": 0}
    progress("завод", "ищу производителя по товарному знаку", 0, len(todo))
    async with RznEnricher() as rzn:
        if not rzn.enabled:
            return
        for i, (key, positions) in enumerate(sorted(todo.items()), 1):
            brand = spelling[key]
            found = await rzn.firm_records(brand)
            progress("завод", f"{brand}: {len(found.items)} регистраций",
                     i, len(todo))
            if not found.items:
                continue
            stats["заводов найдено"] += 1
            idx = build_index([RznEnricher._record(it, "firm", 1.0)
                               for it in found.items])
            for p in positions:
                if p.manufacturer:
                    continue
                m = match_rules(idx, p.mark, p.trademark, p.ru_name)
                if m.ok and m.strong > 0:
                    _apply_registry(p, m.record, source="реестр РЗН (по заводу)")
                    stats["строк закрыто"] += 1
    result.stats["поиск по заводу"] = stats


def _brand_tokens(pos: Position) -> list[str]:
    """Слова позиции, которые могут оказаться названием завода.

    Берём только приметные — латиницу, кириллицу заглавными, слово в
    кавычках: заводов с названием «электрод» или «наблюдения» не бывает, а
    каждый лишний знак — это запрос к реестру."""

    from .enrich.rzn import strong_tokens

    hints = _type_hints(pos)
    out: list[str] = []
    for text in (pos.trademark, pos.mark):
        for word in strong_tokens(text or ""):
            if len(word) < 4 or not word.isalpha():
                continue
            if is_type_word(word, hints) or is_measure(word) or is_opf_only(word):
                continue
            if word.lower() in {w.lower() for w in out}:
                continue
            out.append(word)
    return out[:2]


async def _enrich_from_kind(rows: list[Row], rzn: RznEnricher,
                            result: RunResult, progress: Progress) -> None:

    from collections import defaultdict
    from .enrich import kindreg
    from .enrich.kindmatch import build_index, match_rules

    if not settings.kind_slice_enabled or not rzn.enabled:
        return
    todo = [r for r in rows if not r.pos.manufacturer and r.pos.nkmi_code]
    if not todo:
        return

    by_code: dict[str, list[Row]] = defaultdict(list)
    for r in todo:
        by_code[r.pos.nkmi_code].append(r)

    stats = {"видов": 0, "записей в срезах": 0, "совпало по правилам": 0,
             "видов не взято": 0}
    progress("вид", "скачиваю регистрации по коду вида", 0, len(by_code))
    for i, (code, group) in enumerate(sorted(by_code.items()), 1):
        sl = await kindreg.fetch(code, rzn)
        progress("вид", f"{sl.name[:40] or code}: {sl.total} регистраций",
                 i, len(by_code))
        if not sl.ok:
            stats["видов не взято"] += 1
            continue
        stats["видов"] += 1
        stats["записей в срезах"] += len(sl.records)
        idx = build_index(sl.records, sl.name, code)
        for r in group:
            p = r.pos
            m = match_rules(idx, p.mark, p.trademark, p.ru_name)
            if m.ok:
                _apply_registry(p, m.record, source="реестр РЗН (по виду)")
                stats["совпало по правилам"] += 1

    result.stats["срез по виду"] = stats


async def _enrich_from_registry(rows: list[Row], rzn: RznEnricher,
                                progress: Progress) -> None:

    queries = [(r.pos.ru_number, r.pos.ru_name or r.pos.name, tuple(_type_hints(r.pos)),
                r.pos.tu_number, r.pos.erul)
               for r in rows]
    progress("РЗН", "поиск в реестре медизделий", 0, len(queries))
    recs = await rzn.lookup_many(
        queries, progress=lambda d, t: progress("РЗН", f"{d} из {t}", d, t))

    for r, rec in zip(rows, recs):
        if rec is not None:
            _apply_registry(r.pos, rec)


def _apply_registry(p: Position, rec, source: str = "") -> None:
    if rec.match == "name":
        return
    if rec.producer:
        p.manufacturer = clean_company(rec.producer)
        if source:
            p.manufacturer_source = source
        elif rec.match == "tu":
            p.manufacturer_source = "реестр РЗН (по № ТУ)"
        elif rec.name_mismatch:
            p.manufacturer_source = "реестр РЗН (наименование не совпало)"
        else:
            p.manufacturer_source = "реестр РЗН"
        p.confidence = {"noRu": "high", "tu": "high", "name": "medium",
                        "mark": "medium"}.get(rec.match, "medium" if source else "low")
        if rec.name_mismatch:
            p.confidence = "medium"
    eng = _mark_key(rec.producer_eng)
    if eng and p.mark and len(p.mark.split()) >= 3 and _mark_key(p.mark) in eng:
        p.mark = ""
    if getattr(rec, "rzn_id", ""):
        p.rzn_id = rec.rzn_id
    if rec.declarant:
        p.declarant = rec.declarant
    if rec.declarant_inn:
        p.declarant_inn = rec.declarant_inn
    if not p.ru_number and rec.ru_number:
        p.ru_number = (f"ЕРУЛ {rec.ru_number}"
                       if ERUL_RE.fullmatch(rec.ru_number) else rec.ru_number)
    if not p.erul and rec.erul:
        p.erul = rec.erul
    if rec.status:
        p.ru_status = rec.status
    if rec.ru_name:
        p.ru_registry_name = rec.ru_name
    if rec.variants:
        p.ru_variants = "; ".join(rec.variants)
    if not p.mark and len(rec.variants) == 1 \
            and not _ACCESSORY_RE.match(rec.variants[0].strip(" \"'«»")):
        p.mark = rec.variants[0]
        p.mark_source = "реестр РЗН"


