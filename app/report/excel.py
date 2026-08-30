from __future__ import annotations

from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .. import groups
from ..models import COLUMNS, DASH, RunResult

FONT = "Bahnschrift Light"
FONT_HEAD = "Bahnschrift SemiBold"

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT_HEAD, color="FFFFFF", bold=False, size=11)
BODY_FONT = Font(name=FONT, size=11)
LINK_FONT = Font(name=FONT, size=11, color="1F6FEB", underline="single")
RU_LINK_FONT = Font(name=FONT, size=11, color="1F6FEB", underline="single")

ALT_FILL = PatternFill("solid", fgColor="F4F6FA")
PRICE_FILL = PatternFill("solid", fgColor="FFF3C4")
PRICE_FILL_ALT = PatternFill("solid", fgColor="FDECAF")
MISSING_FILL = PatternFill("solid", fgColor="F8CFCB")

HAIR = Side(style="hair", color="C9CEDA")
BORDER = Border(bottom=HAIR, left=HAIR, right=HAIR)

MONEY = '#,##0.00" ₽"'
INT = "#,##0"
PERCENT = '0.0" %"'
DATE = "DD.MM.YYYY"

PRICE_COLUMN = "Цена за ед., ₽"
MISSING_COLUMNS = ("№ РУ", "Производитель", "Держатель РУ в РФ")
LINK_COLUMN = "Ссылка на ЕИС"
LINK_TEXT = "открыть в ЕИС"

LEFT = "left"
CENTER = "center"

# колонка -> (ширина, выравнивание, формат числа, переносить ли текст)
LAYOUT: dict[str, tuple[int, str, Optional[str], bool]] = {
    "Дата контракта": (13, CENTER, DATE, False),
    "Ссылка на ЕИС": (16, CENTER, None, False),
    "Наименование позиции": (42, LEFT, None, True),
    "Текст позиции из контракта": (58, LEFT, None, True),
    "НКМИ": (10, CENTER, None, False),
    "КТРУ": (23, CENTER, None, False),
    "№ РУ": (19, LEFT, None, False),
    "Производитель": (34, LEFT, None, True),
    "Держатель РУ в РФ": (30, LEFT, None, True),
    "ИНН держателя РУ": (16, CENTER, None, False),
    "Цена за ед., ₽": (17, CENTER, MONEY, False),
    "Количество": (12, CENTER, INT, False),
    "Сумма по позиции, ₽": (18, CENTER, MONEY, False),
    "Ставка НДС": (12, CENTER, None, False),
    "Цена контракта, ₽": (18, CENTER, MONEY, False),
    "Снижение, %": (26, LEFT, PERCENT, True),
    "Заказчик": (40, LEFT, None, True),
    "Поставщик": (34, LEFT, None, True),
    "ИНН поставщика": (16, CENTER, None, False),
    "Характеристики": (70, LEFT, None, True),
}

DEFAULT_LAYOUT = (18, LEFT, None, False)


# ── Лист «Сводка»: что это за рынок, кто на нём и как менялись закупки ──

SUM_MONEY = '#,##0" ₽"'
TILE_FILL = PatternFill("solid", fgColor="EEF1F8")
BAND_FILL = PatternFill("solid", fgColor="F4F6FA")
TITLE_FONT = Font(name=FONT_HEAD, size=20, color="1F3864")
LEAD_FONT = Font(name=FONT, size=11, color="5A6480")
LABEL_FONT = Font(name=FONT, size=9.5, color="5A6480")
TILE_FONT = Font(name=FONT_HEAD, size=15, color="1F3864")
HEAD2_FONT = Font(name=FONT_HEAD, size=13, color="1F3864")
NOTE_FONT = Font(name=FONT, size=9.5, color="8A93A8")

TOP_MAKERS = 8
OTHERS = "прочие производители"


def _tile(ws, col: int, row: int, label: str, value, fmt: str = "") -> None:
    head = ws.cell(row=row, column=col, value=label)
    head.font = LABEL_FONT
    head.alignment = Alignment(horizontal=LEFT, vertical="bottom", wrap_text=True)
    head.fill = TILE_FILL
    cell = ws.cell(row=row + 1, column=col, value=value)
    cell.font = TILE_FONT
    cell.alignment = Alignment(horizontal=LEFT, vertical="center")
    cell.fill = TILE_FILL
    if fmt:
        cell.number_format = fmt


