

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Optional

from lxml import etree

log = logging.getLogger(__name__)

_KTRU = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{4,8}\b")

_COLUMNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"наименовани\w*\s+медицинск\w*\s+издели\w*|"
                r"в\s+соответствии\s+с\s+(?:РУ|регистрационн)", re.I), "ru_name"),
    (re.compile(r"товарн\w*\s*знак", re.I), "trademark"),
    (re.compile(r"производител|изготовител", re.I), "manufacturer"),
    (re.compile(r"регистрационн\w*\s*удостоверен|номер\s*РУ", re.I), "ru_number"),
    (re.compile(r"стран\w*\s+происхожд", re.I), "country"),
    (re.compile(r"код\w*\s*(?:по\s*)?(?:справочник|позиции|КТРУ|ОКПД)", re.I), "code"),
    (re.compile(r"наименовани\w*\s+товар|наименовани\w*\s+объекта", re.I), "name"),
]
_EMPTY = re.compile(r"^(?:не\s*указан\w*|отсутству\w*|нет|-|—|н/д)$", re.I)


@dataclass(slots=True)
class SpecRow:
    code: str = ""
    name: str = ""
    ru_name: str = ""
    ru_number: str = ""
    trademark: str = ""
    manufacturer: str = ""
    country: str = ""

    @property
    def useful(self) -> bool:
        return bool(self.ru_name or self.trademark or self.manufacturer or self.ru_number)


def _clean(v: str) -> str:
    v = " ".join((v or "").split())
    return "" if _EMPTY.match(v) else v


def extract_tables(data: bytes) -> list[list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" not in z.namelist():
            return []
        root = etree.fromstring(z.read("word/document.xml"))
    tables: list[list[list[str]]] = []
    for tbl in root.iter("{*}tbl"):
        rows: list[list[str]] = []
        for tr in tbl.iter("{*}tr"):
            cells = []
            for tc in tr.iter("{*}tc"):
                text = " ".join(
                    "".join(t.text or "" for t in p.iter("{*}t"))
                    for p in tc.iter("{*}p"))
                cells.append(" ".join(text.split()))
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _map_columns(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, cell in enumerate(header):
        for pat, key in _COLUMNS:
            if key not in out and pat.search(cell):
                out[key] = i
                break
    return out


def parse_spec(data: bytes) -> list[SpecRow]:
    try:
        tables = extract_tables(data)
    except Exception as e:
        log.debug("docx не читается: %s: %s", type(e).__name__, e)
        return []

    out: list[SpecRow] = []
    for tbl in tables:
        cols: dict[str, int] = {}
        head_at = -1
        for i, row in enumerate(tbl[:4]):
            m = _map_columns(row)
            if {"ru_name", "trademark", "manufacturer", "ru_number"} & set(m):
                cols, head_at = m, i
                break
        if not cols:
            continue

        def cell(row: list[str], key: str) -> str:
            i = cols.get(key)
            return _clean(row[i]) if i is not None and i < len(row) else ""

        for row in tbl[head_at + 1:]:
            if len(row) < 2:
                continue
            joined = " ".join(row)
            if re.match(r"^\s*(?:итого|всего)\b", joined, re.I):
                continue
            r = SpecRow(
                name=cell(row, "name"),
                ru_name=cell(row, "ru_name"),
                ru_number=cell(row, "ru_number"),
                trademark=cell(row, "trademark"),
                manufacturer=cell(row, "manufacturer"),
                country=cell(row, "country"),
            )
            m = _KTRU.search(cell(row, "code") or joined)
            r.code = m.group(0) if m else ""
            if r.useful:
                out.append(r)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (s or "").lower().replace("ё", "е"))


def match_row(rows: list[SpecRow], *, ktru: str = "", name: str = "") -> Optional[SpecRow]:

    if ktru:
        same = [r for r in rows if r.code == ktru]
        if len(same) == 1:
            return same[0]
        if same:
            rows = same
    if name:
        target = _norm(name)
        best, best_score = None, 0.0
        for r in rows:
            cand = _norm(r.name or r.ru_name)
            if not cand:
                continue
            if cand == target or cand in target or target in cand:
                return r
            common = len(set(cand) & set(target)) / max(1, len(set(target)))
            if common > best_score:
                best, best_score = r, common
        if best is not None and best_score >= 0.9:
            return best
    return rows[0] if len(rows) == 1 else None
