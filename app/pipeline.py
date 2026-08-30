
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .config import settings, DEFAULT_STAGES
from .eis.client import EisClient
from .eis.documents import (fetch_contract_xml, fetch_print_form,
                            is_amended)
from .eis.html_parser import parse_print_form
from .eis.nmck import fetch_nmck_many
from .eis.search import search_ktru
from .eis.xml_parser import parse_contract_xml
from .enrich.textutil import articles, is_type_word
from .enrich.nameparse import (COUNTRY_RE, DESCRIPTIVE_RE, ERUL_RE,
                               extract_ru_numbers, extract_tu,
                               is_opf_only, is_plain_word, looks_like_mark,
                               parse_name, pick_main_ru, quoted_marks,
                               variants_in_registry_name)
from .enrich.verify import (OPF_TAIL, clean_company, company_key,
                            company_keys, is_company_name, is_initialism)
from .enrich.rzn import RznEnricher
from .models import ContractMeta, Position, Problem, Row, RunResult

log = logging.getLogger(__name__)

Progress = Callable[[str, str, int, int], None]


def _noop(*_a, **_k) -> None:
    pass


@dataclass(slots=True)
class SearchParams:
    ktru: list[str] = field(default_factory=list)
    date_from: str = "01.01.2025"
    date_to: str = ""
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    limit_per_ktru: int = 0


async def run_online(params: SearchParams, progress: Progress = _noop) -> RunResult:
    result = RunResult()
    wanted = {k for k in params.ktru if k}
    if not wanted:
        result.problems.append(Problem("—", "вход", "не задан ни один код КТРУ"))
        return result

    t0 = time.time()
    metas: dict[str, ContractMeta] = {}
    totals: dict[str, int] = {}

    async with EisClient() as client:
        progress("поиск", "поиск контрактов в ЕИС", 0, len(wanted))
        done_k = 0
        for code in params.ktru:
            try:
                found, total = await search_ktru(
                    client, code,
                    date_from=params.date_from, date_to=params.date_to,
                    stages=params.stages, limit=params.limit_per_ktru,
                )
                totals[code] = total
                for m in found:
                    metas.setdefault(m.reestr_number, m)
            except Exception as e:
                result.problems.append(Problem(code, "поиск", f"{type(e).__name__}: {e}"))
                log.warning("поиск по КТРУ %s: %s", code, e)
            done_k += 1
            progress("поиск", f"КТРУ {code}: найдено {totals.get(code, 0)}",
                     done_k, len(wanted))

        if not metas:
            result.stats = {"контрактов": 0, "по КТРУ": totals}
            return result

        items = list(metas.values())
        progress("контракты", "загрузка контрактов", 0, len(items))
        done = 0
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(settings.eis_concurrency)
        parsed: list[tuple[ContractMeta, list[Position]]] = []

        async def one(meta: ContractMeta):
            nonlocal done
            async with sem:
                out = await _load_contract(client, meta, result)
            async with lock:
                done += 1
                progress("контракты", f"{done} из {len(items)}", done, len(items))
            return out

        for meta, chunk in zip(items, await asyncio.gather(
                *(one(m) for m in items), return_exceptions=True)):
            if isinstance(chunk, BaseException):
                result.problems.append(Problem(
                    meta.reestr_number, "загрузка",
                    f"{type(chunk).__name__}: {chunk}"))
                log.warning("контракт %s: %s", meta.reestr_number, chunk)
            elif chunk:
                parsed.append(chunk)

        result.stats["разбор контрактов"] = _source_stats(parsed)
        await _fill_nmck(client, [m for m, _ in parsed], result, progress)

    rows = _collect_rows(parsed, wanted, result)
    await _enrich(rows, params, result, progress)

    result.rows = rows
    result.stats.update({
        "контрактов найдено": len(metas),
        "контрактов обработано": len(parsed),
        "позиций в отчёте": len(rows),
        "по КТРУ": totals,
        "секунд": round(time.time() - t0, 1),
    })
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
                  wanted: set[str], result: RunResult) -> list[Row]:
    rows: list[Row] = []
    skipped = 0
    no_match = 0
    no_code = 0
    by_name: list[tuple[str, str]] = []
    only_matching = bool(wanted)
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
        for p in take:
            if not p.ru_number:
                src = p.ru_name or p.name
                nums = extract_ru_numbers(src)
                if nums:
                    p.ru_number = pick_main_ru(src, nums, _type_hints(p))
            rows.append(Row(meta=meta, pos=p))
    if skipped:
        result.stats["позиций отфильтровано"] = skipped
    if no_code:
        result.stats["позиций без кода КТРУ"] = no_code
    if by_name:
        result.stats["взято по совпадению наименования"] = len(by_name)
        for reestr, name in by_name:
            result.problems.append(Problem(
                reestr, "отбор",
                f"позиция «{name}» взята в отчёт по дословному совпадению "
                f"наименования: заказчик не заполнил код КТРУ"))
    if no_match:
        result.stats["контрактов без искомого КТРУ"] = no_match
    return rows


