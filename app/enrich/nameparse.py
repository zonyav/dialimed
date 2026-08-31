

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from functools import lru_cache

RU_NUMBER_RE = re.compile(
    r"\b(РЗН|ФСЗ|ФСР|ФС)\s*[-–]?\s*№?\s*(\d{4})\s*/\s*(\d{2,6})\b", re.I)
MZ_NUMBER_RE = re.compile(r"\bМЗ\s*РФ\s*№?\s*(\d{2,4})\s*/\s*(\d{2,6})\b", re.I)
ERUL_RE = re.compile(r"\b([А-ЯA-Z]{1,3}\d{3}[-–]\s?\d{3,6}[-–]\s?\d{2}\s?/\s?\d{4,10})\b")
ERUL_PARTIAL_RE = re.compile(r"\b[А-ЯA-Z]{1,3}\d{3}(?:[-–]\s?\d{2,6})*[-–]?\s?\d{0,6}\b")

_TU = re.compile(
    r"\s*(?:по\s+)?\bТУ\s*[-–]?\s*№?\s*[\d\w]+(?:(?:\s*[-–]\s*|\.)[\d\w]+)+\s*", re.I)
TU_NUMBER_RE = re.compile(
    r"\bТУ\s*[-–]?\s*№?\s*(\d[\d.]*(?:\s*[-–]\s*[\dA-Za-zА-ЯЁа-яё]+){2,})", re.I)
_GOST = re.compile(
    r"\s*(?:по\s+)?\b(?:ГОСТ|ОСТ|РСТ)\b(?:\s+РФСР|\s+Р\b)?[\w\s.\-]{0,25}\d{3,}[\w.\-]*\s*",
    re.I)
_TU_SUFFIX = re.compile(
    r"\s*(?:по\s+)?\b[А-ЯЁA-Z]{2,6}[.\-]\s?\d{4,6}[.\-]\s?\d{2,4}\s*ТУ\b\.?\s*", re.I)
_ACCESSORIES = re.compile(
    r"\s*[,;:]?\s*(?:в\s+комплекте\s+)?с\s+принадлежност\w*\s*", re.I)
_ATTACHMENT_TAIL = re.compile(
    r"\s*[,;:]?\s*(?:в\s+комплекте\s+)?с\s+(?:приставк\w+|насадк\w+)\b.*$", re.I)
_IN_SET = re.compile(r"\s*[,;]\s*в\s+набор\w*\s*$", re.I)
_ACCESSORY_TAIL = re.compile(r"\s*(?:[IVX]{1,4}\.|\d{1,2}\.)?\s*Принадлежности\s*:.*$", re.I)
_PARTS_LIST_TAIL = re.compile(
    r"\s*\d{1,2}\.\s*[^;]{2,60}?[-–]\s*\d+\s*шт\.?.*$", re.I | re.S)
_COMPOSITION_TAIL = re.compile(r"\s*,?\s*в\s+состав\w*\s*:.*$", re.I | re.S)
_DATE_TAIL = re.compile(r"\s*от\s+\d{1,2}[\s.]\w+[\s.]\d{4}\s*(?:год\w*)?\s*", re.I)
_ERUL_LABEL = re.compile(r"\(\s*ЕРУЛ\s*[-–]?\s*", re.I)
_REG_NUMBER = re.compile(
    r"\s*(?:номер\s+)?(?:государственн\w*\s+)?регистрац\w*\s*(?:номер)?"
    r"(?:\s+товарн\w*\s+знак\w*)?\s*[:№]?\s*\d{4,12}\s*", re.I)
_MI_BLOCK = re.compile(r"\(\s*объект закупки является медицинским издели.*$", re.I | re.S)
_NKMI_INLINE = re.compile(r"код\s*НКМИ\s*[:\s]\s*\d{3,8}", re.I)
_BARE_RU = re.compile(r"\b(?:19|20)\d{2}\s*/\s*\d{2,6}\b")
CODE_KTRU_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}(?:-\d{4,8})?\b")
_CODE_LABEL = re.compile(r"\b(?:ОКПД\s*-?\s*2?|ОКП|КТРУ)\b\s*[:№]?", re.I)

_VARIANT_SPLIT = re.compile(
    r"[,;:]?\s*(?:в\s+)?вариант\w*\s+исполнени\w*\s*[:\-–]?\s*", re.I)
_MODEL_SPLIT = re.compile(
    r"[,;:]?\s*(?:следующ\w*\s+)?модел[ьия]\w*\s*[:\-–]?\s*", re.I)
_SERIES_SPLIT = re.compile(r"[,;:]?\s*сери[ияй]\s*[:\-–]?\s*", re.I)
_BARE_EXEC_SPLIT = re.compile(
    r"[,;:]\s*(?:в\s+)?(?:следующ\w+\s+)?исполнени\w*\s*[:\-–]?\s*", re.I)
_ENUM_PREFIX = re.compile(r"^\s*(?:[IVX]{1,4}\.|\d{1,2}\.|[-–—])\s*", re.I)
_EXEC_WORD_PREFIX = re.compile(
    r"^\s*(?:исполнени\w*|вариант\w*|модел[ьия]\w*|сери[ияй])\s*[:\-–№]?\s*", re.I)

