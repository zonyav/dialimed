from __future__ import annotations

import re
from typing import Iterable

from .nameparse import DESCRIPTIVE_RE

_TYPE_WORD_RE = re.compile(
    r"^(?:облучател\w*|рециркулятор\w*|обеззаражива\w*|измерител\w*|анализатор\w*|"
    r"монитор\w*|комплекс\w*|стерилизатор\w*|автоклав\w*|лампы?|дозатор\w*|"
    r"ингалятор\w*|небулайзер\w*|тонометр\w*|термометр\w*|коагулятор\w*|"
    r"стимулятор\w*|дефибриллятор\w*|отсасыватель\w*|насос\w*|весы|шкафы?|"
    r"кроват\w*|носилки|контейнер\w*|сумки?|пакеты?|бикс\w*|ширм\w*|"
    r"знаки?)$", re.I)

_DOC_PREFIX_RE = re.compile(
    r"^(?:рзн|p3h|p3н|фсз|фср|фс|ерул|ру|ту|гост|окпд|ктру|нкми|сгр|тр)$", re.I)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+(?:-[A-Za-zА-Яа-яЁё0-9]+)*")


def is_type_word(word: str, hints: Iterable[str] = ()) -> bool:

    w = (word or "").strip(" .,\"'«»-").lower()
    if not w:
        return True
    if _DOC_PREFIX_RE.match(w):
        return True
    for h in hints:
        if w in _WORD_RE.findall((h or "").lower()):
            return True
    parts = [p for p in re.split(r"[-–]", w) if any(c.isalpha() for c in p)]
    if not parts:
        return False
    return all(_TYPE_WORD_RE.match(p) or DESCRIPTIVE_RE.match(p) for p in parts)


_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    "×": "x", "✕": "x",
})


def norm_token(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "",
                  (s or "").translate(_HOMOGLYPHS).lower().replace("ё", "е"))


def articles(mark: str) -> set[str]:

    out = set()
    for tok in re.split(r"\s+", mark or ""):
        t = norm_token(tok)
        if t and re.search(r"\d", t):
            out.add(t)
    return out


# «10мм», «d=4мм», «302 мм» — это размер изделия, а не обозначение модели.
# Правила разбора вытаскивали такое из наименования по РУ («Эндоскоп d=10мм
# (исп.3)») и на этом останавливались: товарный знак в позицию уже не попадал,
# а перенос производителя шёл по ключу «10мм» — то есть по совпадению диаметра
# с чужим изделием. Единица измерения или приставка диаметра обязательны:
# голое число обозначением быть может («мод.8989»), размер — нет.
_MEASURE_RE = re.compile(
    r"^(?:(?P<pre>[dhlwдøØ⌀]|диам(?:етр)?\w*)\s*[=:]?\s*)?"
    r"\d+(?:[.,]\d+)?\s*"
    r"(?P<unit>мм|см|мкм|нм|м|дюйм\w*|fr|шр|°|град(?:ус\w*)?)?$", re.I)


def is_measure(value: str) -> bool:

    s = (value or "").strip(" .,:;\"'«»()")
    m = _MEASURE_RE.match(s)
    return bool(m) and bool(m.group("unit") or m.group("pre"))