def _head_row(ws, row: int, titles: list[str]) -> None:
    for i, name in enumerate(titles, 1):
        cell = ws.cell(row=row, column=i, value=name)
        cell.font = Font(name=FONT_HEAD, size=10, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal=LEFT if i == 1 else CENTER,
                                   vertical="center", wrap_text=True)


def _sheet_summary(ws: Worksheet, result: RunResult, note: str) -> None:
    from .analysis import market_summary

    ws.title = "Сводка"
    ws.sheet_view.showGridLines = False
    for col, width in zip("ABCDEFG", (46, 13, 14, 13, 18, 13, 17)):
        ws.column_dimensions[col].width = width

    data = market_summary(result.rows)

    title = ws.cell(row=1, column=1, value="Сводка по рынку")
    title.font = TITLE_FONT
    ws.row_dimensions[1].height = 30
    lead = ws.cell(row=2, column=1, value=note or "")
    lead.font = LEAD_FONT

    ws.row_dimensions[4].height = 26
    ws.row_dimensions[5].height = 24
    # первая плитка — в широкой колонке, поэтому она же и главная цифра
    tiles = [
        ("Закуплено на сумму", data["sum"], SUM_MONEY),
        ("Контрактов", data["contracts"], INT),
        ("Позиций", data["positions"], INT),
        ("Производителей", data["makers_known"], INT),
        ("Цена за единицу", data["price"], SUM_MONEY),
        ("Снижение на торгах", data["drop"], PERCENT),
    ]
    for i, (label, value, fmt) in enumerate(tiles, 1):
        _tile(ws, i, 4, label, value if value is not None else DASH, fmt)

    note_cell = ws.cell(row=6, column=1,
                        value=f"производитель определён у {data['known_share']} % "
                              "позиций; «обычная» цена и снижение — медианные, "
                              "то есть середина, а не среднее")
    note_cell.font = NOTE_FONT

    row = 8
    head = ws.cell(row=row, column=1, value="Кто поставляет")
    head.font = HEAD2_FONT
    row += 1
    _head_row(ws, row, ["Производитель", "Позиций", "Контрактов", "Единиц",
                        "Сумма, ₽", "Доля рынка", "Цена за ед., ₽"])
    first = row + 1

    shown = [m for m in data["makers"] if not m["unknown"]][:TOP_MAKERS]
    rest = [m for m in data["makers"] if m not in shown]
    if rest:
        shown = shown + [{
            "name": OTHERS if len(rest) > 1 else rest[0]["name"],
            "positions": sum(m["positions"] for m in rest),
            "contracts": sum(m["contracts"] for m in rest),
            "units": round(sum(m["units"] for m in rest), 2),
            "sum": round(sum(m["sum"] for m in rest), 2),
            "share": round(sum(m["share"] for m in rest), 1),
            "price": None,
        }]

    for n, maker in enumerate(shown):
        row += 1
        values = [maker["name"], maker["positions"], maker["contracts"],
                  maker["units"], maker["sum"], maker["share"], maker["price"]]
        for i, value in enumerate(values, 1):
            cell = ws.cell(row=row, column=i,
                           value=value if value is not None else DASH)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(horizontal=LEFT if i == 1 else CENTER,
                                       vertical="center", wrap_text=i == 1)
            if n % 2:
                cell.fill = BAND_FILL
        ws.cell(row=row, column=4).number_format = INT
        ws.cell(row=row, column=5).number_format = SUM_MONEY
        ws.cell(row=row, column=6).number_format = PERCENT
        ws.cell(row=row, column=7).number_format = SUM_MONEY

    _pie(ws, first, row)

    row += 2
    head = ws.cell(row=row, column=1, value="Как шли закупки по месяцам")
    head.font = HEAD2_FONT
    row += 1
    _head_row(ws, row, ["Месяц", "Контрактов", "Сумма, ₽", "Цена за ед., ₽"])
    first_month = row + 1
    for n, month in enumerate(data["timeline"]):
        row += 1
        for i, value in enumerate([month["month"], month["contracts"],
                                   month["sum"], month["price"]], 1):
            cell = ws.cell(row=row, column=i,
                           value=value if value is not None else DASH)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(horizontal=LEFT if i == 1 else CENTER,
                                       vertical="center")
            if n % 2:
                cell.fill = BAND_FILL
        ws.cell(row=row, column=3).number_format = SUM_MONEY
        ws.cell(row=row, column=4).number_format = SUM_MONEY

    if row >= first_month:
        _bars(ws, first_month, row)