_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")

_NO_RU_NEEDED = re.compile(
    r"(?=.*регистрационн\w*\s+удостоверени\w*)"
    r".*(?:не\s+требуе\w*|не\s+подлежит\s+регистрац\w*|не\s+регистрируются)", re.I | re.S)

_HYPHEN_BREAK = re.compile(r"\b([А-Яа-яЁё]{2,})-([а-яё]{2,})\b")

_HOMOGLYPH = str.maketrans("ACBEHKMOPTXYacepoxy", "АСВЕНКМОРТХУасеорху")


_HOMOGLYPH_BACK = str.maketrans("АСВЕНКМОРТХУасеорху", "ACBEHKMOPTXYacepoxy")


def _extract_probe(s: str) -> str:

    def one(m: re.Match) -> str:
        w = m.group(0)
        lat = len(re.findall(r"[A-Za-z]", w))
        cyr = len(re.findall(r"[А-Яа-яЁё]", w))
        if not lat or not cyr:
            return w
        return w.translate(_HOMOGLYPH) if cyr > lat else w.translate(_HOMOGLYPH_BACK)

    return re.sub(r"\S+", one, s or "")


def _mixed_norm(s: str) -> str:

    def one(m: re.Match) -> str:
        w = m.group(0)
        lat = len(re.findall(r"[A-Za-z]", w))
        cyr = len(re.findall(r"[А-Яа-яЁё]", w))
        if 1 <= lat <= 2 and cyr >= 3:
            return w.translate(_HOMOGLYPH)
        return w
    return re.sub(r"\S+", one, s or "")
_STRUCTURE_WORDS = ("модель", "модели", "модель", "вариант", "варианты",
                    "исполнение", "исполнения", "исполнении", "серия", "серии",
                    "принадлежности", "принадлежностями", "комплектация")

_NOT_A_MARK = frozenset("""
FALSE TRUE NULL NONE NAN N/A NA
ЗАМЕНА НЕТ ОТСУТСТВУЕТ ОТСУТСТВУЮТ
""".split()) | {"НЕ ТРЕБУЕТСЯ", "НЕ ТРЕБУЮТСЯ", "Б/Н", "БЕЗ НОМЕРА",
                "НЕ УКАЗАНО", "НЕ ПРИМЕНИМО"}


def _is_not_a_mark(s: str) -> bool:
    t = " ".join((s or "").split()).strip(" .,:;!?\"'«»()[]-–—")
    return t.upper().replace("Ё", "Е") in _NOT_A_MARK

YEAR_RE = re.compile(r"^(?:19|20)\d{2}\s*(?:г|год\w*)?\.?$", re.I)

COUNTRY_RE = re.compile(
    r"^(?:росси\w*|российск\w+(?:\s+федерац\w+)?|рф|кита\w*|китайск\w+(?:\s+народн\w+"
    r"\s+республик\w+)?|герман\w*|итали\w*|япони\w*|коре\w*|тайван\w*|инди\w*|"
    r"турци\w*|сша|швейцари\w*|беларус\w*|казахстан\w*|польш\w*|чехи\w*|"
    r"франци\w*|великобритани\w*|нидерланд\w*|финлянди\w*|словаки\w*|"
    r"russia|china|germany|italy|japan|korea|usa)$", re.I)


@dataclass(slots=True)
class NameParts:
    brand: str = ""
    model: str = ""
    variant: str = ""
    ru_numbers: list[str] = field(default_factory=list)
    tu_number: str = ""
    erul: str = ""
    core: str = ""
    leftover: str = ""

    @property
    def model_full(self) -> str:
        if self.model and self.variant and self.variant.lower() != self.model.lower():
            return f"{self.model} ({self.variant})"
        return self.model or self.variant

    @property
    def full(self) -> str:

        brand, model = (self.brand or "").strip(), (self.model_full or "").strip()
        if not brand:
            return model
        if not model:
            return brand
        if model.lower().startswith(brand.lower()):
            return model
        return f"{brand} {model}"


def _mend_spaced_number(text: str) -> str:

    return re.sub(r"(?<=\d)[\s ]+(?=\d{2,}\s*[-–])", "", text)


def clean_number(s: str) -> str:

    s = re.sub(r"[\s ]+", "", s or "")
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = re.sub(r"(?<=\d{4})[A-Za-zА-ЯЁа-яё]$", "", s)
    return s.strip(" -.")


def extract_tu(text: str) -> str:

    m = TU_NUMBER_RE.search(_mend_spaced_number(text or ""))
    if not m:
        return ""
    return clean_number(m.group(1))


def extract_ru_numbers(text: str) -> list[str]:
    out: list[str] = []
    for m in RU_NUMBER_RE.finditer(text or ""):
        v = f"{m.group(1).upper()} {m.group(2)}/{m.group(3)}"
        if v not in out:
            out.append(v)
    for m in MZ_NUMBER_RE.finditer(text or ""):
        v = f"МЗ РФ № {m.group(1)}/{m.group(2)}"
        if v not in out:
            out.append(v)
    return out


