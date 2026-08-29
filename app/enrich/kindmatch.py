

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rzn import (RznRecord, _norm, latinize, producer_key, similarity,
                  strong_tokens)

MAX_DOC_FREQ = 0.30

MIN_SCORE = 3.0

FIRM_MATCH = 0.80
FIRM_SAME_INN = 0.76

_HAS_DIGIT = re.compile(r"\d")


@dataclass(slots=True)
class _Score:

    total: float
    strong: float
    weak: float
    hits: list[str]
    i: int


@dataclass(slots=True)
class Match:
    record: RznRecord | None = None
    score: float = 0.0
    tokens: list[str] = field(default_factory=list)
    reason: str = ""
    rivals: int = 0

    @property
    def ok(self) -> bool:
        return self.record is not None


@dataclass(slots=True)
class SliceIndex:

    records: list[RznRecord] = field(default_factory=list)
    dev: list[str] = field(default_factory=list)
    firm: list[str] = field(default_factory=list)
    doc_freq: dict[str, int] = field(default_factory=dict)
    kind_words: set[str] = field(default_factory=set)

    def __len__(self) -> int:
        return len(self.records)

    def haystack(self, i: int) -> str:
        return self.dev[i] + self.firm[i]


def _flat(s: str) -> str:
    return " " + re.sub(r"[^a-zа-я0-9]+", " ", latinize(_norm(s)).lower()).strip() + " "


def _token_key(t: str) -> str:

    return _flat(t).strip()


_BARE_NUMBER = re.compile(r"^\d{1,4}$")


def build_index(records: list[RznRecord], kind_name: str = "",
                kind_code: str = "") -> SliceIndex:

    idx = SliceIndex(records=list(records),
                     kind_words={w for w in re.findall(r"[а-яa-z]{4,}", _norm(kind_name))})
    if kind_code:
        idx.kind_words.add(_token_key(kind_code))
    for r in idx.records:
        idx.dev.append(_flat(" ".join((r.ru_name, r.models_description))))
        idx.firm.append(_flat(" ".join((r.producer, r.producer_eng, r.declarant))))
    for hay in idx.dev:
        for word in set(hay.split()):
            idx.doc_freq[word] = idx.doc_freq.get(word, 0) + 1
    return idx


def _doc_freq(idx: SliceIndex, key: str) -> int:

    words = [w for w in key.split() if len(w) >= 3]
    if not words:
        return len(idx)
    return min(idx.doc_freq.get(w, 0) for w in words)


def candidate_tokens(idx: SliceIndex, *texts: str) -> list[str]:
    limit = max(1, int(len(idx) * MAX_DOC_FREQ)) if idx.records else 1
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for t in strong_tokens(text or ""):
            key = _token_key(t)
            if len(key.replace(" ", "")) < 3 or key in seen:
                continue
            if _BARE_NUMBER.match(key):
                continue
            if key in idx.kind_words:
                continue
            if _doc_freq(idx, key) > limit and not _in_any_firm(idx, key):
                continue
            seen.add(key)
            out.append(t)
    return out


def _in_any_firm(idx: SliceIndex, key: str) -> bool:
    needle = f" {key} "
    return any(needle in hay for hay in idx.firm)


def _weight(token: str) -> float:
    key = _token_key(token)
    letters = len(key.replace(" ", ""))
    base = (4.0 if letters >= 4 else 2.5) if _HAS_DIGIT.search(key) \
        else (3.0 if letters >= 5 else 2.0)
    return base + 1.5 * (len(key.split()) - 1)


