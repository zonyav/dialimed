"""Агентный поиск производителя по реестру Росздравнадзора.

Модель здесь **не источник фактов**. Она умеет одно: придумывать запросы к
реестру и, найдя подходящую запись, назвать её номер РУ и дословную цитату.
Цитату сверяет программа, номер перезапрашивается в реестре, а производитель,
держатель и ИНН берутся из ответа реестра — не из слов модели. Не прошла хоть
одна проверка — ячейка остаётся пустой.

Почему именно так: выбор из готового среза по коду вида упирается в потолок —
в 37% случаев нужной записи в срезе нет вообще, и там модель либо молчит, либо
выдумывает. Свободный поиск по всему реестру дал 97–98% попаданий на двух
независимых наборах КТРУ.

Модуль ничего не делает, пока пользователь не включит ИИ и не введёт свой ключ.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# Промпт даёт те самые 97–98%: он объясняет, что поиск идёт по вхождению
# подстроки в наименование, и что артикул в реестре не ищется. Правки здесь
# меняют точность — их надо перемерять, а не вносить на глаз.
PROMPT = """Ты определяешь производителя медизделия по тексту позиции из госконтракта.
У тебя есть поиск по реестру регистрационных удостоверений Росздравнадзора.
Ищет он вхождением подстроки и по двум разным полям: по НАИМЕНОВАНИЮ изделия
и по НАЗВАНИЮ завода (производителя или держателя РУ).