def pick_main_ru(text: str, ru_numbers: list[str], type_hints: list[str]) -> str:

    if len(ru_numbers) <= 1:
        return ru_numbers[0] if ru_numbers else ""

    hints = [_norm(h) for h in type_hints if h]
    if not hints:
        return ru_numbers[0]

    best, best_score = ru_numbers[0], -1.0
    for num in ru_numbers:
        pos = text.upper().find(num.upper())
        if pos < 0:
            continue
        left = _norm(text[max(0, pos - 160):pos])
        score = max(_overlap(left, h) for h in hints)
        if score > best_score:
            best, best_score = num, score
    return best


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9 ]+", " ", s)


def _overlap(text: str, phrase: str) -> float:
    words = [w for w in phrase.split() if len(w) > 3]
    if not words:
        return 0.0
    return sum(1 for w in words if w in text) / len(words)


_TM_SIGN = re.compile(r"[®™©℠]")


def _strip_noise(s: str) -> str:
    s = _TM_SIGN.sub("", s)
    s = _MI_BLOCK.sub(" ", s)
    s = _NKMI_INLINE.sub(" ", s)
    if _ERUL_LABEL.search(s):
        s = _ERUL_LABEL.sub(" ", s)
        s = ERUL_RE.sub(" ", s)
        s = ERUL_PARTIAL_RE.sub(" ", s, count=1)
    s = ERUL_RE.sub(" ", s)
    s = RU_NUMBER_RE.sub(" ", s)
    s = _BARE_RU.sub(" ", s)
    s = _DATE_TAIL.sub(" ", s)
    s = _TU.sub(" ", s)
    s = _TU_SUFFIX.sub(" ", s)
    s = _GOST.sub(" ", s)
    s = _REG_NUMBER.sub(" ", s)
    s = re.sub(r"(?<=[A-Za-zА-Яа-я:])(I{1,3}\.\s*[А-ЯA-Z])", r" \g<1>", s)
    s = re.sub(r"(?<=[A-Za-z0-9])([А-ЯЁ][а-яё]{3,})", r" \g<1>", s)
    s = _IN_SET.sub(" ", s)
    s = CODE_KTRU_RE.sub(" ", s)
    s = _CODE_LABEL.sub(" ", s)
    s = _ACCESSORY_TAIL.sub(" ", s)
    s = _PARTS_LIST_TAIL.sub(" ", s)
    s = _COMPOSITION_TAIL.sub(" ", s)
    s = _ATTACHMENT_TAIL.sub(" ", s)
    s = s.replace("по РУ", " ")
    return " ".join(s.split()).strip(" ,;:.-–—\"'«»()")


def _drop_dangling(s: str) -> str:

    words = re.sub(r"(\S)\s+([-–])\s+", r"\1\2 ", s or "").split()
    out: list[str] = []
    for i, w in enumerate(words):
        if len(w) > 1 and w[-1] in "-–":
            stem = w[:-1]
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt and nxt.lower().startswith(stem.lower()):
                continue
            w = stem
        out.append(w)
    return " ".join(out)


def _strip_type(s: str, type_hints: list[str]) -> str:

    stems: set[str] = set()
    abbr: set[str] = set()
    for hint in type_hints:
        for w in re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", hint or ""):
            stems.add(w[:-2].lower() if len(w) > 5 else w.lower())
        if hint and hint != hint.upper():
            abbr.update(re.findall(r"\b[А-ЯЁA-Z]{3,5}\b", hint))

    def _mend(m: re.Match) -> str:
        joined = (m.group(1) + m.group(2))
        low = joined.lower().replace("ё", "е")
        if low in _STRUCTURE_WORDS:
            return joined
        return joined if any(low.startswith(st) for st in stems) else m.group(0)

    if stems or _STRUCTURE_WORDS:
        s = _HYPHEN_BREAK.sub(_mend, s)
    if abbr:
        s = re.sub(r"\b(?:%s)\b(?![-–\w])" % "|".join(
            sorted(map(re.escape, abbr), key=len, reverse=True)), " ", s)
    if stems:
        pattern = r"\b(?:%s)[а-яё]*" % "|".join(
            sorted(map(re.escape, stems), key=len, reverse=True))
        probe = _mixed_norm(s)
        chars = list(s)
        for m in re.finditer(pattern, probe, flags=re.I):
            for i in range(*m.span()):
                chars[i] = " "
        s = "".join(chars)
    return _drop_dangling(" ".join(s.split()).strip(" ,;:.-–—\"'«»"))