def match_rules(idx: SliceIndex, mark: str = "", trademark: str = "",
                ru_name: str = "") -> Match:
    if not idx.records:
        return Match(reason="срез пуст")
    tokens = candidate_tokens(idx, mark, trademark, ru_name)
    if not tokens:
        return Match(reason="в тексте позиции нет признаков, различающих внутри вида")

    scores: list[_Score] = []
    for i in range(len(idx.records)):
        dev, firm = idx.dev[i], idx.firm[i]
        hit: list[str] = []
        strong = weak = firm_pts = 0.0
        for t in tokens:
            key = f" {_token_key(t)} "
            in_dev, in_firm = key in dev, key in firm
            if not (in_dev or in_firm):
                continue
            hit.append(t)
            w = _weight(t)
            if in_firm:
                firm_pts += w
            elif _HAS_DIGIT.search(_token_key(t)):
                strong += w
            else:
                weak += w
        if firm_pts + strong + weak >= MIN_SCORE:
            scores.append(_Score(firm_pts + strong + weak, strong, weak, hit, i))
    if not scores:
        return Match(reason="ни одна запись среза не совпала", tokens=tokens)

    best = max(s.total for s in scores)
    top = [s for s in scores if s.total >= best - 0.01]
    picked_records = [idx.records[s.i] for s in top]
    if not one_firm(picked_records):
        return Match(reason="признаки указали на разные заводы", tokens=tokens,
                     rivals=len({producer_key(r.producer) for r in picked_records}),
                     score=best)

    winner = picked_records[0]
    pool = [s for s in scores if one_firm([idx.records[s.i], winner])] or top

    pick = max(pool, key=lambda s: (s.strong,
                                    is_active(idx.records[s.i].status),
                                    similarity(ru_name or mark, idx.records[s.i].ru_name),
                                    s.weak, s.total))
    chosen = idx.records[pick.i]
    if contradicts(chosen, mark, trademark, ru_name):
        return Match(reason="артикул в контракте не сходится с артикулом "
                            "в записи реестра — изделие зарегистрировано "
                            "под другим видом", tokens=tokens, score=pick.total)
    return Match(record=chosen, score=pick.total, tokens=pick.hits)


_ARTICLE = re.compile(r"\b([a-zа-я]{3,6})\s?-\s?(\d{3,5})[a-zа-я0-9-]*\b")
_TAG = re.compile(r"<[^>]+>")


def _article_families(text: str) -> dict[str, set[str]]:

    from .nameparse import _TU

    out: dict[str, set[str]] = {}
    clean = latinize(_norm(_TAG.sub(" ", _TU.sub(" ", text or "")))).lower()
    for m in _ARTICLE.finditer(clean):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


def contradicts(record: RznRecord, mark: str = "", trademark: str = "",
                ru_name: str = "") -> bool:

    pos = _article_families(" ".join((mark, trademark, ru_name)))
    if not pos:
        return False
    rec = _article_families(" ".join((record.ru_name, record.models_description)))
    return any(pref in rec and not (nums & rec[pref]) for pref, nums in pos.items())


def one_firm(records: list[RznRecord]) -> bool:

    from .verify import phonetic_close

    uniq: dict[str, RznRecord] = {}
    for r in records:
        if r.producer:
            uniq.setdefault(producer_key(r.producer), r)
    if len(uniq) <= 1:
        return True
    items = list(uniq.values())
    with_inn = [r.declarant_inn for r in items if r.declarant_inn]
    same_inn = len(with_inn) == len(items) and len(set(with_inn)) == 1
    limit = FIRM_SAME_INN if same_inn else FIRM_MATCH
    return all(phonetic_close(items[0].producer, r.producer, limit)
               for r in items[1:])


def confirms(idx: SliceIndex, record: RznRecord, mark: str = "",
             trademark: str = "", ru_name: str = "", strict: bool = False) -> bool:

    return bool(evidence_of(idx, record, mark, trademark, ru_name,
                            strict=strict))


def evidence_of(idx: SliceIndex, record: RznRecord, mark: str = "",
                trademark: str = "", ru_name: str = "",
                strict: bool = False) -> list[str]:

    if contradicts(record, mark, trademark, ru_name):
        return []
    for i, r in enumerate(idx.records):
        if r is record:
            hay = idx.haystack(i)
            tokens = (candidate_tokens(idx, mark, trademark, ru_name) if strict
                      else _evidence_tokens(idx, mark, trademark, ru_name))
            return [t for t in tokens if f" {_token_key(t)} " in hay]
    return []


_EVIDENCE_STOP = {
    "принадлежностями", "принадлежности", "вариант", "варианты", "исполнения",
    "исполнение", "модель", "модели", "серия", "серии", "медицинский",
    "медицинская", "медицинское", "медицинских", "изделие", "изделия",
    "комплект", "комплекте", "составе", "состав", "номер", "года", "году",
    "тип", "типа", "россия", "оборудование", "оборудования", "имплантаты",
}


def _evidence_tokens(idx: SliceIndex, *texts: str) -> list[str]:

    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        words = re.findall(r"[a-zа-яё0-9][\w\-]{2,}", (text or "").lower().replace("ё", "е"))
        for t in list(strong_tokens(text or "")) + words:
            key = _token_key(t)
            flat = key.replace(" ", "")
            if len(flat) < 4 or key in seen or _BARE_NUMBER.match(key):
                continue
            if key in idx.kind_words or key in _EVIDENCE_STOP:
                continue
            seen.add(key)
            out.append(t)
    return out


def is_active(status: str) -> bool:
    return (status or "").strip().lower().startswith("действ")
