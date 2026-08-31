from __future__ import annotations

import json
import logging

from .config import DATA

log = logging.getLogger(__name__)

FILE = DATA / "timing.json"

# Пока своих замеров нет, берём осторожную оценку: контракт — это запрос
# карточки, запрос документа и запрос начальной цены, а темп к ЕИС
# ограничен примерно полутора запросами в секунду.
DEFAULT = 3.0
FLOOR, CEIL = 0.4, 40.0
SMOOTH = 0.3           # какую долю в среднем занимает свежий замер
FRESH_ENOUGH = 0.5     # прогон по кэшу быстрый, для оценки он не показателен

# Обещание перед запуском выходило вдвое короче правды: 612 контрактов —
# «около 29 минут», а прошло больше часа. «Сек_на_контракт» — средняя по
# прогону, и складывается она в основном из коротких прогонов, где ЕИС ещё
# отвечает быстро; на длинном ограничитель темпа притормаживает после
# каждого 429 и до прежней скорости уже не возвращается. Поэтому обещаем
# с запасом: закончить раньше обещанного человеку приятно, а вот число,
# которое по ходу дела растёт с 29 минут до 49, доверия не вызывает.
SLOWDOWN = 1.8


def per_contract() -> float:
    """Сколько секунд уходит на один контракт на этом компьютере."""

    try:
        value = float(json.loads(FILE.read_text(encoding="utf-8"))["сек_на_контракт"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT
    return min(CEIL, max(FLOOR, value))


def pace() -> float:
    """Секунд на контракт — то число, которое обещают человеку."""

    return per_contract() * SLOWDOWN


def record(contracts: int, seconds: float, fresh_share: float) -> None:
    """Запомнить темп закончившегося прогона."""

    if contracts < 3 or seconds <= 0 or fresh_share < FRESH_ENOUGH:
        return
    fresh = min(CEIL, max(FLOOR, seconds / contracts))
    value = round(per_contract() * (1 - SMOOTH) + fresh * SMOOTH, 3)
    try:
        FILE.write_text(json.dumps({"сек_на_контракт": value,
                                    "последний_замер": round(fresh, 3),
                                    "контрактов": contracts},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        log.debug("замер темпа не сохранён: %s", e)


def human(seconds: float) -> str:
    """«около 4 минут» — так, как сказал бы человек."""

    if seconds < 45:
        return "меньше минуты"
    minutes = int(round(seconds / 60))
    if minutes <= 1:
        return "около минуты"
    if minutes < 60:
        return f"около {minutes} {_plural(minutes, 'минуты', 'минут', 'минут')}"
    hours = minutes // 60
    rest = minutes % 60
    head = ("около часа" if hours == 1 else
            f"около {hours} {_plural(hours, 'часа', 'часов', 'часов')}")
    return head if rest < 10 else f"{head} {rest} мин"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)