_MARK_PATTERNS = (
    re.compile(r"[«\"']([^«»\"']{2,40})[»\"']"),
    re.compile(r"\b(?=[A-Za-zА-ЯЁа-яё0-9\-–./]*[A-ZА-ЯЁ0-9])"
               r"[A-Za-zА-ЯЁа-яё0-9]+"
               r"(?:[-–/][A-Za-zА-ЯЁа-яё0-9]+(?:\.\d+)*)+\b"),
    re.compile(r"\b[A-Za-z][A-Za-z&+.]{1,}\b"),
    re.compile(r"\b[А-ЯЁ]{2,}\b"),
    re.compile(r"\b[А-ЯЁA-Z][а-яёA-Za-z]*[-–][А-ЯЁA-Z0-9][\w\-]*\b"),
    re.compile(r"\b[A-Za-zА-ЯЁа-яё]*\d[\w\-]*\b"),
    re.compile(r"\b[А-ЯЁ][а-яё]{3,}\b"),
    re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]+[А-ЯЁA-Z][а-яёA-Za-z]*\b"),
    re.compile(r"\b[А-ЯЁA-Z](?=\.\s*[А-ЯЁA-Z])"),
    re.compile(r"\b[А-ЯЁA-Z](?=\s\d)"),
)
_MARK_STOP = {"ту", "ру", "гост", "исо", "iso", "мм", "см", "шт", "кг", "мл",
              "тип", "серия", "модель", "вариант", "исполнение", "ерул", "рзн",
              "фсз", "фср", "пр", "др", "т", "г", "в", "с", "по", "и",
              "фирма", "компания", "завод", "предприятие", "производитель",
              "изготовитель", "концерн", "корпорация", "холдинг"}
_MARK_UNITS = {"мм", "см", "кг", "мл", "шт", "г", "т", "л"}

_OPF_WORDS = re.compile(
    r"\b(ооо|оао|зао|пао|ао|нао|ип|фгуп|гуп|муп|ано|нко|gmbh|ltd|llc|inc|co|s\.?r\.?[ol]\.?)\b",
    re.I)


def _company_core(s: str) -> str:
    t = _OPF_WORDS.sub(" ", (s or "").lower().replace("ё", "е"))
    return re.sub(r"[^a-zа-я0-9]+", "", t)


def _drop_company(text: str, keys: set[str]) -> str:

    def cut(match: re.Match) -> str:
        return " " if _company_core(match.group(0)) in keys else match.group(0)

    text = _QUOTED_RE.sub(lambda m: " " if _company_core(m.group(1)) in keys else m.group(0), text)
    text = re.sub(r"[-–]?\b[A-Za-zА-ЯЁа-яё]+(?:[-–\s][A-Za-zА-ЯЁа-яё]+)?\b", cut, text)
    return " ".join(text.split())
_MARK_STOP |= {n.lower() for n in (
    "I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX"
).split()}

_QUOTED_RE = re.compile(r"[«\"']([^«»\"']{2,40})[»\"']")

_TYPO_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‟": '"',
                              "‘": "'", "’": "'", "‚": "'", "”": '"'})

_GLUED_TOKEN = re.compile(
    r"^([«\"'“„]*)"
    r"([\wА-Яа-яЁё][\wА-Яа-яЁё./]*(?:-[\wА-Яа-яЁё0-9./]+)*)"
    r"-[«\"'“„]"
    r"([\wА-Яа-яЁё][\wА-Яа-яЁё0-9-]{1,19})"
    r"[»\"'”‟]+([,.;:)]*)$")


def unglue_quoted_tail(text: str) -> str:

    out = []
    for token in (text or "").translate(_TYPO_QUOTES).split(" "):
        m = _GLUED_TOKEN.match(token)
        out.append(f"{m.group(2)}-{m.group(3)}{m.group(4)}" if m else token)
    return " ".join(out)


def quoted_marks(text: str) -> list[str]:

    out: list[str] = []
    for m in _QUOTED_RE.finditer((text or "").translate(_TYPO_QUOTES)):
        v = _TU.sub(" ", m.group(1))
        v = " ".join(v.split()).strip(" ,;:.-–—")
        if v and len(v) >= 2 and v.lower() not in _MARK_STOP and v not in out:
            out.append(v)
    return out


def _extract_mark(s: str, quoted: list[str] | None = None) -> str:
    found = _extract_marks(s, quoted)
    return found[0] if found else ""


def _extract_marks(s: str, quoted: list[str] | None = None) -> list[str]:

    if not s:
        return []
    spans: list[tuple[int, int]] = []
    probe = _extract_probe(s)
    for pat in _MARK_PATTERNS:
        for m in pat.finditer(probe):
            span = m.span(1) if m.lastindex else m.span()
            g = s[span[0]:span[1]]
            token = g.strip()
            end = m.end(1) if m.lastindex else m.end()
            exec_letter = (len(token) == 1 and token.isalpha() and token.isupper()
                           and re.match(r"\s\d", s[end:end + 2]) is not None)
            if len(token) < 2 and not token.isdigit() and not exec_letter \
                    and s[end:end + 1] != ".":
                continue
            if DESCRIPTIVE_RE.fullmatch(token):
                continue
            if token.strip(".").lower() in _MARK_STOP:
                if token.lower() not in _MARK_UNITS or token.islower():
                    continue
                before = s[:m.start()].rstrip()
                if not before or before[-1].isdigit():
                    continue
            spans.append((m.start(1), m.end(1)) if m.lastindex else (m.start(), m.end()))
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for a, b in spans[1:]:
        gap = s[merged[-1][1]:a] or ""
        initial = (merged[-1][1] - merged[-1][0] == 1
                   and re.fullmatch(r"\.\s*", gap) is not None)
        if a <= merged[-1][1] or initial or re.fullmatch(r"[\s\-–]*", gap):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    merged.sort(key=lambda p: (_markness(s[p[0]:p[1]], quoted), p[1] - p[0]), reverse=True)
    out: list[str] = []
    for a, b in merged:
        v = s[a:b].strip(" ,;:.-–—\"'«»")
        if v and v not in out:
            out.append(v)
    return out


