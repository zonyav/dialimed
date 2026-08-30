
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime
from typing import Optional

from lxml import etree

from ..models import ContractMeta, Position
from .search import normalize_purchase_number

log = logging.getLogger(__name__)


def _block(parent, tag: str):

    direct = parent.find(f"./{{*}}{tag}")
    if direct is not None and len(direct):
        return direct
    return parent.find(f".//{{*}}{tag}")


_ENTITY = re.compile(r"&(?:[A-Za-z]+|#\d+);")


def _node_text(node) -> str:
    if node is None or node.text is None:
        return ""
    v = " ".join(node.text.split())
    return html.unescape(v) if _ENTITY.search(v) else v


def _text(el, path: str) -> str:
    if el is None:
        return ""
    return _node_text(el.find(path))


def _text_any(el, *names: str) -> str:
    for n in names:
        v = _text(el, f".//{{*}}{n}")
        if v:
            return v
    return ""


def _num(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return None


def _money(s: str) -> Optional[float]:

    v = _num(s)
    return None if v is None else round(v, 2)


def _date(s: str) -> Optional[date]:

    s = (s or "").strip()
    if not s:
        return None
    head = re.split(r"[T+ ]", s, maxsplit=1)[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


_TM_NOISE = re.compile(
    r"^(?:отсутству|не\s*установл|не\s*указан|нет\b|-|—|n/?a$|"
    r"свидетельств|сертификат|№\s*\d|рег\w*\s*(?:номер|№)|"
    r"номер\s+(?:госуд|регистр))", re.I)
_TM_BARE_NUMBER = re.compile(r"^(?:№|N|No|поз\.?|п\.?)?\s*[\d\s.\-/]{1,20}$")


_TM_SPECS = re.compile(r"[<>]=|≥|≤|кгс/см|мм рт|,\s*кг\b|\bвольт\b|\bгц\b", re.I)


_TM_FLAGS = frozenset({"false", "true", "0", "1", "да", "нет"})


_TM_BOILERPLATE = re.compile(
    r"\(\s*(?:объект\s+закупки\s+)?явля\w+\s+медицинским\s+издели\w*[^)]*\)?",
    re.I)
_TM_ABSENT = re.compile(
    r"товарн(?:ый|ые)\s*знак(?:и)?\s*[:—-]?\s*"
    r"(?:отсутств\w*|не\s*установл\w*|не\s*указан\w*|нет)\b", re.I)


def clean_trademark(tm: str) -> str:

    s = _TM_BOILERPLATE.sub(" ", tm or "")
    s = _TM_ABSENT.sub(" ", s)
    return " ".join(s.split()).strip(" .,;:—-")


def _is_real_trademark(tm: str) -> bool:
    tm = " ".join((tm or "").split())
    if len(tm) < 2 or len(tm) > 120:
        return False
    if tm.lower() in _TM_FLAGS:
        return False
    if _TM_SPECS.search(tm) or tm.count(":") >= 2:
        return False
    return not (_TM_NOISE.match(tm) or _TM_BARE_NUMBER.match(tm))


def _supplier_name(part) -> str:

    name = _text_any(part, "shortName", "fullName")
    if name:
        return name
    person = part.find(".//{*}individualPersonRFInfo")
    if person is None:
        return ""
    fio = " ".join(x for x in (_text_any(person, "lastName"),
                               _text_any(person, "firstName"),
                               _text_any(person, "middleName")) if x)
    if not fio:
        return ""
    if fio.isupper():
        fio = fio.title()
    return f"ИП {fio}" if _text_any(person, "isIP").lower() == "true" else fio


def parse_contract_xml(data: bytes, meta: ContractMeta | None = None
                       ) -> tuple[ContractMeta, list[Position]]:

    root = etree.fromstring(data)
    m = meta or ContractMeta()

    m.contract_number = m.contract_number or _text_any(root, "contractNumber")
    m.purchase_number = (m.purchase_number
                         or normalize_purchase_number(_text_any(root, "purchaseNumber")))
    m.ikz = m.ikz or _text_any(root, "IKZ", "ikz", "purchaseCode")

    cust = root.find(".//{*}customerInfo")
    if cust is not None:
        m.customer = m.customer or _text_any(cust, "shortName", "fullName")
        m.customer_inn = m.customer_inn or _text(cust, "./{*}INN") or _text_any(cust, "INN")

    part = root.find(".//{*}participantInfo")
    if part is not None:
        m.supplier = m.supplier or _supplier_name(part)
        m.supplier_inn = m.supplier_inn or _text_any(part, "INN")

    if m.contract_price is None:
        price_info = root.find(".//{*}contractFinancingInfo/{*}contractPriceInfo")
        m.contract_price = _num(_text(price_info, "./{*}price")) if price_info is not None else None
        if m.contract_price is None:
            m.contract_price = _num(_text_any(root, "contractPrice"))

    if not m.placing_way:
        way = root.find(".//{*}placingWay")
        if way is not None:
            m.placing_way = _text(way, "./{*}name") or _text_any(way, "name")

    m.currency = _text(root, ".//{*}currencyInfo/{*}code") or "RUB"

    if m.execution_end_date is None:
        m.execution_end_date = _date(
            _text(root, ".//{*}contractExecutionTermsInfo//{*}endDate")
            or _text_any(root, "endDate")
        )
    if m.conclusion_date is None:
        m.conclusion_date = _date(_text_any(root, "signDate", "conclusionDate",
                                            "contractDate"))

    m.source = "eis-xml"

    positions: list[Position] = []
    for i, p in enumerate(root.iter("{*}productInfo"), 1):
        positions.append(_parse_product(p, i))
    if not positions:
        for tag in ("product", "contractSubjectInfo"):
            for i, p in enumerate(root.iter("{*}" + tag), 1):
                if p.find(".//{*}KTRUInfo") is not None or p.find(".//{*}price") is not None:
                    positions.append(_parse_product(p, i))
            if positions:
                break
    return m, positions


# --- характеристики позиции из карточки КТРУ в контракте ---------------
# ЕИС хранит их тремя способами: качественное значение словами, число
# с единицей измерения и диапазон «от и до». Приводим всё к строке
# «Имя: значение», чтобы позиции можно было сравнивать глазами.

_SIGNS = {"greaterOrEqual": "≥", "greater": ">",
          "lessOrEqual": "≤", "less": "<"}


def _unit(value) -> str:
    okei = value.find("./{*}OKEI")
    if okei is None:
        return ""
    code = _text(okei, "./{*}nationalCode") or _text(okei, "./{*}name")
    # «ММ» -> «мм», но однобуквенные обозначения оставляем как есть: «К»
    return code.lower() if len(code) > 1 else code


def _edge(rng, side: str) -> str:
    num = _text(rng, f"./{{*}}{side}")
    if not num:
        return ""
    sign = _SIGNS.get(_text(rng, f"./{{*}}{side}MathNotation"), "")
    return f"{sign} {num}".strip() if sign in (">", "<") else num


def _value_text(value) -> str:

    quality = _text(value, "./{*}qualityDescription")
    if quality:
        return quality

    unit = _unit(value)
    concrete = _text(value, ".//{*}concreteValue")
    if concrete:
        return f"{concrete} {unit}".strip()

    rng = value.find(".//{*}valueRange")
    if rng is not None:
        low, high = _edge(rng, "min"), _edge(rng, "max")
        if low and high:
            return f"{low}–{high} {unit}".strip()
        if low:
            return f"от {low} {unit}".strip()
        if high:
            return f"до {high} {unit}".strip()
    return ""


def parse_characteristics(product) -> list[tuple[str, str]]:

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in product.iter("{*}characteristics"):
        for ch in block:
            tag = etree.QName(ch).localname
            if not tag.startswith("characteristicsUsing"):
                continue
            name = _text(ch, "./{*}name")
            if not name:
                continue
            values = [_value_text(v) for v in ch.iter("{*}value")]
            value = ", ".join(v for v in values if v)
            key = name.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            out.append((name, value))
    return out


def _parse_product(p, index: int) -> Position:
    pos = Position(index=index)

    raw_idx = _text(p, "./{*}indexNum")
    if raw_idx.isdigit():
        pos.index = int(raw_idx)

    pos.name = _text(p, "./{*}name")

    ktru = p.find(".//{*}KTRUInfo")
    if ktru is not None:
        pos.ktru = _text(ktru, "./{*}code")
        pos.ktru_name = _text(ktru, "./{*}name")

    if not pos.name:
        pos.name = pos.ktru_name

    okpd = p.find(".//{*}OKPD2Info")
    if okpd is not None:
        pos.okpd2 = _text(okpd, "./{*}OKPDCode")

    mp = _block(p, "medicalProductInfo")
    if mp is not None:
        pos.is_medical = _text(mp, "./{*}isMedicalProductInfo").lower() == "true"
        pos.nkmi_code = _text(mp, "./{*}medicalProductCode")
        pos.nkmi_name = _text(mp, "./{*}medicalProductName")
        pos.ru_name = _text(mp, "./{*}certificateNameMedicalProduct")

    pos.specs = parse_characteristics(p)

    okei = _block(p, "OKEIInfo")
    if okei is not None:
        pos.unit = _text(okei, "./{*}nationalCode") or _text(okei, "./{*}name")

    pos.quantity = _num(_text(p, "./{*}quantity"))
    if pos.quantity is None:
        parent = p.getparent() if hasattr(p, "getparent") else None
        if parent is not None and _text(
                parent, "./{*}quantityUndefined").strip().lower() == "true":
            pos.quantity_undefined = True
    pos.price = _money(_text(p, "./{*}price"))
    pos.total = _money(_text(p, "./{*}sum"))
    if pos.total is None and pos.price is not None and pos.quantity is not None:
        pos.total = round(pos.price * pos.quantity, 2)

    vat = _block(p, "VATRateInfo")
    if vat is not None:
        pos.vat = _text(vat, "./{*}VATName") or _text(vat, "./{*}VATCode")

    country = _block(p, "originCountryInfo")
    if country is not None:
        pos.country = _text(country, "./{*}countryFullName") or _text(country, "./{*}countryCode")

    parts = [pos.name, pos.ru_name]
    tm = _text_any(p, "tradeMark", "trademark")
    if tm:
        cleaned = clean_trademark(tm)
        if cleaned and _is_real_trademark(cleaned):
            tm = cleaned
        if _is_real_trademark(tm):
            pos.trademark = tm
            parts.append(tm)
        else:
            pos.trademark_raw = tm
    pos.raw_medical_block = " | ".join(x for x in parts if x)
    return pos
