from __future__ import annotations

import json
import logging

from .config import DATA
from .enrich.rzn import latinize, producer_key

log = logging.getLogger(__name__)

FILE = DATA / "producers.json"


def key(name: str) -> str:
    """Ключ написания: без организационной формы, кавычек и регистра.

    «ООО "ДИКСИОН"», «Диксион, ООО» и «DIXION» сводятся к одному ключу —
    по нему одинаковые фирмы объединяются сами, без участия человека.
    """

    # сначала снимаем организационную форму, потом сводим похожие
    # буквы кириллицы и латиницы — иначе «ООО» не распознаётся
    return latinize(producer_key(name or ""))


def load() -> dict[str, list[str]]:
    """Объединения, сделанные руками: имя группы -> написания."""

    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    groups = data.get("группы")
    if not isinstance(groups, dict):
        return {}
    return {str(t): [str(x) for x in names]
            for t, names in groups.items() if isinstance(names, list)}


def save(groups: dict[str, list[str]]) -> None:
    try:
        FILE.write_text(
            json.dumps({"версия": 1, "группы": groups}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError as e:
        log.warning("объединения производителей не сохранены: %s", e)


def index() -> dict[str, str]:
    """Ключ написания -> имя группы, куда его отнесли руками."""

    out: dict[str, str] = {}
    for title, names in load().items():
        for name in names:
            out[key(name)] = title
    return out


def merge(names: list[str], title: str = "") -> str:
    """Свести написания в одну группу. Имя по умолчанию — первое из списка."""

    names = [n for n in names if n and n.strip()]
    if not names:
        return ""
    title = (title or names[0]).strip()
    groups = load()
    names += groups.get(title, [])      # к группе с таким же именем дописываем
    keys = {key(n) for n in names}

    # то, что уже лежало в других группах, забираем себе целиком
    for other, members in list(groups.items()):
        if other == title:
            continue
        if any(key(m) in keys for m in members):
            names += members
            del groups[other]

    seen: dict[str, str] = {}
    for n in names:
        seen.setdefault(key(n), n)
    groups[title] = sorted(seen.values(), key=str.lower)
    save(groups)
    return title


def split(names: list[str]) -> None:
    """Разъединить: написания снова сами по себе."""

    keys = {key(n) for n in names if n}
    groups = load()
    for title, members in list(groups.items()):
        left = [m for m in members if key(m) not in keys and key(title) not in keys]
        if len(left) == len(members):
            continue
        if len(left) > 1:
            groups[title] = left
        else:
            del groups[title]
    save(groups)


def rename(title: str, new_title: str) -> None:
    new_title = (new_title or "").strip()
    if not new_title or new_title == title:
        return
    groups = load()
    if title in groups:
        groups[new_title] = groups.pop(title)
        save(groups)