def _pie(ws: Worksheet, first: int, last: int) -> None:
    if last < first:
        return
    chart = PieChart()
    chart.title = "Доля рынка по сумме"
    chart.height, chart.width = 8.6, 12.5
    chart.add_data(Reference(ws, min_col=5, min_row=first, max_row=last),
                   titles_from_data=False)
    chart.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showPercent = True
    ws.add_chart(chart, "I3")


def _bars(ws: Worksheet, first: int, last: int) -> None:
    chart = BarChart()
    chart.type = "col"
    chart.title = "Сумма закупок по месяцам, ₽"
    chart.height, chart.width = 8.6, 12.5
    chart.legend = None
    chart.y_axis.numFmt = "#,##0"
    chart.add_data(Reference(ws, min_col=3, min_row=first, max_row=last),
                   titles_from_data=False)
    chart.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
    ws.add_chart(chart, "I21")


def build_workbook(result: RunResult, params_note: str = "") -> Workbook:
    wb = Workbook()
    _sheet_summary(wb.active, result, params_note)
    _sheet_positions(wb.create_sheet(), result)
    return wb


def _sheet_positions(ws: Worksheet, result: RunResult) -> None:
    ws.title = "Позиции"
    idx = {name: i for i, name in enumerate(COLUMNS, 1)}

    for name, i in idx.items():
        width, _align, _fmt, _wrap = LAYOUT.get(name, DEFAULT_LAYOUT)
        ws.column_dimensions[get_column_letter(i)].width = width
        cell = ws.cell(row=1, column=i, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal=CENTER, vertical="center",
                                   wrap_text=True)
    ws.row_dimensions[1].height = 38

    # в таблицу идёт объединённое имя производителя — то же, что
    # в списке на странице
    titles = groups.assign(r.pos.manufacturer for r in result.rows)

    n = 0
    for row in result.rows:
        n += 1
        r = n + 1
        data = row.as_dict()
        striped = n % 2 == 0
        for name, i in idx.items():
            _w, align, fmt, wrap = LAYOUT.get(name, DEFAULT_LAYOUT)
            value = data[name]
            if name == "Производитель":
                value = titles.get(row.pos.manufacturer, value)
            cell = ws.cell(row=r, column=i, value=value)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(horizontal=align, vertical="top",
                                       wrap_text=wrap)
            if fmt and isinstance(value, (int, float)):
                cell.number_format = fmt
            elif fmt == DATE and value not in (None, DASH):
                cell.number_format = fmt
            if striped:
                cell.fill = ALT_FILL

        price = ws.cell(row=r, column=idx[PRICE_COLUMN])
        price.fill = PRICE_FILL_ALT if striped else PRICE_FILL

        for name in MISSING_COLUMNS:
            cell = ws.cell(row=r, column=idx[name])
            if not cell.value or cell.value == DASH:
                cell.fill = MISSING_FILL

        card = row.pos.rzn_url
        if card:
            ru = ws.cell(row=r, column=idx["№ РУ"])
            ru.hyperlink = card
            ru.font = RU_LINK_FONT

        link = ws.cell(row=r, column=idx[LINK_COLUMN])
        if isinstance(link.value, str) and link.value.startswith("http"):
            link.hyperlink = link.value
            link.value = LINK_TEXT
            link.font = LINK_FONT

    if n:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{n + 1}"
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def save_report(result: RunResult, path: str | Path, params_note: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = build_workbook(result, params_note)
    wb.save(path)
    return path