def _markness(s: str, quoted: list[str] | None = None) -> int:
    score = 0
    if quoted and _quote_hit(s, quoted):
        score += 10
    if re.search(r"[A-Za-z]", s):
        score += 3
    if re.search(r"\d", s):
        score += 3
    if re.search(r"\b[А-ЯЁ]{2,}\b", s):
        score += 2
    if re.match(r"[А-ЯЁA-Z]", s):
        score += 1
    if re.fullmatch(r"[а-яё\s\-–]+", s):
        score -= 5
    return score


def _hint_words(type_hints: list[str] | None) -> list[str]:
    out: list[str] = []
    for h in type_hints or []:
        out.extend(w.lower().replace("ё", "е")
                   for w in re.findall(r"[А-Яа-яЁё]{5,}", h or ""))
    return out


def _is_type_word(word: str, hint_words: list[str]) -> bool:

    w = (word or "").lower().replace("ё", "е")
    if len(w) < 5:
        return False
    for h in hint_words:
        n = min(len(w), len(h))
        i = 0
        while i < n and w[i] == h[i]:
            i += 1
        if i >= 5:
            return True
        j = 0
        while j < n and w[-1 - j] == h[-1 - j]:
            j += 1
        if j >= 6:
            return True
    return False


def _drop_lead_type(value: str, hint_words: list[str],
                    quoted: list[str] | None = None,
                    bare_ok: bool = False) -> str:

    v = " ".join((value or "").split())
    for _ in range(3):
        words = v.split()
        if len(words) < 2:
            break
        first = words[0].strip(" ,;:.")
        if not re.fullmatch(r"[А-ЯЁа-яё]{5,}", first):
            break
        if quoted and _quote_hit(first, quoted):
            break
        if not _is_type_word(first, hint_words):
            break
        rest = " ".join(words[1:]).strip(" ,;:.-–—")
        if not rest or not _looks_like_mark(rest, quoted, bare_ok):
            break
        v = rest
    return v


def is_type_abbrev(candidate: str, source: str) -> bool:

    letters = re.sub(r"[^A-ZА-ЯЁ]", "", candidate or "")
    if not (2 <= len(letters) <= 5) or letters != re.sub(r"[\s\-–]", "", candidate or ""):
        return False
    words = re.findall(r"[А-ЯЁA-Za-zа-яё]{3,}", source or "")
    for start in range(len(words)):
        head = "".join(w[0].upper() for w in words[start:start + len(letters)])
        if head == letters and words[start].upper() != letters:
            return True
    return False


def enumerated_articles(quoted: list[str]) -> list[str]:

    arts = [q for q in quoted if _DIGIT.search(q) and len(q) <= 24]
    if len(arts) < 2:
        return []
    prefixes = {re.match(r"[^\d]*", a).group(0).upper().strip(" -–_") for a in arts}
    numbers = {re.sub(r"\D", "", a) for a in arts}
    return arts if len(prefixes) == 1 and len(numbers) >= 2 else []


_PAREN_ITEM = re.compile(r"\(\s*([A-Za-zА-ЯЁа-яё0-9][\w\-–/. ]{2,23})\s*\)")


def paren_articles(text: str) -> list[str]:
    out: list[str] = []
    for m in _PAREN_ITEM.finditer(text or ""):
        v = " ".join(m.group(1).split()).strip(" ,;:.-–—")
        if v and v.lower() not in _MARK_STOP and v not in out:
            out.append(v)
    return out


def cut_article_list(s: str, articles: list[str]) -> str:

    if not articles:
        return s
    i = s.find(articles[0])
    if i <= 0:
        return s
    cut = max(s.rfind(c, 0, i) for c in ":,;")
    head = (s[:cut] if cut > 0 else s[:i]).strip(" ,;:.-–—")
    return head or s


def _quote_hit(fragment: str, quoted: list[str]) -> bool:

    f = _norm(fragment).strip()
    if not f:
        return False
    return any(f == (q := _norm(v).strip()) or f.startswith(q) or q.startswith(f)
               for v in quoted)


_OPF_TRANSLIT = re.compile(
    r"\b(ко|лтд|лимитед|гмбх|инк|корп|компани|энд|кг|аг|пте|плс)\b", re.I)


def _contains_words(whole: str, part: str) -> bool:
    w, p = _norm(whole).strip(), _norm(part).strip()
    if not w or not p:
        return False
    return bool(re.search(r"(?:^|\s)%s(?:\s|$)" % re.escape(p), w))


