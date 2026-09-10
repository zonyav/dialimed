"""Написания одного и того же названия.

Поиск ЕИС ищет буквальное вхождение: «ESTEN» не находит контракт, где написано
«ЭСТЕН», и наоборот. Замерено на разобранном наборе из 49 контрактов с этим
знаком: латинское написание нашло 47, кириллическое — оставшиеся два,
объединение даёт все 49. Поэтому запрос идёт двумя написаниями, а не одним.

Угадывать чужую орфографию дальше этого («ЭСТЭН» через «Э» посередине)
бессмысленно — для таких случаев пользователь пишет своё написание отдельной
строкой, и она ищется наравне с остальными.

Тот же модуль решает обратную задачу — сошлась ли позиция с запросом. Там
сравниваются звуковые ключи, поэтому «Рускан», «РУСКАН» и «RuScan» — одно
слово, а «70П» и «70P» — один артикул.
"""

from __future__ import annotations

import re

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Латиница в кириллицу — по звуку. Двухбуквенные сочетания идут первыми,
# иначе «sh» превратится в «сх».
_LAT_TO_CYR = (
    ("shch", "щ"), ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"),
    ("kh", "х"), ("ts", "ц"), ("yu", "ю"), ("ya", "я"), ("ph", "ф"),
    ("th", "т"), ("ck", "к"), ("qu", "кв"), ("ee", "и"), ("oo", "у"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
    ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"),
    ("z", "з"),
)

_VOWELS_LAT = set("aeiouy")

_HAS_CYR = re.compile(r"[а-яёА-ЯЁ]")
_HAS_LAT = re.compile(r"[a-zA-Z]")
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


def to_latin(text: str) -> str:
    """Кириллица латиницей по звуку: «РУСКАН» -> «RUSKAN»."""

    out: list[str] = []
    for ch in text or "":
        low = _CYR_TO_LAT.get(ch.lower())
        if low is None:
            out.append(ch)
        else:
            out.append(low.upper() if ch.isupper() else low)
    return "".join(out)


def _y_to_cyr(word: str) -> str:
    """«y» после гласной — это «й» (Bayer), в остальных местах «и» (Olympus)."""

    out: list[str] = []
    for i, ch in enumerate(word):
        if ch != "y":
            out.append(ch)
            continue
        prev = word[i - 1] if i else ""
        out.append("й" if prev in _VOWELS_LAT else "и")
    return "".join(out)


def to_cyrillic(text: str) -> str:
    """Латиница кириллицей по звуку: «ESTEN» -> «ЭСТЕН».

    Первая «e» слова становится «э» — так пишут заимствования («Эстония»,
    «Эксперт»), и именно так записан завод, который в контракте назван ESTEN.
    """

    out: list[str] = []
    for part in re.split(r"(\W+)", text or ""):
        if not part or not _HAS_LAT.search(part):
            out.append(part)
            continue
        upper = part.isupper() and len(part) > 1
        body = _y_to_cyr(part.lower())
        for a, b in _LAT_TO_CYR:
            body = body.replace(a, b)
        if part.lower().startswith("e") and body.startswith("е"):
            body = "э" + body[1:]
        out.append(body.upper() if upper else body)
    return "".join(out)


def spellings(phrase: str, limit: int = 3) -> list[str]:
    """Написания фразы, которыми есть смысл спросить ЕИС.

    Первым идёт то, что набрал пользователь: даже если перевод в другой
    алфавит окажется неудачным, свой запрос он получит."""

    phrase = re.sub(r"\s+", " ", (phrase or "").strip())
    if not phrase:
        return []
    out = [phrase]

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", (value or "").strip())
        if value and value.lower() not in {x.lower() for x in out}:
            out.append(value)

    if _HAS_CYR.search(phrase):
        add(to_latin(phrase))
    if _HAS_LAT.search(phrase):
        add(to_cyrillic(phrase))
    return out[:limit]


def key(word: str) -> str:
    """Слово в одном виде для сравнения — звуковой ключ.

    Берём тот же `verify.phonetic`, которым сверяются названия заводов: он
    приводит кириллицу к латинице и сглаживает «c/k», «w/v», «y/i» и удвоения,
    так что «RuScan» и «Рускан» сходятся."""

    from .enrich.verify import phonetic

    return phonetic(word or "")


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text or "") if w]


def phrase_matches(phrase: str, text: str) -> bool:
    """Есть ли в тексте все слова запроса — в любом написании.

    Слово запроса засчитывается и как часть длинного слова текста («70п» в
    «ЭГД-70П»), иначе модель, слипшаяся с обозначением, не находилась бы
    никогда."""

    want = [k for k in (key(w) for w in words(phrase)) if k]
    if not want:
        return False
    have = [k for k in (key(w) for w in words(text)) if k]
    if not have:
        return False
    flat = " ".join(have)
    return all(w in flat for w in want)


def any_match(phrases: list[str], text: str) -> bool:
    """Позиция подходит, если сошлась хотя бы с одним запросом: несколько
    строк в поле — это «или», иначе двумя брендами не искать."""

    return any(phrase_matches(p, text) for p in phrases if p.strip())


def search_words(phrase: str) -> list[str]:
    """Чем спрашивать ЕИС про эту фразу.

    Спрашивать целой фразой нельзя — замерено: «рускан 70п» даёт 30
    контрактов, «рускан» — 645, и в 14 случаях из 40 проверенных «лишних»
    модель РуСкан 70П в позиции есть. Слова в поиске ЕИС соединяются «и», но
    «70П» как отдельное слово находится далеко не везде, где оно написано:
    в товарном знаке «РуСкан 70П» индексируется, судя по всему, только имя.
    Дефис ломает поиск совсем — «ЭГД-70П» возвращает ноль.

    Поэтому в ЕИС уходит слово, а модель дальше отбирается уже по тексту
    позиции. Слова короче трёх букв и голые числа не спрашиваем: «CV» или
    «30» вернут пол-реестра и ничего не сузят."""

    words = [w for w in _WORD.findall(phrase or "")
             if len(w) >= 3 and re.match(r"[A-Za-zА-Яа-яЁё]", w)]
    return words or ([phrase.strip()] if (phrase or "").strip() else [])