Отвечай ТОЛЬКО одним JSON-объектом, без пояснений:
  {"поиск": "строка"} — сделать запрос к реестру по наименованию;
  {"завод": "строка"} — спросить все регистрации завода;
  {"ответ": "<номер РУ из найденной записи>", "цитата": "<кусок записи реестра,
   дословно, который доказывает совпадение>", "уверенность": "высокая|средняя|низкая"}
  {"ответ": null, "почему": "коротко"} — если доказательства нет.

Как искать:
- поиск идёт по ТОЧНОМУ вхождению подстроки, поэтому ищи КОРОТКИЕ куски:
  одно-два слова, без кавычек, скобок, ® и «»; длинная фраза почти всегда даёт ноль;
- в реестре изделие называется по-своему: «Трубки оптические», «Инструменты
  эндоскопические», «Эндоскопы жесткие» — пробуй такие формы, а не слово из КТРУ;
- товарного знака в наименовании обычно НЕТ: реестр пишет «Гистероскоп по ТУ
  26.60.12-004-65500831-2024», а знак ESTEN стоит только в названии завода —
  ООО «ЭСТЭН». Знак и название линейки поэтому ищи через {"завод": ...},
  а не через {"поиск": ...};
- завод в реестре чаще записан кириллицей: BISSINGER — это «Гюнтер Биссингер
  Медицинтехник ГмбХ», ESTEN — ООО «ЭСТЭН». Если латиница не дала ничего,
  попробуй кириллическое написание того же имени;
- название линейки (EndoGlance, Arthrex) в наименовании встречается — его
  можно искать и через {"поиск": ...};
- запрос из одного общего слова вернёт слишком много — уточняй;
- артикул (Т01-100-320-30) в поиске НЕ находится: он спрятан в вариантах
  исполнения. Найди изделие по названию, а артикул сверь в самой записи.
Внимание: в реестре и в контракте одно и то же может писаться по-разному —
буква «О» вместо нуля, латиница вместо кириллицы.

Отвечай номером РУ только тогда, когда в записи есть то, что названо
в контракте: артикул, товарный знак, линейка или сам завод. Совпадение вида
изделия доказательством НЕ является. Пустой ответ лучше неверного."""

SPECS_CHARS = 400
# Выдача реестра — главный вес каждого шага: она уезжает модели заново при
# каждом следующем вопросе. Резать её надо числом записей, а не их содержимым:
# при 300 символах вариантов исполнения прогон подешевел, но нашёл на три
# строки меньше — артикул подтверждается именно по этому перечню, и обрезанный
# перечень заставляет модель молчать.
VARIANTS_CHARS = 700
# У завода регистраций бывают десятки; показываем первые — этого хватает, чтобы
# узнать изделие, и не хватает, чтобы выдача стала дороже самого вопроса.
FIRM_SHOW = 10
MAX_TOKENS = 700
SOURCE = "ИИ + реестр РЗН"

# Номер уговора с моделью. Ответы лежат в кэше вечно — они оплачены, и
# повторный прогон обязан дать тот же отчёт. Но ответ, данный по прежнему
# промпту, отвечает уже на другой вопрос: с появлением поиска по заводу
# молчание перестало значить «в реестре этого нет». Поэтому номер входит в
# ключ: прежние ответы остаются в кэше, но новых вопросов не закрывают.
PROTOCOL = "2"

# Имя пространства в кэше. Ответы уговора №2 записаны с пустым именем — так
# сложилось, и переименовывать их значит выбросить оплаченное. Начиная со
# следующего уговора номер входит в ключ; у проходов, появившихся позже,
# имя своё (см. VARIANT_KIND).
NAMESPACE = "" if PROTOCOL == "2" else PROTOCOL


@dataclass(slots=True)
class AiAnswer:
    """Что модель ответила про одну позицию, до всяких проверок."""

    ru_number: str = ""
    quote: str = ""
    confidence: str = ""
    why: str = ""
    steps: int = 0
    error: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.ru_number)


# ── чистые проверки: их можно гонять без сети ───────────────────────────────

def flat(s: str) -> str:
    """Текст без всего, что мешает сравнению: регистр, ё, кавычки, ®, дефисы."""

    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-я]+", " ", s)).strip()


def record_text(rec) -> str:
    """Вся запись реестра одной строкой — по ней сверяется цитата: наименование,
    варианты исполнения, номер, завод и держатель."""

    return " ".join(str(getattr(rec, name, "") or "") for name in
                    ("ru_name", "models_description", "ru_number", "producer",
                     "producer_eng", "declarant"))


def quote_supported(quote: str, text: str) -> bool:
    """Цитата должна найтись в самой записи. Слова короче четырёх букв не в
    счёт — они совпадают у всего подряд; из длинных должна найтись хотя бы
    половина. Именно эта проверка ловит выдумку."""

    haystack = flat(text)
    if not haystack:
        return False
    words = [w for w in flat(quote).split() if len(w) >= 4]
    if not words:
        needle = flat(quote)
        return bool(needle) and needle in haystack
    hits = sum(1 for w in words if w in haystack)
    return hits * 2 >= len(words)


def mask_number(text: str, number: str) -> str:
    """Прячет номер РУ из текста позиции — так строка с известным ответом
    превращается в проверочную задачу для самопроверки прогона."""

    if not number:
        return text
    body = re.sub(r"^\s*(?:ФСР|ФС|РЗН|ЕРУЛ|РД)\s*", "", number, flags=re.I).strip()
    out = text
    for piece in (number, body):
        if len(piece) >= 4:
            out = re.sub(re.escape(piece), "…", out, flags=re.I)
    return out


def parse_reply(raw: str) -> dict:
    """Ответ модели — один JSON-объект. Иногда он приходит в ```-заборе или с
    пояснением вокруг, поэтому берём первый объект, а не весь текст."""

    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    start, depth = s.find("{"), 0
    if start < 0:
        return {}
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return {}
                return obj if isinstance(obj, dict) else {}
    return {}


def question(text: str, specs: str = "") -> str:
    """Всё, что модель видит о позиции: текст из контракта и характеристики."""

    parts = [text.strip()]
    if specs:
        parts.append("Характеристики: " + specs[:SPECS_CHARS])
    return "\n".join(p for p in parts if p)


# ── какое исполнение закупается ────────────────────────────────────────────

# Второй, совсем другой вопрос к модели: не «где искать в реестре», а «что
# написано в самом контракте». Правила разбирают текст по словам и потому
# спотыкаются на живом языке — «в комплектации AJ-15», «исп. 15», «Вариант
# исполнения» в характеристиках. Модель читает, а не токенизирует.
#
# Источником факта она и здесь не становится: ответ принимается, только если
# цитата дословно нашлась в тексте позиции и обозначение действительно стоит
# в этой цитате. Что искал человек, модели не говорят — иначе она будет
# соглашаться с подсказкой; сравнивает программа, уже после ответа.
VARIANT_KIND = "исполнение-1"

VARIANT_PROMPT = """Ты читаешь позицию из российского госконтракта и отвечаешь на один вопрос:
какое исполнение (вариант, модель, артикул) изделия закупается по этой позиции.

Отвечай ТОЛЬКО одним JSON-объектом, без пояснений:
  {"исполнение": "<обозначение так, как оно написано в тексте>",
   "цитата": "<дословный кусок текста, где это написано>"}
  {"исполнение": null, "почему": "коротко"} — если в тексте этого нет.

Правила:
- бери только написанное. Не выводи исполнение из состава, характеристик или
  из того, что бывает у этого производителя;
- «варианты исполнения: AJ11, AJ12, AJ15» — это перечень всей регистрации, а
  не выбор заказчика. Если ничего другого в тексте нет, отвечай null;
- выбранное исполнение обычно стоит в наименовании позиции, в товарном знаке
  или в характеристиках («Вариант исполнения: AJ15», «в комплектации AJ-15»);
- цитата должна быть дословной: по ней программа проверяет ответ.
  Пустой ответ лучше выдуманного."""


@dataclass(slots=True)
class VariantAnswer:
    """Что модель вычитала про исполнение — до проверок."""

    variant: str = ""
    quote: str = ""
    why: str = ""
    error: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.variant)


async def pick_variant(text: str, ask: Ask) -> VariantAnswer:
    """Один вопрос, один ответ: никаких поисков, никакого диалога."""

    messages = [{"role": "system", "content": VARIANT_PROMPT},
                {"role": "user", "content": text}]
    try:
        raw = await ask(messages)
    except Exception as e:
        log.debug("шлюз ИИ (исполнение): %s: %s", type(e).__name__, e)
        return VariantAnswer(error=f"{type(e).__name__}: {e}")
    reply = parse_reply(raw)
    if not reply:
        return VariantAnswer(error="ответ модели не разобран")
    value = reply.get("исполнение")
    if not value:
        return VariantAnswer(why=str(reply.get("почему") or ""))
    return VariantAnswer(variant=str(value).strip(),
                         quote=str(reply.get("цитата") or ""))


def variant_supported(answer: VariantAnswer, text: str) -> bool:
    """Цитата — из текста позиции, и обозначение стоит в самой цитате.

    Первое ловит выдумку, второе — цитату не по делу: без него сгодился бы
    любой кусок текста, а названо в нём было бы что угодно."""

    from ..query import article_keys, norm

    if not (answer.variant and answer.quote):
        return False
    if not quote_supported(answer.quote, text):
        return False
    key = norm(answer.variant)
    if not key:
        return False
    return any(key == k or key in k for k in article_keys(answer.quote))


def _found(search, limit: int = 0) -> str:
    """Выдача реестра для модели: только то, по чему она может судить."""

    if not search.answered:
        return json.dumps({"ошибка": "реестр не ответил"}, ensure_ascii=False)
    if search.too_broad:
        return json.dumps({"ошибка": "слишком общий запрос, уточните",
                           "найдено": search.total}, ensure_ascii=False)
    records = []
    for it in (search.items[:limit] if limit else search.items):
        prod = it.get("producer") or {}
        decl = it.get("declarant") or {}
        records.append({
            "РУ": it.get("noRu") or "",
            "наименование": it.get("name") or "",
            "производитель": prod.get("name") or "",
            "держатель": decl.get("name") or "",
            "варианты исполнения": (it.get("modelsDescription") or "")[:VARIANTS_CHARS],
        })
    return json.dumps({"найдено": search.total if search.total is not None
                       else len(records), "записи": records}, ensure_ascii=False)


# ── диалог ─────────────────────────────────────────────────────────────────

Ask = Callable[[list[dict]], Awaitable[str]]
Search = Callable[[str], Awaitable[object]]


async def identify(text: str, ask: Ask, search: Search,
                   search_firm: Optional[Search] = None,
                   *, max_steps: int = 0) -> AiAnswer:
    """Цикл «запрос → выдача → ответ».

    Шагов не больше отведённого: платим мы за каждый, а каждый следующий
    дороже предыдущего — вся переписка вместе с выдачей реестра уезжает
    модели заново. Дороже всего обходятся диалоги, которые упираются в
    предел, и они же не приносят ничего. Поэтому два предохранителя:

    · повторный запрос — значит новых мыслей нет, и дальше будет то же;
    · три пустых поиска подряд — искать больше нечем.

    Оба обрывают диалог там, где он уже кончился, но продолжает стоить денег.

    Поисков два — по наименованию изделия и по названию завода. Второй нужен
    ровно потому, что товарный знак из контракта в наименовании реестра почти
    никогда не встречается, зато стоит в названии завода."""

    steps = max_steps or settings.ai_steps
    messages = [{"role": "system", "content": PROMPT},
                {"role": "user", "content": text}]
    asked: set[str] = set()
    empty = 0
    for step in range(1, steps + 1):
        try:
            raw = await ask(messages)
        except Exception as e:                      # шлюз недоступен или ответил ошибкой
            log.debug("шлюз ИИ: %s: %s", type(e).__name__, e)
            return AiAnswer(steps=step, error=f"{type(e).__name__}: {e}")
        reply = parse_reply(raw)
        if not reply:
            return AiAnswer(steps=step, error="ответ модели не разобран")
        by_firm = bool(reply.get("завод")) and search_firm is not None
        if reply.get("поиск") or by_firm:
            query = str(reply["завод"] if by_firm else reply["поиск"])
            key = ("завод:" if by_firm else "") + flat(query)
            if key in asked:
                return AiAnswer(steps=step, why="повторяет прежний запрос")
            asked.add(key)
            found = await (search_firm(query) if by_firm else search(query))
            empty = 0 if getattr(found, "items", None) else empty + 1
            if empty >= 3:
                return AiAnswer(steps=step, why="три поиска подряд впустую")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user",
                             "content": _found(found, FIRM_SHOW if by_firm else 0)})
            continue
        number = reply.get("ответ")
        if not number:
            return AiAnswer(steps=step, why=str(reply.get("почему") or ""))
        return AiAnswer(ru_number=str(number).strip(),
                        quote=str(reply.get("цитата") or ""),
                        confidence=str(reply.get("уверенность") or ""),
                        steps=step)
    return AiAnswer(steps=steps, why="не уложился в отведённые шаги")


# ── шлюз ───────────────────────────────────────────────────────────────────

class Gateway:
    """Обращение к шлюзу, совместимому с OpenAI. Без SDK: одна библиотека
    httpx уже в сборке, а лишний пакет — это лишние мегабайты в exe.

    Прокси httpx подхватывает из окружения сам: прямого доступа к шлюзу из
    России нет, и без прокси все запросы честно упадут с внятной ошибкой."""

    def __init__(self, key: str, model: str, base: str, *,
                 timeout: float = 0.0, concurrency: int = 0):
        self.key = re.sub(r"^bearer\s+", "", (key or "").strip(), flags=re.I)
        self.model = model or settings.ai_model
        self.base = (base or settings.ai_base).rstrip("/")
        self.timeout = timeout or settings.ai_timeout
        self._sem = asyncio.Semaphore(concurrency or settings.ai_concurrency)
        self._client: Optional[httpx.AsyncClient] = None
        # считаем запросы и токены: пользователь тратит свои деньги и вправе
        # видеть, во что обошёлся прогон, не заходя на сайт шлюза
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        # ключ уезжает в заголовок HTTP, а туда пролезает только латиница:
        # кириллица в нём роняла запрос вместо внятного ответа
        self.problem = ""
        if not self.key:
            self.problem = "Ключ не задан."
        elif not self.key.isascii():
            self.problem = ("В ключе есть русские буквы — скорее всего он "
                            "скопирован не целиком или вместе с лишним текстом.")

    async def __aenter__(self) -> "Gateway":
        if not self.problem:
            self._client = httpx.AsyncClient(
                base_url=self.base,
                headers={"Authorization": f"Bearer {self.key}",
                         "Content-Type": "application/json"},
                timeout=httpx.Timeout(self.timeout, connect=20.0),
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ask(self, messages: list[dict], *, attempts: int = 3) -> str:
        """Один вопрос модели. На прогонах 5–10% запросов падали и проходили
        со второй попытки — поэтому повтор, а не отказ с первого раза. Кнопка
        «Проверить» просит одну попытку: человек ждёт ответа, а не трёх."""

        if self._client is None:
            raise RuntimeError(self.problem or "ключ для ИИ не задан")
        body = {"model": self.model, "messages": messages,
                "temperature": 0, "max_tokens": MAX_TOKENS}
        last = ""
        async with self._sem:
            for attempt in range(max(1, attempts)):
                try:
                    r = await self._client.post("/chat/completions", json=body)
                    r.raise_for_status()
                    self.calls += 1
                    data = r.json()
                    usage = data.get("usage") or {}
                    self.tokens_in += int(usage.get("prompt_tokens") or 0)
                    self.tokens_out += int(usage.get("completion_tokens") or 0)
                    return (((data.get("choices") or [{}])[0].get("message") or {})
                            .get("content") or "")
                except Exception as e:
                    last = f"{type(e).__name__}: {e}"
                    # шлюз объясняет отказ в теле ответа: без него остаётся
                    # голый номер ошибки, по которому непонятно, что делать
                    body_text = getattr(getattr(e, "response", None), "text", "")
                    if body_text:
                        last += f" | {body_text[:300]}"
                    log.debug("шлюз ИИ, попытка %d: %s", attempt + 1, last)
                    if attempt < attempts - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(last or "шлюз не ответил")

    def spent(self) -> dict:
        """Во что обошёлся прогон. Цену берём из справочника моделей; для
        незнакомой модели показываем только запросы и токены."""

        from ..config import ai_price

        out = {"запросов к ИИ": self.calls,
               "токенов": self.tokens_in + self.tokens_out}
        cin, cout = ai_price(self.model)
        if cin or cout:
            usd = (self.tokens_in * cin + self.tokens_out * cout) / 1_000_000
            out["стоило"] = f"${usd:.2f}" if usd >= 0.01 else "меньше цента"
        return out

    async def probe(self) -> tuple[bool, str]:
        """Кнопка «Сохранить и проверить» на странице: работает ли ключ прямо
        сейчас. Одна попытка вместо трёх — ответ нужен за секунды."""

        if self.problem:
            return False, self.problem
        try:
            await self.ask([{"role": "user", "content": "Ответь одним словом: готов"}],
                           attempts=1)
        except Exception as e:
            return False, self.explain(str(e))
        return True, f"Ключ работает, модель {self.model} отвечает."

    @staticmethod
    def explain(text: str) -> str:
        """Отказ шлюза — по-русски и про то, что делать. «Ошибка» без причины
        не говорит человеку ничего: одна и та же надпись стоит и за опечаткой
        в ключе, и за пустым счётом, и за выключенным прокси."""

        low = text.lower()
        def has(*words: str) -> bool:
            return any(w in low for w in words)

        if has("connecterror", "connecttimeout", "proxy", "getaddrinfo",
               "name or service"):
            return ("Шлюз не открывается. Из России он доступен только через "
                    "прокси — включите его и попробуйте снова.")
        if has("sslerror", "certificate"):
            return ("Соединение со шлюзом обрывается на проверке сертификата — "
                    "так обычно ведёт себя антивирус или сетевой фильтр.")
        if has("readtimeout", "timeout"):
            return "Шлюз не ответил вовремя. Попробуйте ещё раз через минуту."
        if has("401", "403", "invalid api key", "unauthorized"):
            return ("Шлюз не принял ключ: он неверный или уже отозван. "
                    "Скопируйте его на сайте шлюза целиком и вставьте заново.")
        if has("402", "insufficient", "balance", "quota", "credit"):
            return ("Ключ верный, но на счёте шлюза нет денег — пополните его "
                    "на api.odirouter.ai.")
        if has("429", "rate limit"):
            return "Шлюз просит подождать: слишком много запросов подряд."
        if has("404") or ("model" in low and "not" in low):
            return "Шлюз не знает выбранную модель — выберите другую в списке."
        if has("500", "502", "503", "504", "bad gateway"):
            return "Сломался сам шлюз, а не ключ. Попробуйте позже."
        return f"Шлюз ответил ошибкой: {text[:200]}"


# ── кэш ответов модели ─────────────────────────────────────────────────────

class AiCache:
    """Ответы модели на диске: повторный прогон по тем же позициям бесплатен
    и, что важнее, повторяем — иначе отчёт нельзя было бы воспроизвести."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._db: Optional[sqlite3.Connection] = None
        db = self._conn()
        db.execute("""CREATE TABLE IF NOT EXISTS ai (
                          key TEXT PRIMARY KEY, payload TEXT, ts INTEGER)""")
        db.commit()

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @staticmethod
    def _key(model: str, text: str, kind: str = NAMESPACE) -> str:
        head = f"{kind}\x00{model}" if kind else model
        return hashlib.sha1(f"{head}\x00{text}".encode("utf-8")).hexdigest()

    async def get(self, model: str, text: str,
                  kind: str = NAMESPACE) -> Optional[AiAnswer]:
        async with self._lock:
            return await asyncio.to_thread(self._get, model, text, kind)

    def _get(self, model: str, text: str,
             kind: str = NAMESPACE) -> Optional[AiAnswer]:
        row = self._conn().execute("SELECT payload FROM ai WHERE key=?",
                                   (self._key(model, text, kind),)).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return AiAnswer(**{k: data.get(k, "") for k in
                           ("ru_number", "quote", "confidence", "why")},
                        steps=int(data.get("steps") or 0))

    async def put(self, model: str, text: str, answer: AiAnswer,
                  kind: str = NAMESPACE) -> None:
        async with self._lock:
            await asyncio.to_thread(self._put, model, text, answer, kind)

    def _put(self, model: str, text: str, answer: AiAnswer,
             kind: str = NAMESPACE) -> None:
        payload = {"ru_number": answer.ru_number, "quote": answer.quote,
                   "confidence": answer.confidence, "why": answer.why,
                   "steps": answer.steps}
        db = self._conn()
        db.execute("INSERT OR REPLACE INTO ai(key, payload, ts) VALUES (?,?,?)",
                   (self._key(model, text, kind),
                    json.dumps(payload, ensure_ascii=False), int(time.time())))
        db.commit()