def _expand_quoted(fragment: str, quoted: list[str],
                   type_hints: list[str] | None = None) -> str:

    if not fragment or not quoted:
        return fragment
    f = _norm(fragment).strip()
    if not f:
        return fragment
    hits = [v for v in quoted if _contains_words(v, fragment)]
    if len(hits) != 1:
        return fragment
    v = hits[0]
    if len(_norm(v).strip()) <= len(f):
        return fragment
    if _OPF_WORDS.search(v) or _OPF_TRANSLIT.search(v) or DESCRIPTIVE_RE.search(v):
        return fragment
    if _norm(_strip_type(v, list(type_hints or []))).strip() != _norm(v).strip():
        return fragment
    return v


def _clean_part(s: str) -> str:
    s = _ENUM_PREFIX.sub("", s or "")
    s = _EXEC_WORD_PREFIX.sub("", s)
    s = _ACCESSORIES.sub(" ", s)
    s = _REG_NUMBER.sub(" ", s)
    s = " ".join(s.split())
    return _cut_maker_tail(s.strip(" ,;:.-–—\"'«»()"))


def _cut_maker_tail(value: str) -> str:

    from .verify import is_company_name

    head, sep, tail = value.partition(",")
    head = head.strip()
    if not sep or not head or len(head) > 40 or not _looks_like_mark(head):
        return value
    first = tail.split(",")[0].strip(" .\"'«»")
    return head if is_company_name(first) else value


def _looks_like_mark(s: str, quoted: list[str] | None = None,
                     bare_cyrillic_ok: bool = False) -> bool:

    if not s or len(s) > 60:
        return False
    if len(s.split()) > 5:
        return False
    if COUNTRY_RE.match(s.strip(" .,\"'«»")) or YEAR_RE.match(s.strip(" .,\"'«»")):
        return False
    if s.strip(" .").lower() in _MARK_STOP:
        return False
    if _is_not_a_mark(s):
        return False
    if re.fullmatch(r"[\d\s.,/-]+", s):
        return False
    if _LATIN.search(s) or _DIGIT.search(s) or s.isupper() \
            or re.search(r"[А-ЯЁ][а-яё]+[-–][А-ЯЁ0-9]", s):
        return True
    if re.search(r"\b[А-ЯЁ][а-яё]+[А-ЯЁ]", s):
        return True
    return bool(_name_cased(s) or bare_cyrillic_ok
                or (quoted and _quote_hit(s, quoted)))


_INITIAL = re.compile(r"^[А-ЯЁA-Z]\.$")


def _name_cased(s: str) -> bool:

    words = [w for w in re.split(r"\s+", (s or "").strip(" .,\"'«»")) if w]
    if len(words) < 2 or len(words) > 4:
        return False
    return all(_INITIAL.match(w) or re.match(r"[А-ЯЁA-Z]", w) for w in words)


_OPF_ALONE = {"ооо", "оао", "зао", "ао", "пао", "нао", "ип", "нко", "уп", "чп",
              "гуп", "муп", "фгуп", "ано", "llc", "ltd", "gmbh", "inc"}


def is_opf_only(s: str) -> bool:
    return (s or "").strip(" .,\"'«»").lower() in _OPF_ALONE


def looks_like_mark(s: str, quoted: list[str] | None = None,
                    type_hints: list[str] | None = None) -> bool:

    if is_opf_only(s):
        return False
    if not _looks_like_mark(s, quoted):
        return False
    hint_words = _hint_words(type_hints)
    words = [w for w in re.findall(r"[А-Яа-яЁё]{5,}", s or "")]
    if hint_words and words and all(_is_type_word(w, hint_words) for w in words):
        return False
    return True


_PLAIN_WORD = re.compile(r"^[а-яё]+$")


def is_plain_word(s: str) -> bool:

    return bool(_PLAIN_WORD.fullmatch((s or "").strip()))