def _type_hints(pos: Position) -> list[str]:

    return [h for h in (pos.ktru_name, pos.nkmi_name) if h]


def _parse_names(rows: list[Row]) -> None:
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

        if parts.ru_numbers:
            p.ru_numbers_all = "; ".join(parts.ru_numbers)
            if not p.ru_number:
                p.ru_number = pick_main_ru(src, parts.ru_numbers, hints)
        p.tu_number = parts.tu_number or extract_tu(p.name) or extract_tu(p.trademark)
        if parts.erul and not p.erul:
            p.erul = parts.erul

        p.mark = parts.full
        p.mark_source = "правила" if p.mark else ""
        if p.trademark and not p.mark and not _is_spec_sheet(p.trademark):
            tm = parse_name(p.trademark, hints)
            got = "" if is_opf_only(tm.full) else tm.full
            p.mark = got or (p.trademark
                             if looks_like_mark(p.trademark, type_hints=hints) else "")
            p.mark_source = "товарный знак" if p.mark else ""
        p.raw_medical_block = " | ".join(
            x for x in (p.name, p.ru_name, p.trademark) if x)


async def _enrich(rows: list[Row], params: SearchParams, result: RunResult,
                  progress: Progress) -> None:

    if not rows:
        return

    _parse_names(rows)

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
    _drop_without_ru(rows, result)


def _drop_without_ru(rows: list[Row], result: RunResult) -> None:
    """Без номера РУ производитель и держатель ничем не подтверждены —
    в отчёт они не идут, чтобы догадка не выглядела как факт."""

    dropped = 0
    for r in rows:
        p = r.pos
        if p.ru_number:
            continue
        if p.manufacturer or p.declarant or p.declarant_inn:
            dropped += 1
        p.manufacturer = ""
        p.declarant = ""
        p.declarant_inn = ""
        p.manufacturer_source = ""
        p.confidence = "low"
    if dropped:
        result.stats["снято без подтверждения по РУ"] = dropped


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


def other_registration(ru_of_mark: dict, src: str, dst: str) -> bool:

    a, b = ru_of_mark.get(src) or set(), ru_of_mark.get(dst) or set()
    return bool(a and b and not (a & b))


def _apply_company_map(rows: list[Row], comp_map: dict[str, str]) -> None:
    for r in rows:
        for field in ("manufacturer", "declarant"):
            val = getattr(r.pos, field)
            if val and val in comp_map:
                setattr(r.pos, field, comp_map[val])


def _merge_by_common_mark(rows: list[Row], result: RunResult) -> None:

    from collections import Counter, defaultdict

    from .enrich.verify import phonetic_close

    marks: dict[str, set[str]] = defaultdict(set)
    counts: Counter = Counter()
    for r in rows:
        m = r.pos.manufacturer
        if not m:
            continue
        counts[m] += 1
        if r.pos.mark:
            marks[m].add(_mark_key(r.pos.mark))

    names = sorted(counts, key=lambda n: -counts[n])
    canon: dict[str, str] = {}
    for i, a in enumerate(names):
        if a in canon:
            continue
        for b in names[i + 1:]:
            if b in canon:
                continue
            if company_key(a)[1] != company_key(b)[1]:
                continue
            if not (marks[a] & marks[b]):
                continue
            if phonetic_close(a, b):
                canon[b] = a
    if not canon:
        return
    _apply_company_map(rows, canon)
    result.stats["сведено по общей марке"] = len(canon)


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


def _number_source_ok(source: str) -> bool:
    return source.startswith("реестр РЗН") and "по виду" not in source


def _transfer_by_number(rows: list[Row], result: RunResult) -> None:

    donors: dict[tuple[str, str, str], list[Position]] = {}
    for r in rows:
        p = r.pos
        if not p.manufacturer or not _number_source_ok(p.manufacturer_source):
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
                if _number_source_ok(p.manufacturer_source):
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
                p.kind_evidence = "совпало: " + ", ".join(m.tokens[:6])
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
        p.manufacturer_source = source or ("реестр РЗН (по № ТУ)" if rec.match == "tu"
                                           else "реестр РЗН")
        p.confidence = {"noRu": "high", "tu": "high", "name": "medium",
                        "mark": "medium"}.get(rec.match, "medium" if source else "low")
    eng = _mark_key(rec.producer_eng)
    if eng and p.mark and len(p.mark.split()) >= 3 and _mark_key(p.mark) in eng:
        p.mark = ""
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


