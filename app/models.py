from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

DASH = "—"

RZN_CARD = "https://elk.roszdravnadzor.gov.ru/widget/med-product/"

NO_BIDDING = "торгов не было"
NO_NMCK = "начальная цена не найдена"
SMALL_VOLUME = "торгов не было: закупка малого объёма"

NO_BIDDING_WAYS = ("единственн", "без проведения")
SMALL_VOLUME_WAYS = ("малого объ", "статьи 93", "ст. 93", "ст.93",
                     "электронный магазин", "портал поставщиков")


def _clean(v: Optional[str]) -> str:
    if v is None:
        return DASH
    s = str(v).replace("\xa0", " ").strip()
    s = " ".join(s.split())
    return s if s else DASH


@dataclass(slots=True)
class ContractMeta:
    reestr_number: str = ""
    contract_number: str = ""
    purchase_number: str = ""
    ikz: str = ""
    conclusion_date: Optional[date] = None
    execution_end_date: Optional[date] = None
    stage: str = ""
    customer: str = ""
    customer_inn: str = ""
    supplier: str = ""
    supplier_inn: str = ""
    contract_price: Optional[float] = None
    currency: str = "RUB"
    nmck: Optional[float] = None
    nmck_note: str = ""
    amended: bool = False
    placing_way: str = ""
    source: str = ""
    source_ref: str = ""

    @property
    def price_incomparable(self) -> str:

        if self.amended:
            return "цена изменена доп. соглашением"
        if self.nmck_note:
            return self.nmck_note
        if (self.nmck is not None and self.contract_price is not None
                and self.contract_price > self.nmck):
            return "цена контракта выше начальной — сравнивать не с чем"
        return ""

    def _raw_pct(self) -> Optional[float]:

        if self.nmck is None or not self.nmck or self.contract_price is None:
            return None
        return round(100.0 * (self.nmck - self.contract_price) / self.nmck, 2)

    @property
    def discount(self) -> Optional[float]:
        if self.nmck is None or self.contract_price is None:
            return None
        if self.no_bidding_note != DASH:
            return None
        return round(self.nmck - self.contract_price, 2)

    @property
    def discount_pct(self) -> Optional[float]:
        if self.no_bidding_note != DASH:
            return None
        return self._raw_pct()

    @property
    def discount_cell(self):

        pct = self.discount_pct
        if pct is not None:
            return pct
        note = self.no_bidding_note
        return note if note != DASH else DASH

    @property
    def no_bidding_note(self) -> str:

        way = (self.placing_way or "").lower()
        if any(k in way for k in NO_BIDDING_WAYS):
            return NO_BIDDING
        reason = self.price_incomparable
        if reason:
            return reason
        if self.nmck is None:
            return NO_NMCK
        if self._raw_pct() == 0 and any(k in way for k in SMALL_VOLUME_WAYS):
            return SMALL_VOLUME
        return DASH

    @property
    def url(self) -> str:
        if not self.reestr_number:
            return DASH
        return ("https://zakupki.gov.ru/epz/contract/contractCard/common-info.html"
                f"?reestrNumber={self.reestr_number}")