def parse_name(ru_name: str, type_hints: list[str] | None = None,
               exclude: list[str] | None = None) -> NameParts:

    parts = NameParts()
    raw = " ".join(unglue_quoted_tail(ru_name).split())
    if not raw:
        return parts

    if _NO_RU_NEEDED.match(raw):
        parts.core = raw
        return parts

    parts.ru_numbers = extract_ru_numbers(raw)
    parts.tu_number = extract_tu(raw)
    m = ERUL_RE.search(raw)
    if m:
        parts.erul = clean_number(m.group(1))

    quoted = quoted_marks(raw)

    bare_ok = False
    if exclude:
        keys = {_company_core(x) for x in exclude if _company_core(x)}
        if keys:
            quoted = [q for q in quoted if _company_core(q) not in keys]
            raw_wo = _drop_company(raw, keys)
            if raw_wo != raw:
                raw, bare_ok = raw_wo, True

    enumerated = enumerated_articles(quoted)
    paren_enum = enumerated_articles(paren_articles(raw))
    if paren_enum and not enumerated:
        enumerated = paren_enum
    named_list = variants_in_registry_name(raw)
    listed = {_norm(x) for x in enumerated} | {_norm(x) for x in named_list}
    family = _norm(re.match(r"[^\d]*", enumerated[0]).group(0)) if enumerated else ""

    def in_listed(v: str) -> bool:
        n = _norm(v)
        return bool(n) and (n in listed
                            or (family and _DIGIT.search(v) and n.startswith(family)))

    hints = list(type_hints or [])
    body = _strip_noise(raw)
    parts.core = body
    if paren_enum:
        body = cut_article_list(body, paren_enum)
    body = _strip_type(body, hints)

    variant = model = ""
    for splitter in (_VARIANT_SPLIT, _BARE_EXEC_SPLIT):
        chunks = splitter.split(body)
        if len(chunks) > 1:
            variant = _clean_part(chunks[-1])
            body = chunks[0]
            break
    chunks = _MODEL_SPLIT.split(body)
    if len(chunks) > 1:
        model = _clean_part(chunks[-1])
        body = chunks[0]
    if not model:
        chunks = _SERIES_SPLIT.split(body)
        if len(chunks) > 1:
            model = _clean_part(chunks[-1])
            body = chunks[0]

    head = _clean_part(_strip_type(body, hints))
    head_marks = [m for m in _extract_marks(head, quoted) if not in_listed(m)]
    brand = (head_marks[0] if head_marks else "") or (
        head if not enumerated and not named_list else "")
    wider = _expand_quoted(brand, quoted, hints)
    if wider != brand and not in_listed(wider):
        brand = wider
    variant = _extract_mark(_strip_type(variant, hints), quoted) or _strip_type(variant, hints)
    model = _extract_mark(_strip_type(model, hints), quoted) or _strip_type(model, hints)
    for name, value in (("variant", variant), ("model", model)):
        wider = _expand_quoted(value, quoted, hints)
        if wider != value and not in_listed(wider):
            if name == "variant":
                variant = wider
            else:
                model = wider
    if in_listed(variant):
        variant = ""
    if in_listed(model):
        model = ""

    if not _looks_like_mark(brand, quoted, bare_ok):
        parts.leftover = head
        brand = ""
    if not _looks_like_mark(model, quoted):
        model = ""
    if not _looks_like_mark(variant, quoted):
        variant = ""
    if model and DESCRIPTIVE_RE.match(model):
        model = ""
    if variant and DESCRIPTIVE_RE.match(variant):
        variant = ""

    hint_words = _hint_words(hints)
    if hint_words:
        brand = _drop_lead_type(brand, hint_words, quoted, bare_ok)
        model = _drop_lead_type(model, hint_words, quoted)
        variant = _drop_lead_type(variant, hint_words, quoted)

    if brand and not model and not variant:
        words = brand.split()
        for i in range(1, len(words)):
            if re.fullmatch(r"\d{2,5}[А-ЯЁA-Za-zа-яё\-]*", words[i]):
                model = " ".join(words[i:])
                brand = " ".join(words[:i])
                break

    if brand and not model and not variant and quoted and _quote_hit(brand, quoted):
        for cand in head_marks[1:]:
            first_word = cand.split()[0].lower().rstrip(".:") if cand.split() else ""
            if first_word in _MARK_STOP:
                continue
            if re.search(r"(?:^|\s)%s(?:\s|$)" % re.escape(_norm(cand).strip()),
                         _norm(brand).strip()):
                continue
            if (len(cand) <= 14 and _DIGIT.search(cand)
                    and _norm(cand) != _norm(brand) and _looks_like_mark(cand)):
                model = cand
                break

    if not brand and (model or variant):
        brand = model or variant
        if brand == model:
            model = ""
        else:
            variant = ""

    if brand and variant and _contains_words(brand, variant):
        variant = ""
    if brand and model and _contains_words(brand, model):
        model = ""

    if brand and is_type_abbrev(brand, raw):
        brand = ""
    if brand and is_type_fragment(brand, hints):
        brand = ""
    if model and is_type_fragment(model, hints):
        model = ""
    if variant and is_type_fragment(variant, hints):
        variant = ""

    for value in (variant, model):
        if brand and value and _same_family(brand, value) and _DIGIT.search(value):
            brand, model, variant = value, "", ""
            break

    brand = _drop_family_duplicates(brand)
    parts.brand, parts.model, parts.variant = brand, model, variant
    return parts


def _family_root(s: str) -> str:

    first = _extract_probe((s or "").strip()).split()
    if not first:
        return ""
    root = re.split(r"[-–/\d]", first[0], maxsplit=1)[0]
    return _norm(root)


def _drop_family_duplicates(value: str) -> str:

    words = (value or "").split()
    if len(words) < 2:
        return value
    keep: list[str] = []
    for w in words:
        twin = next((i for i, k in enumerate(keep)
                     if _same_family(k, w) and bool(_DIGIT.search(k)) != bool(_DIGIT.search(w))),
                    None)
        if twin is None:
            keep.append(w)
        elif _DIGIT.search(w):
            keep[twin] = w
    return " ".join(keep)


