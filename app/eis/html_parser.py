

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from selectolax.parser import HTMLParser

from ..models import ContractMeta, Position
from .search import normalize_purchase_number
from .xml_parser import _is_real_trademark

_NKMI = re.compile(r"код\s*НКМИ\s*[:\s]\s*(\d{3,8})", re.I)
_NKMI_NAME = re.compile(r"код\s*НКМИ\s*[:\s]\s*\d{3,8}\s*[:\s]\s*([^,)]{3,160})", re.I)
_RU_NAME = re.compile(r"наименовани\w*\s+в\s+соответствии\s+с\s+РУ\s*[:\s]\s*(.+?)\s*$",
                      re.I | re.S)
_IS_MEDICAL = re.compile(r"явля\w*\s+медицинским\s+издели\w*", re.I)
_TRADEMARK = re.compile(r"Товарн\w*\s+знак\s*[:\s]\s*([^\n,;]{2,80})", re.I)
_KTRU = re.compile(r"\b(\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{4,8})\b")
_OKPD = re.compile(r"\b(\d{2}\.\d{2}\.\d{2}\.\d{3})\b")
_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_NUM = re.compile(r"-?\d[\d\s  ]*(?:[.,]\d+)?")

_COLUMN_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*№\s*(п/п)?\s*$", re.I), "index"),
    (re.compile(r"наименовани\w*\s+объекта\s+закупки", re.I), "name"),
    (re.compile(r"тип\s+объекта\s+закупки", re.I), "type"),
    (re.compile(r"код\s+позиции|позици\w*\s+по\s+КТРУ", re.I), "code"),
    (re.compile(r"количеств\w*.*(?:единиц\w*\s+измерени|ОКЕИ)", re.I | re.S), "qty"),
    (re.compile(r"характеристик\w*\s+объекта", re.I), "chars"),
    (re.compile(r"цена\s+за\s+единиц", re.I), "price"),
    (re.compile(r"ставка\s+НДС|НДС", re.I), "vat"),
    (re.compile(r"стран\w*\s+происхождени", re.I), "country"),
    (re.compile(r"сумма", re.I), "sum"),
]


def _txt(node) -> str:
    if node is None:
        return ""
    return " ".join(node.text(separator=" ", strip=True).replace("\xa0", " ").split())


def _num(s: str) -> Optional[float]:
    m = _NUM.search((s or "").replace("\xa0", " "))
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _money(s: str) -> Optional[float]:
    v = _num(s)
    return None if v is None else round(v, 2)