@dataclass(slots=True)
class Position:
    index: Optional[int] = None
    name: str = ""
    ktru: str = ""
    ktru_name: str = ""
    okpd2: str = ""
    is_medical: bool = False
    nkmi_code: str = ""
    nkmi_name: str = ""
    ru_name: str = ""
    ru_number: str = ""
    tu_number: str = ""
    ru_registry_name: str = ""
    ru_status: str = ""
    rzn_id: str = ""
    ru_variants: str = ""
    erul: str = ""
    trademark: str = ""
    manufacturer: str = ""
    declarant: str = ""
    declarant_inn: str = ""
    mark: str = ""
    mark_source: str = ""
    # почему строка в отчёте: какая строка запроса сошлась и с какой оговоркой
    match_note: str = ""
    match_doubt: str = ""   # «» | «исполнение» | «бренд»
    manufacturer_source: str = ""
    confidence: str = ""
    price: Optional[float] = None
    quantity: Optional[float] = None
    quantity_undefined: bool = False
    unit: str = ""
    vat: str = ""
    country: str = ""
    total: Optional[float] = None
    specs: list = field(default_factory=list)

    @property
    def specs_text(self) -> str:
        """Характеристики позиции одной строкой: «Имя: значение; …»."""

        return "; ".join(f"{n}: {v}" for n, v in self.specs)

    @property
    def rzn_url(self) -> str:
        """Карточка регистрационного удостоверения в открытом реестре РЗН."""

        if not self.rzn_id:
            return ""
        return f"{RZN_CARD}{self.rzn_id}"

    @property
    def from_contract_number(self) -> bool:
        """Производитель взят из реестра по номеру, который написан в самом
        контракте, — самый надёжный путь. Срез по виду сюда не входит: там
        номер выбрала программа, а не прочитала."""

        s = self.manufacturer_source
        return s.startswith("реестр РЗН") and "по виду" not in s

    @property
    def has_clue(self) -> bool:
        """Было ли в позиции за что зацепиться: номер, обозначение модели
        или товарный знак. Пустая ячейка при зацепке и пустая ячейка без неё —
        разные новости, и в отчёте они разного цвета."""

        return bool(self.ru_number or self.tu_number or self.erul
                    or self.mark or self.trademark)

    def matches_ktru(self, wanted: set[str]) -> bool:
        return bool(self.ktru) and self.ktru in wanted

    def contract_text(self) -> str:

        parts = [self.name]
        if self.is_medical or self.nkmi_code:
            inner = ": ".join(x for x in (self.nkmi_code, self.nkmi_name) if x)
            parts.append(f"(объект закупки является медицинским изделием"
                         + (f", код НКМИ: {inner}" if inner else "") + ")")
        if self.ru_name and self.ru_name != self.name:
            parts.append(f"Наименование по РУ: {self.ru_name}")
        if self.trademark:
            parts.append(f"Товарный знак: {self.trademark}")
        return "\n".join(x for x in parts if x)


@dataclass(slots=True)
class Row:
    meta: ContractMeta
    pos: Position

    def as_dict(self) -> dict:
        m, p = self.meta, self.pos
        return {
            "Дата контракта": m.conclusion_date or DASH,
            "Ссылка на ЕИС": m.url,
            "Наименование позиции": _clean(p.name),
            "Текст позиции из контракта": _clean(p.contract_text()),
            "НКМИ": _clean(p.nkmi_code),
            "КТРУ": _clean(p.ktru),
            "№ РУ": _clean(p.ru_number),
            "Производитель": _clean(p.manufacturer),
            "Держатель РУ в РФ": _clean(p.declarant),
            "ИНН держателя РУ": _clean(p.declarant_inn),
            "Цена за ед., ₽": p.price,
            "Количество": p.quantity,
            "Сумма по позиции, ₽": p.total,
            "Ставка НДС": _clean(p.vat),
            "Цена контракта, ₽": m.contract_price,
            # НМЦК стоит рядом со снижением: снижение считается из этих двух
            # чисел, и когда оно не считается — видно, какого из них нет
            "НМЦК, ₽": m.nmck,
            "Снижение, %": m.discount_cell,
            # почему строка здесь: по какой строке запроса она взята и что в
            # этом совпадении неточного. Без этого «что попало в отчёт и
            # почему» приходится выяснять чтением текста позиции
            "Совпадение с запросом": _clean(p.match_note),
            "Заказчик": _clean(m.customer),
            "Поставщик": _clean(m.supplier),
            "ИНН поставщика": _clean(m.supplier_inn),
            "Характеристики": _clean(p.specs_text),
        }


COLUMNS: list[str] = list(
    Row(ContractMeta(), Position()).as_dict().keys()
)


@dataclass(slots=True)
class Problem:
    ref: str
    stage: str
    message: str


@dataclass(slots=True)
class RunResult:
    rows: list[Row] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
