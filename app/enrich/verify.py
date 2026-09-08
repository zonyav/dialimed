

from __future__ import annotations

import re

COMPANY_FORM = re.compile(
    r"(?:^|[\s\"'«(])(?:ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП|НПО|НПП|НПФ|НПЦ|ФГУП|ГУП|МУП|"
    r"АНО|НКО|КБ|ТД)(?:[\s\"'»).,]|$)"
    r"|\b(?:Co\.?\s*,?\s*Ltd|Co|Ltd|LTD|Limited|LLC|L\.L\.C|Inc|Corp|Corporation|"
    r"Company|GmbH|AG|S\.?p\.?A|S\.?r\.?[lo]\.?|S\.?A\.?S|SARL|SAS|B\.?V|N\.?V|"
    r"A/S|Oy|AB|Kft|Sp\.\s*z\s*o\.?o|Pvt|KG|"
    r"Лтд|ЛТД|Лимитед|ГмбХ|Корпорейшн|Компани|Ко|с\.?р\.?о|а\.?с)\b\.?",
    re.I,
)
MANUF_LABEL = re.compile(r"(?:производител[ья]|изготовител[ья]|manufacturer)\s*[:：]", re.I)
NOT_COMPANY = re.compile(
    r"^\(?\d|показател|характеристик|требован|объекта\s+закупки|"
    r"^(?:кита[йя]|росси|герман|итал|япон|коре|тайван|инди|турци|швейцар|сша|"
    r"беларус|казахстан|польш|чеш|франц|британ|нидерланд)",
    re.I,
)

GENERIC = re.compile(
    r"^(установк\w*|аппарат\w*|систем\w*|прибор\w*|стол\w*|кресл\w*|стул\w*|набор\w*|"
    r"катетер\w*|нить\w*|нити|стент\w*|датчик\w*|комплект\w*|тележк\w*|весы|ростомер\w*|"
    r"носилк\w*|проводник\w*|издели\w*|оборудовани\w*|медицинск\w*|материал\w*|"
    r"инструмент\w*|мочеприемник\w*|интродьюсер\w*|томограф\w*|ингалятор\w*)\b",
    re.I,
)

def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def trim_edges(value: str) -> str:

    v = " ".join((value or "").split())
    prev = None
    while v and v != prev:
        prev = v
        v = v.strip(" .,;:-–—\"'«»[]")
        if len(v) > 1 and v[0] == "(" and v[-1] == ")" and _enclosed(v):
            v = v[1:-1]
        elif v[:1] == "(" and v.count("(") > v.count(")"):
            v = v[1:]
        elif v[-1:] == ")" and v.count(")") > v.count("("):
            v = v[:-1]
    return v


def _enclosed(v: str) -> bool:
    depth = 0
    for i, ch in enumerate(v):
        depth += (ch == "(") - (ch == ")")
        if depth == 0:
            return i == len(v) - 1
    return False


def is_company_name(value: str, source_text: str = "") -> bool:

    s = " ".join((value or "").split())
    if len(s) < 4 or len(s) > 120:
        return False
    if NOT_COMPANY.search(s):
        return False
    if COMPANY_FORM.search(s):
        return True
    m = MANUF_LABEL.search(source_text or "")
    if m:
        tail = norm(source_text[m.end(): m.end() + 120])
        if norm(s) and norm(s) in tail:
            return True
    return False


OPF = {"ооо": "ооо", "ао": "ао", "зао": "ао", "оао": "ао", "пао": "ао",
        "ип": "ип", "фгуп": "гуп", "гуп": "гуп", "муп": "гуп", "ано": "ано"}
OPF_TAIL = {"лтд", "ltd", "limited", "лимитед", "гмбх", "gmbh", "ко", "co",
             "corp", "корпорейшн", "инк", "inc", "llc", "srl", "spa", "ag",
             "bv", "nv", "sa", "as", "компани", "company"}
ABBREV = {
    "нпф": "научно производственная фирма",
    "нпо": "научно производственное объединение",
    "нпп": "научно производственное предприятие",
    "нпк": "научно производственная компания",
    "пкф": "производственно коммерческая фирма",
    "тд": "торговый дом",
}


_COUNTRIES = (
    r"Российск\w+\s+Федерац\w+|Росси\w+|"
    r"Китайск\w+\s+Народн\w+\s+Республик\w+|Китайск\w+|Кита[йя]|"
    r"Республик\w+\s+Беларус\w+|Беларус\w+|"
    r"Республик\w+\s+Корея|Южн\w+\s+Корея|Корея|"
    r"Соединённ\w+\s+Штат\w+(?:\s+Америки)?|США|"
    r"Федеративн\w+\s+Республик\w+\s+Герман\w+|Герман\w+|"
    r"Итали\w+|Япони\w+|Тайвань|Инди\w+|Турци\w+|Швейцари\w+|"
    r"Казахстан\w*|Польш\w+|Чехи\w+|Франци\w+|Великобритани\w+|"
    r"Испани\w+|Нидерланд\w+|Финлянди\w+|Швеци\w+|Австри\w+|Венгри\w+"
)
COUNTRY_TAIL_RE = re.compile(r"[,;]\s*(?:" + _COUNTRIES + r")\s*[.,]?$", re.I)


# Название целиком в кавычках и без кавычек внутри: реестр так пишет
# зарубежные заводы — «"КИРХНЕР & ВИЛЬГЕЛЬМ ГмбХ + Ко. КГ"». Кавычки при
# имени завода ничего не значат, а в отчёте выглядят как опечатка.
# У «ЗАО "ЗАВОД ЭМА"» кавычки внутри — их не трогаем.
_WRAPPED = re.compile(r'^"([^"]+)"$|^«([^«»]+)»$')