def _same_family(a: str, b: str) -> bool:

    ra, rb = _family_root(a), _family_root(b)
    if len(ra) < 4 or len(rb) < 4:
        return False
    return ra == rb or ra.startswith(rb) or rb.startswith(ra)


_HTML_TAG = re.compile(r"<[^>]+>")
DESCRIPTIVE_RE = re.compile(
    r"\b(кресл\w*|камер\w*|модул\w*|светильник\w*|блок\w*|столик\w*|стол\w*|плеч\w*|"
    r"наконечник\w*|шланг\w*|канал\w*|бутылк\w*|чаш\w*|фильтр\w*|систем\w*|"
    r"инструмент\w*|"
    r"пистолет\w*|микромотор\w*|скалер\w*|компрессор\w*|аспиратор\w*|педал\w*|"
    r"стойк\w*|штатив\w*|треног\w*|основани\w*|кронштейн\w*|тележк\w*|"
    r"держател\w*|подголовник\w*|подлокотник\w*|принадлежност\w*|комплект\w*|"
    r"устройств\w*|прибор\w*|аппарат\w*|установк\w*|издели\w*|состав\w*)\b",
    re.I,
)
_VARIANT_LINE = re.compile(
    r"(?:^|\n)\s*(?:[IVX]{1,4}\.|\d{1,2}\.)\s*(.{3,120}?)\s*(?:,\s*в\s+составе.*)?$",
    re.M | re.I)
_VARIANT_INLINE = re.compile(
    r"(?:сери[ияй]|вариант\w*\s+исполнени\w*)\s*[:\-–]?\s*([^,;.\n]{1,40})", re.I)


def variants_from_registry(models_description: str, type_hints: list[str] | None = None,
                           limit: int = 12) -> list[str]:

    if not models_description:
        return []
    return list(_variants_cached(models_description,
                                 tuple(type_hints or ()), limit))


@lru_cache(maxsize=4096)
def _variants_cached(models_description: str, type_hints: tuple,
                     limit: int) -> tuple:
    return tuple(_variants_parse(models_description, list(type_hints), limit))


def _variants_parse(models_description: str, type_hints: list[str],
                    limit: int) -> list[str]:
    text = _HTML_TAG.sub("\n", models_description)
    text = html.unescape(text.replace("&nbsp;", " "))
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)

    found: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        if len(found) >= limit:
            return
        v = _TU.sub(" ", v or "")
        v = _GOST.sub(" ", v)
        v = _clean_part(_strip_type(v, list(type_hints or [])))
        v = re.sub(r"\s*[-–]\s*\d+\s*шт\.?.*$", "", v, flags=re.I)
        v = re.sub(r"\s*,?\s*в\s+состав\w*.*$", "", v, flags=re.I).strip(" ,;:.-–—/")
        v = re.sub(r"^\d{1,2}\s*[.)]\s*", "", v)
        if v.count("(") == v.count(")") + 1:
            v += ")"
        if not v or len(v) < 2 or len(v) > 40:
            return
        if v[0].islower() or re.search(
                r"\d\s*шт\b|\bшт\.?\b|\bсостав|\bэкз\b|руководств|паспорт|"
                r"инструкц|упаков", v, re.I):
            return
        if not _looks_like_mark(v) or DESCRIPTIVE_RE.search(v):
            return
        key = v.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(v)

    for m in _VARIANT_INLINE.finditer(text):
        add(m.group(1))
    if len(found) < 2:
        for m in _VARIANT_LINE.finditer(text):
            add(m.group(1))
    return found[:limit]


_REG_LIST_HEAD = re.compile(
    r"(?:в\s+)?(?:модел[ьия]\w*|исполнени\w*|вариант\w*(?:\s+исполнени\w*)?)\s*[:\-–]?\s+",
    re.I)
_REG_LIST_ITEM = re.compile(r"^(?=[\w\-./]*[A-Za-zА-ЯЁа-яё])[A-ZА-ЯЁ0-9][\w\-./]{0,20}$")


def variants_in_registry_name(name: str) -> list[str]:

    m = _REG_LIST_HEAD.search(name or "")
    if not m:
        return []
    out: list[str] = []
    for chunk in re.split(r"[,;]", (name or "")[m.end():]):
        v = " ".join(chunk.split()).strip(" .«»\"'()")
        if _REG_LIST_ITEM.match(v):
            if v.lower() not in {x.lower() for x in out}:
                out.append(v)
        elif out:
            break
    return out if len(out) >= 2 else []


def is_type_fragment(candidate: str, type_hints: list[str] | None = None) -> bool:

    s = " ".join((candidate or "").split())
    if not s or not re.search(r"[А-ЯЁа-яё]\d", s):
        return False
    letters = re.sub(r"[^A-Za-zА-ЯЁа-яё]", "", s).lower().replace("ё", "е")
    if len(letters) < 4:
        return False
    for word in _hint_words(type_hints):
        w = word.lower().replace("ё", "е")
        if len(w) > len(letters) and w.startswith(letters):
            return True
    return False