def _date_of(s: str) -> Optional[date]:
    m = _DATE.search(s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _map_columns(cells: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, c in enumerate(cells):
        for pat, key in _COLUMN_MAP:
            if key in out:
                continue
            if pat.search(c):
                out[key] = i
                break
    return out


_REQUIRED_COLS = {"name", "price"}
_SCORED_COLS = ("name", "price", "code", "qty", "vat", "country", "sum", "type", "index")


def _find_object_table(tree: HTMLParser):

    best = None
    best_score = 0
    for tbl in tree.css("table"):
        head = _header_cells(tbl)
        if not head:
            continue
        cols = _map_columns(head)
        if not _REQUIRED_COLS <= set(cols):
            continue
        score = sum(1 for k in _SCORED_COLS if k in cols)
        if score > best_score:
            best, best_score = (tbl, cols), score
    return best if best else (None, {})


def _header_cells(tbl) -> list[str]:
    thead = tbl.css_first("thead")
    scope = thead or tbl
    cells = [_txt(c) for c in scope.css("td, th")]
    if not cells:
        return []
    cells = [c for c in cells if not re.fullmatch(r"\d{1,2}", c)]
    return cells[:14]


def parse_print_form(html: str, source_ref: str = "") -> tuple[ContractMeta, list[Position]]:
    tree = HTMLParser(html)
    meta = _parse_meta(tree)
    meta.source = "print-form"
    meta.source_ref = source_ref

    tbl, cols = _find_object_table(tree)
    if tbl is None:
        return meta, []

    positions: list[Position] = []
    body = tbl.css_first("tbody") or tbl
    for tr in body.css("tr"):
        cells = tr.css("td")
        if len(cells) < 5:
            continue
        texts = [_txt(c) for c in cells]
        first = texts[0]
        if not re.fullmatch(r"\d{1,3}", first):
            continue
        if re.fullmatch(r"(?:\d{1,2}\s*)+", " ".join(texts)):
            continue
        pos = _parse_row(texts, cols)
        if pos is not None:
            positions.append(pos)
    return meta, positions


def _cell(texts: list[str], cols: dict[str, int], key: str) -> str:
    i = cols.get(key)
    if i is None or i >= len(texts):
        return ""
    return texts[i]


def _parse_row(texts: list[str], cols: dict[str, int]) -> Optional[Position]:
    pos = Position()
    idx = _cell(texts, cols, "index") or texts[0]
    if idx.isdigit():
        pos.index = int(idx)

    name_cell = _cell(texts, cols, "name")
    if not name_cell:
        return None

    code_cell = _cell(texts, cols, "code")
    m = _KTRU.search(code_cell)
    pos.ktru = m.group(1) if m else ""
    okpd = [o for o in _OKPD.findall(code_cell) if not pos.ktru.startswith(o)]
    pos.okpd2 = okpd[0] if okpd else ""
    mk = re.match(r"\s*(.+?)\s*\(\s*\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{4,8}", code_cell)
    pos.ktru_name = mk.group(1).strip() if mk else ""

    qty_cell = _cell(texts, cols, "qty")
    pos.quantity = _num(qty_cell)
    mu = re.search(r"\d[\d\s.,]*\s+(.+)$", qty_cell)
    pos.unit = mu.group(1).strip() if mu else ""

    pos.price = _money(_cell(texts, cols, "price"))
    pos.total = _money(_cell(texts, cols, "sum"))
    pos.vat = _cell(texts, cols, "vat")
    pos.country = _cell(texts, cols, "country")
    if pos.total is None and pos.price is not None and pos.quantity is not None:
        pos.total = round(pos.price * pos.quantity, 2)

    _parse_name_cell(pos, name_cell)
    return pos


def _parse_name_cell(pos: Position, cell: str) -> None:
    pos.is_medical = bool(_IS_MEDICAL.search(cell))

    m = _NKMI.search(cell)
    pos.nkmi_code = m.group(1) if m else ""
    m = _NKMI_NAME.search(cell)
    if m:
        pos.nkmi_name = m.group(1).strip(" .,;:")

    m = _TRADEMARK.search(cell)
    if m:
        tm = m.group(1).strip(" .,;:")
        if _is_real_trademark(tm):
            pos.trademark = tm
    body = _TRADEMARK.sub(" ", cell)

    mb = re.search(r"\(\s*объект закупки является медицинским издели\w*(.*)$",
                   body, re.I | re.S)
    if mb:
        block = _cut_at_closing_paren(mb.group(1))
        m = _RU_NAME.search(block)
        if m:
            pos.ru_name = m.group(1).strip(" .,;:")

    head = re.split(r"\(\s*объект закупки является|Товарн\w*\s+знак\s*:", cell, maxsplit=1,
                    flags=re.I)[0]
    pos.name = head.strip(" .,;:") or cell[:200]


def _cut_at_closing_paren(s: str) -> str:

    depth = 1
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[:i]
    return s


def _parse_meta(tree: HTMLParser) -> ContractMeta:

    meta = ContractMeta()
    pairs: list[tuple[str, str]] = []
    for tr in tree.css("tr"):
        cells = tr.css("td")
        if len(cells) != 2:
            continue
        k, v = _txt(cells[0]), _txt(cells[1])
        if k and v and len(k) < 130:
            pairs.append((k.lower(), v))

    def find(*needles: str) -> str:
        for k, v in pairs:
            if all(n in k for n in needles):
                return v
        return ""

    meta.contract_number = find("номер контракта")
    meta.ikz = find("идентификационный код закупки")
    meta.purchase_number = normalize_purchase_number(find("номер извещения"))
    meta.currency = find("валюта контракта") or "RUB"
    price = find("цена контракта")
    if price:
        meta.contract_price = _num(price)
    end = find("дата окончания исполнения")
    if end:
        meta.execution_end_date = _date_of(end)

    text = " ".join(tree.body.text(separator="\n", strip=True).split("\n")) if tree.body else ""

    shorts = [v for k, v in pairs if "сокращенное наименование" in k]
    fulls = [v for k, v in pairs if "полное наименование" in k]
    meta.customer = (shorts[0] if shorts else (fulls[0] if fulls else ""))
    if len(shorts) > 1:
        meta.supplier = shorts[1]
    elif len(fulls) > 1:
        meta.supplier = fulls[1]
    elif shorts and fulls and shorts[0] != fulls[0]:
        meta.supplier = ""

    ru_inns = _collect_inns(tree)
    if ru_inns:
        meta.customer_inn = ru_inns[0]
        if len(ru_inns) > 1:
            meta.supplier_inn = ru_inns[1]

    meta.conclusion_date = _signature_date(text)
    return meta


def _collect_inns(tree: HTMLParser) -> list[str]:
    out: list[str] = []
    for tr in tree.css("tr"):
        cells = tr.css("td")
        if len(cells) < 2:
            continue
        if _txt(cells[0]).strip().upper() == "ИНН":
            v = _txt(cells[1])
            if re.fullmatch(r"\d{10,12}", v) and v not in out:
                out.append(v)
    return out


def _signature_date(text: str) -> Optional[date]:
    m = re.search(r"Подпись заказчика.*?Дата и время подписания:?\s*(\d{2}\.\d{2}\.\d{4})",
                  text, re.S | re.I)
    if m:
        return _date_of(m.group(1))
    dates = [_date_of(d) for d in re.findall(
        r"Дата и время подписания:?\s*(\d{2}\.\d{2}\.\d{4})", text, re.I)]
    dates = [d for d in dates if d]
    return max(dates) if dates else None