def unwrap_quotes(s: str) -> str:
    m = _WRAPPED.match((s or "").strip())
    return (m.group(1) or m.group(2)).strip() if m else s


def clean_company(s: str) -> str:

    # кавычки снимаем первыми: иначе они прикрывают хвост от чистки, и второй
    # проход по тому же названию даёт другой ответ, чем первый
    s = unwrap_quotes(" ".join((s or "").split()))
    for _ in range(3):
        before = s
        s = s.strip(' ,;.')
        s = re.sub(r'[,;]?\s+[^"()]{0,40}\(\d{3}\)$', "", s)
        s = re.sub(r'\s*/\s*[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s.-]{1,28}$', "", s)
        s = COUNTRY_TAIL_RE.sub("", s)
        if s == before:
            break
    if s.count('"') % 2:
        s = s + '"'
    return unwrap_quotes(s.strip(" ,;"))


def company_keys(name: str) -> set[str]:

    out: set[str] = set()
    for chunk in [name] + re.findall(r"\(([^)]+)\)", name or ""):
        chunk = re.sub(r"\([^)]*\)", " ", chunk)
        core = company_key(chunk)[0]
        if len(core) >= 6:
            out.add(core)
    return out


def company_key(s: str) -> tuple[str, str]:

    t = (s or "").lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я0-9]+", " ", t)
    words = t.split()
    opf = ""
    rest: list[str] = []
    for w in words:
        if not opf and w in OPF:
            opf = OPF[w]
            continue
        if w in OPF_TAIL:
            continue
        rest.extend(ABBREV.get(w, w).split())
    return "".join(rest), opf


_TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
             "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
             "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
             "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
             "ь": "", "э": "e", "ю": "yu", "я": "ya", "ё": "e"}
_PHON_PAIRS = (("qu", "kv"), ("ck", "k"), ("ch", "h"), ("sh", "s"), ("zh", "z"),
               ("ph", "f"), ("c", "k"), ("w", "v"), ("x", "ks"), ("y", "i"),
               ("ee", "i"), ("oo", "u"))


def phonetic(name: str) -> str:
    t = "".join(_TRANSLIT.get(c, c) for c in (name or "").lower())
    t = re.sub(r"[^a-z0-9]+", "", t)
    for a, b in _PHON_PAIRS:
        t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t)


def phonetic_variants(name: str) -> list[str]:

    parts = [name, re.sub(r"\([^)]*\)", " ", name or "")]
    parts += re.findall(r"\(([^)]+)\)", name or "")
    return [x for x in {phonetic(t) for t in parts} if len(x) >= 6]


def phonetic_close(a: str, b: str, threshold: float = 0.82) -> bool:
    from difflib import SequenceMatcher

    va, vb = phonetic_variants(a), phonetic_variants(b)
    return any(SequenceMatcher(None, x, y).ratio() >= threshold
               for x in va for y in vb)


def same_company(a: str, b: str) -> bool:
    """Один ли это завод, записанный по-разному. Нужен там, где два названия
    пришли из разных мест и надо решить, спор это или одно и то же.

    Три случая, и все три встретились на живом прогоне: ключи совпали;
    отличается только приписанная страна («Сателек САС» и «Сателек САС
    Франция» — реестр так пишет производственные площадки); короткое —
    аббревиатура длинного («ДЗМО» и «Досчатинский завод медицинского
    оборудования»).

    Чего здесь намеренно нет: вхождения одного названия в другое — «Медтехника»
    и «Медтехника плюс» бывают разными фирмами; и транслитерации — «Диксион» и
    «DIXION» разводит человек, а не программа."""

    ka, kb = company_key(a)[0], company_key(b)[0]
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ca, cb = company_key(_drop_country(a))[0], company_key(_drop_country(b))[0]
    if ca and ca == cb:
        return True
    return is_initialism(a, b) or is_initialism(b, a)


def _drop_country(name: str) -> str:
    from .nameparse import COUNTRY_RE

    words = (name or "").strip(" ,").split()
    while words and COUNTRY_RE.match(words[-1].strip(" ,\"'«»()")):
        words.pop()
    return " ".join(words)


def _core_words(name: str) -> list[str]:
    s = re.sub(r"^\s*(?:ООО|ОАО|ЗАО|АО|ПАО|НАО|УП|ЧП|ИП)\b\.?", " ",
               name or "", flags=re.I)
    s = re.sub(r"[«»\"'()]", " ", s)
    return [w for w in re.findall(r"[А-Яа-яЁёA-Za-z]+", s)
            if len(w) > 1 and w.lower() not in OPF_TAIL]


def is_initialism(short: str, full: str) -> bool:

    a, b = _core_words(short), _core_words(full)
    if len(b) < 2 or not a:
        return False
    if len(a) == 1:
        abbr = a[0]
        if len(abbr) < 3 or not abbr.isupper():
            return False
        return abbr == "".join(w[0] for w in b).upper()
    return _head_initialism(a, b)


def _head_initialism(a: list[str], b: list[str]) -> bool:

    if len(b) < 3 or len(a) >= len(b):
        return False
    tail = len(a) - 1
    if [w.lower() for w in a[-tail:]] != [w.lower() for w in b[-tail:]]:
        return False
    if len("".join(a[-tail:])) < 3:
        return False
    abbr, words = a[0], b[:len(b) - tail]
    if len(abbr) < 2 or not abbr.isupper() or len(words) < 2:
        return False
    return abbr == "".join(w[0] for w in words).upper()
