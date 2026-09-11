"""Сошлась ли позиция с тем, что искал человек.

Замерено на «Ajax AJ15» (28 строк по коду против 20 по модели): из десяти
позиций, которые поиск по модели потерял, во всех десяти написано «AJ15» и ни
в одной — «Ajax». Старое правило требовало все слова запроса сразу, поэтому
десять верных строк были отброшены, а одну («Товарный знак: AJ15») ЕИС даже
вернул — её выбросил уже отбор.

Отсюда два разряда слов вместо одного:

* **артикул** — слово с цифрой: «AJ15», «70П», «КС-02». Он и называет модель.
* **имя** — слово из букв: «Ajax», «Рускан», «Olympus». Оно называет семейство.

Приметный артикул («AJ15» — две буквы и цифры) стоит сам за себя: если он
сошёлся, имя рядом не обязательно, потому что заказчик его попросту не пишет.
Артикул, начинающийся с цифры («70П»), сам за себя не стоит — он встречается у
кого угодно, и имя для него остаётся обязательным. Ровно поэтому замер по
«рускан 70п» (224 строки) остаётся в силе: там правило не изменилось.

Второй разряд — **где** написан артикул, и это решает, уверенное совпадение
или сомнительное:

* в том, что заказчик выбрал (наименование, товарный знак, характеристики), —
  уверенно;
* в наименовании по РУ, где перечислены все исполнения регистрации
  («варианты исполнения: AJ11, AJ12, AJ15, AJ16, AJ18» — два таких контракта в
  корпусе из 57), — сомнительно: какое из них поставлено, из текста не видно;
* в наименовании по РУ, где названо одно исполнение («вариант исполнения:
  AJ15» — так написаны 9 потерянных строк из 10), — уверенно.

Сомнение не выбрасывает строку, а подписывает её: колонка «Совпадение с
запросом» говорит, почему строка в отчёте, и по ней же сомнительные строки
отбираются в Excel фильтром.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .spelling import key as _phon, to_latin

_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")

# «AJ 15» и «AJ-15» — тот же артикул, что «AJ15»: приставка отдельным словом
# встречается прямо в товарном знаке («Установка стоматологическая Ajax AJ 15»).
# Приставку длиннее пяти букв к числу не клеим — «исполнения 3» артикулом не
# является.
PREFIX_MAX = 5

# Артикул стоит сам за себя, если начинается хотя бы с двух букв: «AJ15»,
# «ЭГД70П», «KC02». «70П» начинается с цифры — такой без имени не ищем.
_STANDALONE = re.compile(r"^[a-z]{2,}\d")


def norm(word: str) -> str:
    """Слово в сравнимый вид: кириллица латиницей, регистр и знаки прочь.

    Через латиницу проходят и кириллические двойники: «АJ15», набранное с
    русской «А», — тот же артикул, что «AJ15»."""

    return re.sub(r"[^a-z0-9]+", "", to_latin(word or "").lower())


@dataclass(slots=True)
class Term:
    raw: str
    key: str


@dataclass(slots=True)
class Query:
    """Одна строка запроса, разобранная на имена и артикулы."""

    raw: str = ""
    names: list[Term] = field(default_factory=list)
    articles: list[Term] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.names or self.articles)

    @property
    def standalone(self) -> bool:
        """Есть ли артикул, которому имя не нужно."""

        return any(_STANDALONE.match(a.key) for a in self.articles)


def parse(phrase: str) -> Query:
    """Разобрать строку запроса на имена и артикулы.

    Приставка, отбитая пробелом или дефисом от числа, склеивается: «AJ 15» —
    один артикул, а не имя «AJ» и число «15»."""

    q = Query(raw=re.sub(r"\s+", " ", (phrase or "").strip()))
    spans = [m for m in _WORD.finditer(phrase or "")]
    words: list[str] = []
    i = 0
    while i < len(spans):
        w = spans[i].group(0)
        glue = _glue(phrase or "", spans, i)
        if glue:
            words.append(glue)
            i += 2
            continue
        words.append(w)
        i += 1
    for w in words:
        k = norm(w)
        if not k:
            continue
        if re.search(r"\d", k):
            q.articles.append(Term(raw=w, key=k))
        elif len(k) >= 3:
            q.names.append(Term(raw=w, key=_phon(w)))
    return q


def _glue(text: str, spans: list, i: int) -> str:
    """Приставка и число, стоящие рядом, — один артикул: «AJ 15», «AJ-15».

    Между ними допустим только пробел или дефис. Иначе склеивается то, что
    рядом не стоит: товарный знак «Ajax (№ 832695)» давал «ajax832695», а
    такой «артикул» потом читался как чужое исполнение семейства AJ."""

    if i + 1 >= len(spans):
        return ""
    w, nxt = spans[i].group(0), spans[i + 1].group(0)
    if not (w.isalpha() and len(w) <= PREFIX_MAX and re.match(r"^\d", nxt)):
        return ""
    between = text[spans[i].end():spans[i + 1].start()]
    return w + nxt if between in ("", " ", "-", "‑", "–") else ""


def article_keys(text: str) -> set[str]:
    """Артикулы, написанные в тексте, — в сравнимом виде.

    Склейка та же, что в запросе: «AJ 15» и «AJ-15» дают «aj15»."""

    out: set[str] = set()
    spans = [m for m in _WORD.finditer(text or "")]
    for i, m in enumerate(spans):
        k = norm(m.group(0))
        if k and re.search(r"\d", k):
            out.add(k)
        glue = _glue(text or "", spans, i)
        if glue:
            out.add(norm(glue))
    return out


def _name_flat(text: str) -> str:
    return " ".join(k for k in (_phon(w) for w in _WORD.findall(text or "")) if k)


def _has_article(key: str, keys: set[str]) -> bool:
    """Артикул засчитывается и внутри длинного обозначения: «70п» в «ЭГД-70П».

    Иначе модель, слипшаяся с типом изделия, не находилась бы никогда."""

    return any(key == k or key in k for k in keys)


def _family(key: str) -> str:
    """Буквенная приставка артикула: у «AJ15» это «aj», у «70p» — пусто.

    Сравнивается она целиком, а не началом: «ajax832695» и «aj11» — разные
    семейства, хотя одно начинается с другого."""

    m = re.match(r"^([a-z]*)\d", key or "")
    return m.group(1) if m else ""


def _siblings(key: str, keys: set[str]) -> int:
    """Сколько ещё исполнений того же семейства перечислено рядом.

    «AJ15» среди «AJ11, AJ12, AJ15, AJ16, AJ18» — пять: регистрация названа
    целиком, и какое исполнение поставлено, текст не говорит."""

    p = _family(key)
    if not p:
        return 1
    return len({k for k in keys if _family(k) == p})


def _sibling_chosen(key: str, chosen: set[str]) -> bool:
    """Названо ли в выбранном заказчиком другое исполнение того же семейства.

    Это единственный случай, когда артикул в наименовании по РУ ничего не
    значит: регистрация перечисляет все исполнения, а купили то, что написано
    в товарном знаке. Правило бесплатное и не требует ни реестра, ни модели."""

    p = _family(key)
    if len(p) < 2:
        return False
    return any(k != key and _family(k) == p for k in chosen)


@dataclass(slots=True)
class Verdict:
    """Сошлась ли позиция и насколько уверенно."""

    ok: bool = False
    note: str = ""
    variant_list: bool = False    # артикул только в перечне исполнений РУ
    brand_unnamed: bool = False   # сошёлся артикул, имени нигде нет
    brand_known: bool = False     # имени в контракте нет, но так зовут завод

    @property
    def doubt(self) -> str:
        """Чем строка неточна — одним словом, для счётчиков сводки."""

        if not self.ok:
            return ""
        if self.variant_list:
            return "исполнение"
        return "бренд" if self.brand_unnamed else ""

    @property
    def rank(self) -> int:
        """Чем меньше, тем лучше: из нескольких строк запроса берём лучшую."""

        if not self.ok:
            return 3
        return 1 if self.doubt else 0


def judge(phrase: str, chosen: str, family: str = "", known: str = "") -> Verdict:
    """Сошлась ли позиция с одной строкой запроса.

    `chosen` — что заказчик выбрал: наименование, товарный знак,
    характеристики. `family` — наименование по РУ: там регистрация названа так,
    как её записал производитель, иногда со всеми исполнениями сразу. `known` —
    что о позиции сказал реестр РЗН: название завода и держателя. Оно приходит
    позже самого отбора, поэтому им имя бренда только подтверждается, но
    никогда не ищется модель: реестр называет изделие по-своему."""

    q = parse(phrase)
    if q.empty:
        return Verdict()

    chosen_arts = article_keys(chosen)
    family_arts = article_keys(family)
    all_names = _name_flat(chosen) + " " + _name_flat(family)

    variant_list = False
    for a in q.articles:
        if _has_article(a.key, chosen_arts):
            continue
        if _sibling_chosen(a.key, chosen_arts):
            # заказчик назвал исполнение, и оно другое: «варианты исполнения:
            # AJ11, AJ12, AJ15, AJ16, AJ18» в наименовании по РУ — это вся
            # регистрация, а товарный знак «AJ11» — то, что покупают
            return Verdict()
        if _has_article(a.key, family_arts):
            if _siblings(a.key, family_arts) > 1:
                variant_list = True
            continue
        return Verdict()

    named = all(n.key and n.key in all_names for n in q.names)
    if named:
        return Verdict(ok=True, variant_list=variant_list,
                       note=_note(q.raw, variant_list, False, False))
    # имя не названо. Терпимо только тогда, когда за модель отвечает приметный
    # артикул: он и есть ответ на запрос
    if not (q.articles and q.standalone):
        return Verdict()
    registry = bool(known) and all(
        n.key and n.key in _name_flat(known) for n in q.names)
    return Verdict(ok=True, variant_list=variant_list,
                   brand_unnamed=not registry, brand_known=registry,
                   note=_note(q.raw, variant_list, not registry, registry))


def _note(raw: str, variant_list: bool, brand_unnamed: bool,
          brand_known: bool) -> str:
    """Строка для колонки «Совпадение с запросом» — по-русски и по делу."""

    why = []
    if variant_list:
        why.append("модель только в перечне исполнений РУ")
    if brand_unnamed:
        why.append("бренд в контракте не назван")
    if brand_known:
        why.append("бренд не назван в контракте, но так зовут производителя")
    return f"{raw}: {', '.join(why)}" if why else raw


def judge_any(phrases: list[str], chosen: str, family: str = "",
              known: str = "") -> Verdict:
    """Лучшее совпадение из нескольких строк запроса.

    Строки в поле складываются как «или»: двумя брендами иначе не искать."""

    best = Verdict()
    for p in phrases:
        if not (p or "").strip():
            continue
        v = judge(p, chosen, family, known)
        if v.rank < best.rank:
            best = v
            if best.rank == 0:
                break
    return best
