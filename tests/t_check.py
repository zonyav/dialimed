

import ast
import collections
import glob
import io
import os
import pathlib
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ONLINE = "--online" in sys.argv
CORPUS = os.path.join(os.path.expanduser("~"), "Downloads")

FAILED: list[str] = []
NOTES: list[str] = []


def section(title: str) -> None:
    print(f"\n{'─' * 74}\n  {title}\n{'─' * 74}")


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILED.append(f"{name}: получено {got!r}, ожидалось {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


section("1. Код: компиляция, лишние импорты, забытая отладка")

FILES = sorted(list((ROOT / "app").rglob("*.py")) + [ROOT / "run.py"])
problems: list[str] = []

for f in FILES:
    src = f.read_text(encoding="utf-8")
    rel = f.relative_to(ROOT)
    try:
        tree = ast.parse(src, filename=str(f))
    except SyntaxError as e:
        problems.append(f"{rel}: синтаксис — {e}")
        continue

    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported[a.asname or a.name] = node.lineno

    for name, line in imported.items():
        if name == "annotations":
            continue
        if src.count(name) <= 1:
            problems.append(f"{rel}:{line}: импорт '{name}' не используется")

    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("print(") and "web" not in str(rel) and rel.name != "run.py":
            problems.append(f"{rel}:{i}: забытый print()")
        if "TODO" in s or "FIXME" in s or "XXX" in s:
            problems.append(f"{rel}:{i}: {s[:70]}")
        if "breakpoint()" in s or "pdb.set_trace" in s:
            problems.append(f"{rel}:{i}: отладочная точка останова")
        if len(line) > 110:
            problems.append(f"{rel}:{i}: строка длиной {len(line)}")
        if any(c in line for c in "\x07\x08\x0b\x0c"):
            problems.append(f"{rel}:{i}: управляющий символ в тексте")

print(f"  проверено файлов: {len(FILES)}")

if problems:
    FAILED.append(f"замечаний по коду: {len(problems)}")
    for p in problems[:20]:
        print("   ⚠", p)
else:
    print("  ok   замечаний нет")


section("2. Отдельные функции")

from app.eis.documents import clean_filename, rank_filename
from app.eis.search import normalize_ktru, parse_ktru_bad, parse_ktru_list
from app.eis.xml_parser import _is_real_trademark, _supplier_name
from app.enrich.rzn import distinctive_tokens, similarity, strong_tokens
from app.enrich.verify import clean_company, company_key, is_company_name
from app.models import ContractMeta, Position, Row, RunResult
from app.pipeline import (_cut_company_tail, _extend_bare_marks,
                          _parse_names)

print("\n имена вложений")
check("размер отрезан", clean_filename("Электронный контракт.xml (67.08 Кб)"),
      "Электронный контракт.xml")
check("выбрана последняя редакция",
      max(["Электронный контракт.xml", "Контракт с учетом доп. соглашений 1,2.xml"],
          key=rank_filename), "Контракт с учетом доп. соглашений 1,2.xml")
for bad in ("Платёжное поручение.xml", "Документ о приемке.xml", "Акт.xml",
            "Счет-фактура.xml", "Контракт.docx"):
    check(f"отбраковано: {bad}", rank_filename(bad) < 0, True)

for name, want, why in (
        ("Контракт счетчик форменных элементов крови.xml", 100, "счётчик не счёт"),
        ("Контракт на поставку счётчиков.xml", 100, "счётчики не счета"),
        ("Подписанный контракт.xml", 100, "подписанный контракт — контракт"),
        ("Контракт с учетом доп. соглашения.xml", 1001, "редакция без номера"),
        ("Контракт с учетом доп. соглашений 1, 2.xml", 1002, "берётся наибольший номер"),
        ("Доп. соглашение 1.xml", -1, "отдельное ДС"),
        ("ДС №1 к контракту.xml", -1, "ДС, названное сокращённо"),
        ("Дополнительное соглашение № 2 к контракту.xml", -1, "ДС полным словом"),
):
    check(f"оценка {want:5d}: {why}", rank_filename(clean_filename(name)), want)

for size in ("(67.08 Кб)", "(1.2 Мб)", "(1.2 Гб)", "(523 б)", "(1 023,45 Кб)"):
    check(f"размер снят: {size}",
          rank_filename(clean_filename(f"Электронный контракт.xml {size}")), 500)
check("расширение в верхнем регистре", rank_filename("Электронный контракт.XML"), 500)

print("\n поставщик: юрлицо и предприниматель")
from lxml import etree as _et

_ЮРЛИЦО = """<participantInfo><legalEntityRFInfo>
    <shortName>ООО "ХТМ"</shortName><fullName>ОБЩЕСТВО С ОГРАНИЧЕННОЙ…</fullName>
    <INN>7805523503</INN></legalEntityRFInfo></participantInfo>"""
_ИП = """<participantInfo><individualPersonRFInfo>
    <nameInfo><lastName>МОЙКИН</lastName><firstName>АЛЕКСАНДР</firstName>
    <middleName>ВЛАДИМИРОВИЧ</middleName></nameInfo>
    <isIP>true</isIP><INN>601400200203</INN></individualPersonRFInfo></participantInfo>"""
_ФИЗЛИЦО = _ИП.replace("<isIP>true</isIP>", "<isIP>false</isIP>")
_СМЕШАННЫЙ = _ИП.replace("МОЙКИН", "Мойкин").replace("АЛЕКСАНДР", "Александр") \
                .replace("ВЛАДИМИРОВИЧ", "Владимирович")

check("юрлицо — короткое название", _supplier_name(_et.fromstring(_ЮРЛИЦО)),
      'ООО "ХТМ"')
check("ИП — с пометкой и полным именем", _supplier_name(_et.fromstring(_ИП)),
      "ИП Мойкин Александр Владимирович")
check("физлицо без отметки — без пометки ИП", _supplier_name(_et.fromstring(_ФИЗЛИЦО)),
      "Мойкин Александр Владимирович")
check("смешанный регистр не ломается", _supplier_name(_et.fromstring(_СМЕШАННЫЙ)),
      "ИП Мойкин Александр Владимирович")

print("\n коды КТРУ")
check("из мусора", normalize_ktru("  код 32.50.11.000-00000080 шт "),
      "32.50.11.000-00000080")
check("короткий суффикс", normalize_ktru("32.50.13.190-00247"), "32.50.13.190-00247")
check("не КТРУ", normalize_ktru("12345"), "")
check("список с дублями",
      parse_ktru_list("32.50.11.000-00000080\n32.50.11.000-00000080, 27.40.39.110-00000002"),
      ["32.50.11.000-00000080", "27.40.39.110-00000002"])
check("почти код опознан", parse_ktru_bad("32.50.13.190 и 32.50.11.000-00000080"),
      ["32.50.13.190"])

print("\n мост НКМИ -> КТРУ: родство наименований")
from app.eis.ktru_card import (MATCH_MIN, _query_variants,
                               _same_name, name_match)

_РОДНЫЕ = [
    ("Стимулятор глубоких тканей электромагнитный ручной",
     "Стимулятор глубоких тканей электромагнитный переносной"),
    ("Спирометр диагностический, профессиональный", "Спирометр диагностический"),
    ("Стол операционный универсальный, с гидравлическим приводом",
     "Стол операционный универсальный"),
]
for _вид, _поз in _РОДНЫЕ:
    check(f"родня: {_поз[:34]}", name_match(_вид, _поз) >= MATCH_MIN, True)

_ЧУЖИЕ = [
    ("Набор для гинекологического обследования",
     "Пепсиноген I ИВД, набор, иммунохемилюминесцентный анализ"),
    ("Весы для младенцев, электронные",
     "Аппарат искусственной вентиляции легких высокочастотный"),
    ("Электрокардиограф, профессиональный, многоканальный",
     "Регистратор амбулаторный для электрокардиографического мониторинга"),
    ("Стол гинекологический для осмотра/терапевтических процедур, механический",
     "Стол операционный гинекологический, электромеханический, с питанием от сети"),
]
for _вид, _поз in _ЧУЖИЕ:
    check(f"чужое: {_поз[:34]}", name_match(_вид, _поз) >= MATCH_MIN, False)

check("короткое наименование позиции — тоже родня",
      name_match("Электрокардиограф, профессиональный, многоканальный",
                 "Электрокардиограф") >= MATCH_MIN, True)

print("\n обозначение: имя завода, приклеенное к артикулу дефисом")
from app.enrich.nameparse import (is_plain_word, parse_name as _pn,
                                  unglue_quoted_tail)

check("кавычки хвоста снимаются",
      unglue_quoted_tail('Стерилизатор паровой ГК-100-4-"ТЗМОИ" по ТУ'),
      "Стерилизатор паровой ГК-100-4-ТЗМОИ по ТУ")
check("типографские кавычки — тоже кавычки",
      unglue_quoted_tail("Стерилизатор ГПД-560-2-“ТЗМОИ” по ТУ"),
      "Стерилизатор ГПД-560-2-ТЗМОИ по ТУ")
check("непарная кавычка не мешает",
      unglue_quoted_tail('Комплекс ЭКГ "КАРДИО-"Астел" по ТУ'),
      'Комплекс ЭКГ КАРДИО-Астел по ТУ')
check("хвост из двух слов не трогаем",
      unglue_quoted_tail('Аппарат СМВ-20-"Мед ТеКо" по ТУ'),
      'Аппарат СМВ-20-"Мед ТеКо" по ТУ')
check("закавыченная марка сама по себе не трогается",
      unglue_quoted_tail('Светильники передвижные "ЭМАЛЕД" по ТУ'),
      'Светильники передвижные "ЭМАЛЕД" по ТУ')
check("обозначение собирается целиком",
      _pn('Стерилизатор паровой ГК-100-4-"ТЗМОИ" по ТУ 9451-102-12517820-2011',
          ["Стерилизатор паровой"]).full, "ГК-100-4-ТЗМОИ")
check("артикул не рвётся по точке в номере",
      _pn("Аппарат низкочастотный физиотерапевтический «Амплипульс-5.1-«МАЯК» "
          "по ТУ 9444-001-07517597-2004", ["Аппарат для СМВ-терапии"]).full,
      "Амплипульс-5.1-МАЯК")

print("\n один завод под двумя именами: аббревиатура и расшифровка")
from app.enrich.verify import is_initialism

check("ГРПЗ", is_initialism('АО "ГРПЗ"',
                            'АО "Государственный Рязанский приборный завод"'), True)
check("ЕПЗ", is_initialism('АО "ЕПЗ"', 'АО "Елатомский приборный завод"'), True)
check("ТЗМОИ", is_initialism(
    'АО "ТЗМОИ"', 'АО "Тюменский завод медицинского оборудования и инструментов"'), True)
check("имя не аббревиатура", is_initialism(
    'ООО "Компания Нео"', 'ООО "Научно-производственное предприятие "Нео""'), False)
check("буквы не сходятся", is_initialism('ООО "МСК"', 'ООО "Медицинские системы"'), False)
check("направление одно", is_initialism(
    'АО "Государственный Рязанский приборный завод"', 'АО "ГРПЗ"'), False)

print("\n исполнение того же семейства вытесняет голову")
check("исполнение вместо головы семейства",
      _pn("Весы напольные медицинские электронные ВМЭН-150, ВМЭН-200 по ТУ "
          "9441-022-00226454-2005 Вариант исполнения ВМЭН 200-50/100-А",
          ["Весы напольные, электронные"]).full, "ВМЭН 200-50/100-А")
check("косая черта не рвёт обозначение",
      _pn("Светильник хирургический ЭМАЛЕД 500/500 по ТУ",
          ["Светильник операционный"]).full, "ЭМАЛЕД 500/500")
check("буква исполнения того же корня",
      _pn("Облучатель-рециркулятор ОРУБ-3-5-«КРОНТ» по ТУ 9451-029-11769436-2006, "
          "вариант исполнения ОРУБп-3-5-«КРОНТ»",
          ["Облучатель ультрафиолетовый бактерицидный"]).full, "ОРУБп-3-5-КРОНТ")
check("чужой корень не вытесняет марку",
      _pn('Носилки бескаркасные "Плащ" по ТУ 32.50.50-005-18585567-2003 '
          "Вариант исполнения: модель 1У", ["Носилки портативные"]).full, "Плащ 1У")

print("\n описание вместо обозначения")
for _слово in ("медицинский", "переносной", "стерильных"):
    check(f"описание: {_слово}", is_plain_word(_слово), True)
for _марка in ("КМ-Магма", "ОРТОРЕНТ", "Vivid T9", "ГК-100-4", "Кардио-Астел"):
    check(f"обозначение: {_марка}", is_plain_word(_марка), False)

check("дословно — только тождество",
      _same_name("Стол операционный универсальный, с гидравлическим приводом",
                 "Стол операционный универсальный"), False)
check("дословно с точностью до знаков",
      _same_name("Спирометр диагностический", "спирометр  диагностический!"), True)
check("запросы укорачиваются справа",
      _query_variants("Спирометр диагностический, профессиональный"),
      ["Спирометр диагностический, профессиональный", "Спирометр диагностический"])
check("до одного слова не укорачиваем",
      any(len(q.split()) < 2 for q in
          _query_variants("Набор для удаления вшей/профилактики их появления")),
      False)

print("\n наименование позиции каталога: пробелы такие, какие в разметке")
from selectolax.lexbor import LexborHTMLParser
from app.eis.ktru_card import _card_from_block

HL = ('<div class="registry-entry__header-mid__number">32.50.50.190-00001184</div>'
      '<div class="registry-entry__header-mid__h4">Система <span>электро</span>'
      '<span>хирург</span>ическая диа<span>те</span>р<span>м</span>ическая</div>')
WORDS = ('<div class="registry-entry__header-mid__number">32.50.11.000-00000080</div>'
         '<div class="registry-entry__header-mid__h4">\n  <span>Система</span>\n'
         '  <span>рентгеновская</span>\n  <span>диагностическая</span>\n</div>')
for label, html, want in (
        ("подсветка не рвёт слова", HL,
         "Система электрохирургическая диатермическая"),
        ("узлы-слова не склеиваются", WORDS,
         "Система рентгеновская диагностическая")):
    block = LexborHTMLParser(f"<div>{html}</div>").css_first("div")
    check(label, _card_from_block(block).name, want)

print("\n товарный знак: мусор не должен уходить в обозначение")
for bad in ("отсутствует", "не установлено", "№ 1355083", "1355083", "нет", "—",
            "поз. 4", "12-345", "N/A",
            "FALSE", "false", "TRUE", "0", "1", "Да"):
    check(f"мусор: {bad}", _is_real_trademark(bad), False)
for good in ("STOMADENT", "Орфей-М", "WATO EX-35", "ОМ-Дельта-02"):
    check(f"настоящий: {good}", _is_real_trademark(good), True)

SRC = "Установка стоматологическая STOMADENT HARMONY с принадлежностями"

print("\n производитель: только похожее на организацию")
check("ООО", is_company_name('ООО "НОВГОДЕНТ"', 'поставка ООО "НОВГОДЕНТ"'), True)
check("Co., Ltd", is_company_name("Shanghai Handy Medical Co., Ltd.", "..."), True)
check("Co в конце", is_company_name("Foshan Safety Medical Equipment Co", "..."), True)
check("бренд не компания", is_company_name("STOMADENT", SRC), False)
check("страна не компания", is_company_name("Китайская Народная Республика", ""), False)
check("явная подпись", is_company_name("Мед ТеКо", "Аппарат Производитель: Мед ТеКо"), True)

print("\n имя характеристики")
from app.eis.xml_parser import _spec_name

check("заказчик уже поставил двоеточие", _spec_name("Источник света:"),
      "Источник света")
check("двоеточие внутри имени остаётся",
      _spec_name("Регулировка (фокусировка): расстояние 250 мм:"),
      "Регулировка (фокусировка): расстояние 250 мм")
check("обычное имя не меняется", _spec_name("Цветовая температура"),
      "Цветовая температура")

print("\n чистка названия фирмы")
check("страна через запятую",
      clean_company('"Жухай Сайгер Медикал Ко., Лтд", Китай'),
      'Жухай Сайгер Медикал Ко., Лтд')
check("страна и лишняя запятая",
      clean_company('"Жухай Сайгер Медикал Ко., Лтд", Китай,'),
      'Жухай Сайгер Медикал Ко., Лтд')
check("название целиком в кавычках — кавычки снимаем",
      clean_company('"КИРХНЕР & ВИЛЬГЕЛЬМ ГмбХ + Ко. КГ"'),
      'КИРХНЕР & ВИЛЬГЕЛЬМ ГмбХ + Ко. КГ')
check("кавычки внутри названия остаются",
      clean_company('ЗАО "ЗАВОД ЭМА"'), 'ЗАО "ЗАВОД ЭМА"')
check("ёлочки при имени завода тоже снимаем",
      clean_company('«Диксион»'), 'Диксион')
check("ёлочки внутри названия остаются",
      clean_company('ООО «Диксион»'), 'ООО «Диксион»')
check("двойные кавычки внутри не трогаем",
      clean_company('ООО "НПЦ МТ "АРМЕД""'), 'ООО "НПЦ МТ "АРМЕД""')
# чистку применяют дважды за прогон: сразу после реестра и при сведении
# написаний. Второй проход обязан ничего не менять
for _name in ('"Нанкин Майндрэй Био-Медикал Электроникс Ко., Лтд."',
              '"Жухай Сайгер Медикал Ко., Лтд", Китай',
              'ЗАО "ЗАВОД ЭМА"', 'ООО «Диксион»', 'Mindray',
              'ЗАО "Завод ЭМА" Российская Федерация (643)'):
    _once = clean_company(_name)
    check(f"чистка повторяется без изменений: {_once[:34]}",
          clean_company(_once), _once)
check("код страны в скобках",
      clean_company('ЗАО "Завод ЭМА" Российская Федерация (643)'), 'ЗАО "Завод ЭМА"')
check("страна через косую черту",
      clean_company("ООО «Каскад-ФТО» / Россия"), "ООО «Каскад-ФТО»")
check("потерянная кавычка достраивается",
      clean_company('АО "Завод ЭМА'), 'АО "Завод ЭМА"')
check("чистое не портится", clean_company('ООО "НОВГОДЕНТ"'), 'ООО "НОВГОДЕНТ"')
check("страна из двух слов",
      clean_company('ООО «Медстальконструкция», Российская Федерация'),
      'ООО «Медстальконструкция»')
check("страна из трёх слов",
      clean_company('ООО «Ромашка», Китайская Народная Республика'), 'ООО «Ромашка»')
check("страна со словом «Республика»",
      clean_company('ООО «Ромашка», Республика Беларусь'), 'ООО «Ромашка»')
check("страна в самом названии остаётся",
      clean_company('ООО "Корея-Мед"'), 'ООО "Корея-Мед"')
check("«Республика» в названии остаётся",
      clean_company('АО "Республика"'), 'АО "Республика"')
check("ООО и АО не смешиваются",
      company_key('ООО "Ромашка"') != company_key('АО "Ромашка"'), True)

print("\n номер РУ из тега контракта чистится от лишнего")
_p = Position(ru_number="ФСЗ 2007/00296 Россия", name="Облучатель бактерицидный")
_parse_names([Row(ContractMeta(), _p)])
check("страна отрезана от номера РУ", _p.ru_number, "ФСЗ 2007/00296")
_p2 = Position(ru_number="Г004-00110-00/02933371", name="Облучатель бактерицидный")
_parse_names([Row(ContractMeta(), _p2)])
check("незнакомый номер не теряется", _p2.ru_number, "Г004-00110-00/02933371")

print("\n достройка голого обозначения внутри одного номера РУ")


def _rows_for(ru_marks, variants=""):
    return [Row(ContractMeta(), Position(ru_number=ru, mark=mk, ru_variants=variants))
            for ru, mk in ru_marks]


def _marks_after(ru_marks, variants=""):
    rs = _rows_for(ru_marks, variants)
    _extend_bare_marks(rs, RunResult())
    return [r.pos.mark for r in rs]


print("\n чистка обозначения")
from app.pipeline import _tidy_one_mark

check("слово повторено подряд", _tidy_one_mark("ГП-20 МО МО"), "ГП-20 МО")
check("повтор целиком",
      _tidy_one_mark("Электростимулятор Cefar Электростимулятор Cefar"), "Cefar")
check("числа не схлопываем", _tidy_one_mark("ЭМАЛЕД 500 500"), "ЭМАЛЕД 500 500")
check("чистое не портится", _tidy_one_mark("Armed 2-115 П"), "Armed 2-115 П")

print("\n номер РУ со знаком номера")
from app.enrich.nameparse import extract_ru_numbers as _ru_nums

check("номер со знаком узнаётся", _ru_nums("ФС № 2004/1557"), ["ФС 2004/1557"])

print("\n изделие названо именем завода")
from app.pipeline import _brand_named_after_firm


def _brand(ru_name, manufacturer):
    return _brand_named_after_firm(Position(ru_name=ru_name, manufacturer=manufacturer))


check("носилки «Виталфарм»",
      _brand('Носилки мягкие «Виталфарм» по ТУ 9451-035-85535470-2015',
             'ЗАО "Виталфарм"'), "Виталфарм")
check("велоэргометр «ОРТОРЕНТ»",
      _brand('Велоэргометр медицинский "ОРТОРЕНТ" по ОРТО.941319.022 ТУ',
             'ООО "Орторент"'), "ОРТОРЕНТ")
check("«НТК Азимут плюс» — фирма",
      _brand('Кольпоскоп "НТК Азимут плюс"', 'ООО "НТК Азимут плюс"'), "")
check("«НПФ-Медтехника» — фирма",
      _brand('Носилки мягкие "НПФ-Медтехника" по ТУ', 'ООО "НПФ "МЕДТЕХНИКА""'), "")
check("чужое имя в кавычках не берём",
      _brand('Носилки мягкие «Виталфарм» по ТУ', 'ООО "ГЕО МЕД"'), "")
check("без кавычек не берём",
      _brand('Носилки мягкие Виталфарм по ТУ', 'ЗАО "Виталфарм"'), "")

print("\n перенос записи реестра по общему номеру")
from app.pipeline import _transfer_by_number


def _after_number_transfer(items):
    rs = [Row(ContractMeta(), Position(ru_number=ru, nkmi_code=kind,
                                       manufacturer=man, manufacturer_source=src))
          for ru, kind, man, src in items]
    _transfer_by_number(rs, RunResult())
    return [r.pos.manufacturer for r in rs]


check("тот же номер и тот же вид — переносим",
      _after_number_transfer([("ФСР 2010/09816", "131980", 'ООО "СПДС"', "реестр РЗН"),
                              ("ФСР 2010/09816", "131980", "", "")]),
      ['ООО "СПДС"', 'ООО "СПДС"'])
check("другой номер — не переносим",
      _after_number_transfer([("ФСР 2010/09816", "131980", 'ООО "СПДС"', "реестр РЗН"),
                              ("ФСР 2010/09817", "131980", "", "")]),
      ['ООО "СПДС"', ""])
check("другой вид изделия — не переносим",
      _after_number_transfer([("ФСР 2010/09816", "131980", 'ООО "СПДС"', "реестр РЗН"),
                              ("ФСР 2010/09816", "270000", "", "")]),
      ['ООО "СПДС"', ""])
check("выбор по срезу вида опорой не служит",
      _after_number_transfer([("ФСР 2010/09816", "131980", 'ООО "СПДС"',
                               "реестр РЗН (по виду)"),
                              ("ФСР 2010/09816", "131980", "", "")]),
      ['ООО "СПДС"', ""])
check("два разных завода по одному номеру — отказ",
      _after_number_transfer([("ФСР 2010/09816", "131980", 'ООО "СПДС"', "реестр РЗН"),
                              ("ФСР 2010/09816", "131980", 'ООО "Ромашка"', "реестр РЗН"),
                              ("ФСР 2010/09816", "131980", "", "")]),
      ['ООО "СПДС"', 'ООО "Ромашка"', ""])

RU = "РЗН 2024/23069"
check("обрывок достроен — исполнение в перечне реестра",
      _marks_after([(RU, "АРМЕД"), (RU, "АРМЕД"), (RU, "АРМЕД-230")],
                   variants="АРМЕД-230"),
      ["АРМЕД-230", "АРМЕД-230", "АРМЕД-230"])
check("реестр перечня не знает — не достраиваем",
      _marks_after([(RU, "АРМЕД"), (RU, "АРМЕД"), (RU, "АРМЕД-230")]),
      ["АРМЕД", "АРМЕД", "АРМЕД-230"])
from app.pipeline import _addition_listed
check("приписка есть в перечне",
      _addition_listed("АРМЕД", "АРМЕД-230", "АРМЕД-230"), True)
check("приписки в перечне нет",
      _addition_listed("EasyTouch", "EasyTouch ААА 2", "EasyTouch® G"), False)
check("перечня нет — приписке не на что опереться",
      _addition_listed("АРМЕД", "АРМЕД-230", ""), False)
check("исполнение не достраивается",
      _marks_after([(RU, "Armed 2-115 П"), (RU, "Armed 2-115 ПТС")]),
      ["Armed 2-115 П", "Armed 2-115 ПТС"])
check("другая марка не притягивается",
      _marks_after([(RU, "АРМЕД"), (RU, "АРМЕДИКА")]), ["АРМЕД", "АРМЕДИКА"])
check("два продолжения — не выбираем",
      _marks_after([(RU, "АРМЕД"), (RU, "АРМЕД-230"), (RU, "АРМЕД-500")]),
      ["АРМЕД", "АРМЕД-230", "АРМЕД-500"])
check("разные номера РУ не смешиваются",
      _marks_after([(RU, "АРМЕД"), ("РЗН 2020/1111", "АРМЕД-230")]),
      ["АРМЕД", "АРМЕД-230"])
_LIST = [Row(ContractMeta(), Position(ru_number=RU, mark=mk,
                                      ru_variants="P3-i; P3-t; E9-i"))
         for mk in ("Lifedent", "Lifedent Р3-с")]
_extend_bare_marks(_LIST, RunResult())
check("при перечне исполнений не достраиваем",
      [r.pos.mark for r in _LIST], ["Lifedent", "Lifedent Р3-с"])
_NAMED = [Row(ContractMeta(), Position(
    ru_number=RU, mark=mk,
    ru_registry_name="Установка стоматологическая Appollo, модели: I, BI, II, "
                     "BII, III, BIII, IV, BIV, V, BV, с принадлежностями"))
    for mk in ("Appollo", "Appollo BIII")]
_extend_bare_marks(_NAMED, RunResult())
check("перечень в наименовании реестра тоже считается",
      [r.pos.mark for r in _NAMED], ["Appollo", "Appollo BIII"])

print("\n мусор и обрывки вместо обозначения")

from app.enrich.nameparse import (is_type_fragment, parse_name,
                                  variants_in_registry_name)
from app.enrich.verify import trim_edges
from app.pipeline import _canon_mark_case, _is_spec_sheet

_STOM = ["Установка стоматологическая"]
check("обрывок родового слова маркой не считается",
      parse_name("РЗН 2016/5045 Уста1:1овка стоматологическая в исполнениях: "
                 "«SL8100», «SL8200», «SL8300»", _STOM).full, "")
check("настоящая марка не задета", is_type_fragment("KLT-6220", _STOM), False)
check("марка без цифры не задета", is_type_fragment("Устамед", _STOM), False)
check("парная скобка не срезается", trim_edges("330 (Люкс)"), "330 (Люкс)")
check("обрамляющая пара снимается", trim_edges("(Люкс)"), "Люкс")
check("непарная скобка снимается", trim_edges("330 (Люкс"), "330 (Люкс")
check("характеристики не товарный знак",
      _is_spec_sheet("Верхнее положение кресла: <= 80 СМ Интраоральная камера: Нет"),
      True)
check("настоящий знак не задет", _is_spec_sheet("GreenMed"), False)
check("буква исполнения перед числом сохраняется",
      parse_name("ФСЗ 2010/07742 Установка стоматологическая AY "
                 "с принадлежностями: вариант исполнения А 1000", _STOM).full,
      "AY А 1000")
check("предлог перед числом маркой не становится",
      parse_name("Установка стоматологическая с 2 креслами", _STOM).full, "")
check("приписка о заводе не съедает исполнение",
      parse_name("Установка стоматологическая SILVERFOX, варианты исполнения: "
                 "8000C-SRS0, \"СИЛЬВЕРФОКС КОРПОРЕЙШН ЛИМИТЕД\", "
                 "Китайская Народная Республика.", _STOM).full,
      "SILVERFOX 8000C-SRS0")
check("описание по первой запятой не режется",
      parse_name("Датчик цифровой дентальной визуализации, интраоральный HDR-500 "
                 "с принадлежностямиПроизводитель:«Shanghai Handy Medical "
                 "Equipment Co., Ltd.»", _STOM).full, "HDR-500")
check("перечень исполнений в наименовании реестра виден",
      variants_in_registry_name("Установка стоматологическая в исполнениях: "
                                "«SL8100», «SL8200», «SL8300»"),
      ["SL8100", "SL8200", "SL8300"])
check("обычное наименование перечнем не считается",
      variants_in_registry_name("Установка стоматологическая GREENMED"), [])
check("перечень словами тоже перечень",
      parse_name("Установка стоматологическая Appollo, модели I, BI, II, BII, "
                 "III, BIII, IV, BIV, V, BV", _STOM).full, "Appollo")
check("названное исполнение остаётся при себе",
      parse_name("Установка стоматологическая Сingol, модель X5 с нижней подачей "
                 "инструментов, с принадлежностями", _STOM).full, "Сingol X5")
check("номер РУ за перечнем не пункт",
      variants_in_registry_name("Установка стоматологическая, варианты "
                                "исполнения: AJ15;2010/07225"), [])
_HOMO = [Row(ContractMeta(), Position(mark=mk)) for mk in
         ("CINGOL", "CINGOL X3", "Сingol X5")]
_canon_mark_case(_HOMO)
check("омоглиф сведён к латинице",
      [r.pos.mark for r in _HOMO], ["CINGOL", "CINGOL X3", "CINGOL X5"])
_TIE = [Row(ContractMeta(), Position(mark=mk)) for mk in
        ("KaVo Estetica E30 S", "KaVo Estetica Е30 TM")]
_canon_mark_case(_TIE)
check("при ничьей побеждает латиница",
      [r.pos.mark for r in _TIE], ["KaVo Estetica E30 S", "KaVo Estetica E30 TM"])
_MIX = [Row(ContractMeta(), Position(mark=mk)) for mk in ("МЕДИКС", "МEДИКС")]
_canon_mark_case(_MIX)
check("смешанный алфавит выправляется", [r.pos.mark for r in _MIX],
      ["МЕДИКС", "МЕДИКС"])

print("\n единое написание обозначения — только регистр букв")

_CASE = [Row(ContractMeta(), Position(mark=mk)) for mk in
         ("Greenmed GD-S200", "GreenMED S300", "Greenmed GD-S800")]
_canon_mark_case(_CASE)
check("написание завода сведено",
      [r.pos.mark for r in _CASE],
      ["Greenmed GD-S200", "Greenmed S300", "Greenmed GD-S800"])
_KEEP = [Row(ContractMeta(), Position(mark=mk)) for mk in ("АРМЕД-230", "АРМЕД")]
_canon_mark_case(_KEEP)
check("буквы и цифры не трогаются", [r.pos.mark for r in _KEEP],
      ["АРМЕД-230", "АРМЕД"])
print("\n объединение производителей")
import tempfile
from app import groups as _groups

check("организационная форма не мешает",
      _groups.key('ООО "ДИКСИОН"') == _groups.key("Диксион, ООО"), True)
check("кавычки и регистр не мешают",
      _groups.key('ЗАО "ЗАВОД ЭМА"') == _groups.key("завод эма"), True)
check("латиница остаётся отдельной фирмой",
      _groups.key("DIXION") == _groups.key("Диксион"), False)
check("разные заводы не сливаются",
      _groups.key("Завод ЭМА") == _groups.key("НПЦ МТ АРМЕД"), False)

_was = _groups.FILE
_groups.FILE = pathlib.Path(tempfile.gettempdir()) / "t_groups.json"
try:
    _groups.FILE.unlink(missing_ok=True)
    _groups.merge(["Диксион", "DIXION"], "Диксион")
    check("объединение запомнилось",
          _groups.index().get(_groups.key("DIXION")), "Диксион")
    _groups.rename("Диксион", "ООО «Диксион»")
    check("переименование дошло до обоих написаний",
          _groups.index().get(_groups.key("Диксион")), "ООО «Диксион»")
    _groups.merge(["ООО «Диксион»", "Dixion Group"])
    check("третье написание доклеилось",
          len(_groups.load().get("ООО «Диксион»", [])), 3)
    _groups.split(["DIXION", "Диксион", "Dixion Group"])
    check("разъединение очистило список", _groups.load(), {})
finally:
    _groups.FILE.unlink(missing_ok=True)
    _groups.FILE = _was


print("\n оценка времени прогона")
from app.timing import DEFAULT, SLOWDOWN, human, pace, per_contract

check("обещание с запасом на замедление ЕИС", pace() > per_contract(), True)
# 612 контрактов обещали за 29 минут (2.82 с на контракт), а шло больше часа
_promise = 612 * 2.82 * SLOWDOWN / 60
check("612 контрактов — не полчаса", 45 <= _promise <= 65, True)
check("на новой машине обещание тоже с запасом", DEFAULT * SLOWDOWN > 4, True)
check("секунды по-человечески", human(30), "меньше минуты")
check("минуты склоняются", human(300), "около 5 минут")
check("две минуты", human(120), "около 2 минут")
check("час", human(3600), "около часа")

print("\n название завода не должно оставаться в обозначении")
FOSHAN = ('Foshan Safety Medical Equipment Co., Ltd. '
          '("Фошань Сейфти Медикал Эквипмент Ко., Лтд.")')
check("хвост с заводом отрезан",
      _cut_company_tail("Mercury Safety Foshan Safety Medical Equipment Co", [FOSHAN]),
      "Mercury Safety")
check("чистое обозначение не трогаем",
      _cut_company_tail("Mercury Safety", [FOSHAN]), "Mercury Safety")
check("фирма в кавычках после марки",
      _cut_company_tail("Элэскулап Мед ТеКо", ['ООО "Мед ТеКо"']), "Элэскулап")

print("\n различающие слова и сходство")
check("родовое без признаков", distinctive_tokens("Носилки медицинские"), [])
check("марка в кавычках", "Орфей-М" in distinctive_tokens("Аппарат «Орфей-М»"), True)
check("дефис не сильный", strong_tokens("Аппарат Орфей-М"), [])
check("родовое не совпадает",
      similarity("Носилки медицинские",
                 "Носилки медицинские для скорой помощи модели АБВ") < 0.62, True)

from app.eis.documents import is_amended
from app.eis.nmck import parse_nmck
from app.report.analysis import _contracts_nom, discount_note, discount_stats

print("\n начальная цена из извещения")
_NM_HTML = ('<span class="section__title">Начальная (максимальная) цена контракта</span>'
            '<span class="section__info">1 234 567,89</span>')
check("цена вынута", parse_nmck(_NM_HTML).value, 1234567.89)
check("нет цены — пусто", parse_nmck("<div>ничего</div>").value, None)
_NM_TWO = _NM_HTML + _NM_HTML.replace("1 234 567,89", "2 000 000,00")
check("совместная закупка: цену брать нельзя", parse_nmck(_NM_TWO).value, None)
check("и об этом сказано", bool(parse_nmck(_NM_TWO).note), True)

print("\n цена изменена доп. соглашением")
check("контракт с учётом ДС",
      is_amended("Электронный контракт с учетом доп соглашений 1,2.xml"), True)
check("обычный контракт", is_amended("Электронный контракт.xml"), False)
check("само доп. соглашение не берём", is_amended("Доп соглашение №1.xml"), False)

print("\n снижение по контрактам")
_C1 = ContractMeta(reestr_number="1" * 19, nmck=100.0, contract_price=80.0,
                   placing_way="Электронный аукцион")
_C2 = ContractMeta(reestr_number="2" * 19, nmck=100.0, contract_price=100.0,
                   placing_way="Электронный аукцион")
_C3 = ContractMeta(reestr_number="3" * 19, nmck=100.0, contract_price=100.0,
                   placing_way="Закупка у единственного поставщика")
_DROP_ROWS = [Row(m, Position()) for m in (_C1, _C2, _C3)]
_D = discount_stats(_DROP_ROWS)
check("медиана считается только по снижавшим", _D["median"], 20.0)
check("контракт по начальной цене учтён как известный", _D["known"], 2)
check("без торгов — отдельно", _D["no_bidding"], 1)
check("контрактов всего", _D["contracts"], 3)
check("пояснение написано", bool(discount_note(_D)), True)
check("склонение: 1 контракт", _contracts_nom(1), "контракт")
check("склонение: 2 контракта", _contracts_nom(2), "контракта")
check("склонение: 5 контрактов", _contracts_nom(5), "контрактов")
check("склонение: 11 контрактов", _contracts_nom(11), "контрактов")

check("снижение в процентах", _C1.discount_pct, 20.0)
check("снижение в рублях", _C1.discount, 20.0)
check("без торгов снижения нет", _C3.discount_pct, None)
check("в ячейку отчёта уходит пояснение", _C3.discount_cell, "торгов не было")
check("а при торгах — число", _C1.discount_cell, 20.0)

from app.pipeline import _registry_lists_several

for _name, _several in [
    ("Весы напольные медицинские электронные ВМЭН-150, ВМЭН-200 "
     "по ТУ 9441-022-00226454-2005", True),
    ("Обеззараживатель-очиститель фотокаталитический воздуха Аэролайф, "
     "модели Е, О, П, Ю, М-100 по ТУ", True),
    ("Камеры ультрафиолетовые для хранения стерильных инструментов «УФК-1», "
     "«УФК-2», «УФК-3» по ТУ", True),
    ("Электрокардиограф 3-6-12 канальный с регистрацией ЭКГ в ручном "
     "и автоматическом режимах ЭК12Т-01-Р-Д", False),
    ("Светильник смотровой передвижной «ЭМАЛЕД 101» "
     "по ТУ 27.40.39-018-46655261-2020", False),
    ("Стерилизатор паровой ГКа-25-ПЗ по ТУ 9451-015-41457390-2004", False),
    ("Шкафы медицинские марки «КМ-Магма» по ТУ 9452-002-32494920-2008", False),
    ("Пульсоксиметр напалечный серии MD300C, с принадлежностями", False),
    ("Аппарат для ДМВ-терапии ДМВ-02 \"Солнышко\" "
     "по ТУ 9444-013-25616222-2006", False),
]:
    check(("реестр называет несколько исполнений: " if _several else
           "одно изделие, а не перечень: ") + _name[:46],
          _registry_lists_several(_name), _several)


from app.eis.xml_parser import clean_trademark

check("код НКМИ из товарного знака убран",
      clean_trademark("Носилки портативные (объект закупки является "
                      "медицинским изделием, код НКМИ: 114030: Носилки "
                      "портативные)"),
      "Носилки портативные")
check("«Товарный знак: Отсутствует» снимается",
      clean_trademark("Очиститель воздуха (объект закупки является "
                      "медицинским изделием, код НКМИ: 152690: Очиститель) "
                      "Товарный знак: Отсутствует"),
      "Очиститель воздуха")
check("номер исполнения в знаке не теряется",
      clean_trademark("Аппарат магнитотерапевтический \"АЛМАГ-02\" "
                      "по ГИКС.941519.104 ТУ. Товарный знак: АЛМАГ"),
      "Аппарат магнитотерапевтический \"АЛМАГ-02\" по ГИКС.941519.104 ТУ. "
      "Товарный знак: АЛМАГ")
check("«номер товарного знака» — не подпись поля",
      clean_trademark("АКСИ +, Регистрационный номер товарного знака: 280293"),
      "АКСИ +, Регистрационный номер товарного знака: 280293")
check("чистый знак не трогаем", clean_trademark("OMRON"), "OMRON")


from app.enrich.kindmatch import build_index, candidate_tokens
from app.enrich.rzn import RznRecord

_recs = [RznRecord(ru_number="ФСР 1", ru_name="Установка «Аэролайф-Л»",
                   models_description="модели L5515 (вид 152690)",
                   producer="ООО «Аэролайф»"),
         RznRecord(ru_number="ФСР 2", ru_name="Установка «Поток»",
                   models_description="исполнение П-1", producer="ООО «Поток»")]
_idx = build_index(_recs, "Очиститель воздуха", "152690")
check("код вида в признаки не идёт",
      [t for t in candidate_tokens(_idx, "код НКМИ: 152690") if "152690" in t],
      [])


from app.enrich.nameparse import (clean_number, extract_tu,
                                  variants_from_registry)

check("длинное тире в номере — на дефис",
      clean_number("32.50.30–003-68690950-2014"), "32.50.30-003-68690950-2014")
check("буква, приклеенная к году, снимается",
      clean_number("9452-001-55307168-2004в"), "9452-001-55307168-2004")
check("латинская буква к году — тоже",
      clean_number("9444-011-34711238-2003I"), "9444-011-34711238-2003")
check("пробел внутри номера ЕРУЛ снимается",
      clean_number("Г004- 00110-00/03939090"), "Г004-00110-00/03939090")
check("номер, разорванный пробелом, сшивается",
      extract_tu("ТУ 9441-0 03-94382367-2010"), "9441-003-94382367-2010")
check("количество к году не приклеивается",
      extract_tu("по ТУ 9444-013-25616222-2006 5 шт"), "9444-013-25616222-2006")
check("целый номер не портится",
      extract_tu("ТУ 26.60.13-011-52120251-2017"), "26.60.13-011-52120251-2017")


from app.enrich.rzn import _mend_bare_opf

_ADDR = "420101, Казань, ул. Дубравная, д. 30, кв. 69"
check("имя предпринимателя восстановлено из карточки завода",
      _mend_bare_opf({"name": "ИП ", "actualAddress": _ADDR},
                     {"name": "ИП Гумеров Ренат Флунович",
                      "actualAddress": _ADDR}),
      "ИП Гумеров Ренат Флунович")
check("при чужом адресе не восстанавливаем",
      _mend_bare_opf({"name": "ИП ", "actualAddress": _ADDR},
                     {"name": "ИП Иванов", "actualAddress": "Москва"}), "ИП")
check("при чужой ОПФ не восстанавливаем",
      _mend_bare_opf({"name": "ИП ", "actualAddress": _ADDR},
                     {"name": "ООО \"Завод\"", "actualAddress": _ADDR}), "ИП")
check("нормальное имя держателя не трогаем",
      _mend_bare_opf({"name": "ООО \"Ромашка\"", "actualAddress": _ADDR},
                     {"name": "ООО \"Ромашка\"", "actualAddress": _ADDR}),
      "ООО \"Ромашка\"")


from app.enrich.kindmatch import evidence_of

_recs2 = [RznRecord(ru_number="РЗН 1", ru_name="Шкаф медицинский HILFE",
                    models_description="МД 2 1780/SG", producer="ООО «Промет»"),
          RznRecord(ru_number="РЗН 2", ru_name="Шкаф медицинский ПАКС",
                    models_description="ШМ-01", producer="ООО «Пакс»")]
_idx2 = build_index(_recs2, "Шкаф медицинский")
check("признаки подтверждения возвращаются списком",
      bool(evidence_of(_idx2, _recs2[0], "МД 2 1780/SG", "HILFE", "")), True)
check("для чужой записи признаков нет",
      evidence_of(_idx2, _recs2[1], "МД 2 1780/SG", "HILFE", ""), [])


from app.pipeline import _tidy_one_mark

for _raw, _want in [
    ("1. СТМ MS", "СТМ MS"),
    ("2 FotonFLY 5М", "FotonFLY 5М"),
    ("12. ЭМАЛЕД 500", "ЭМАЛЕД 500"),
    ("2) АРМЕД", "АРМЕД"),
    ("75 ПЗ", "75 ПЗ"),
    ("3.5 Люкс", "3.5 Люкс"),
    ("1.5 Т", "1.5 Т"),
    ("5МТ-01", "5МТ-01"),
    ("100-01 П", "100-01 П"),
]:
    check(f"номер пункта: {_raw}", _tidy_one_mark(_raw), _want)


check("«1шт» в значении — это состав, а не исполнение",
      variants_from_registry("1. медицинский перевязочный СМПэ-02 1шт"), [])


from app.enrich.verify import is_initialism

for _s, _f, _same in [
    ('ООО МК "АСК"', 'ООО Медицинская Компания "АСК"', True),
    ('ООО НПФ "Медтехника"', 'ООО Научно-производственная фирма "Медтехника"',
     True),
    ('АО "ГРПЗ"', 'АО "Государственный Рязанский приборный завод"', True),
    ('ООО МК "АСК"', 'ООО Медицинская Компания "Бета"', False),
    ('ООО ТД "Альфа"', 'ООО Торговый Дом "Бета"', False),
    ('ООО "Оптимед"', 'ООО "Оптимех"', False),
]:
    check(("одна фирма: " if _same else "разные фирмы: ") + _s[:34],
          is_initialism(_s, _f), _same)
check("руководство по эксплуатации — не модель",
      variants_from_registry("Руководство по эксплуатации 1 экз"), [])


print("\n обновление программы")

import asyncio
import hashlib
import tempfile

from app import update as upd

check("новая версия видна", upd._newer("v1.0.5", "1.0.4"), True)
check("своя версия не новее", upd._newer("v1.0.4", "1.0.4"), False)

_assets = [
    {"name": "Medizdeliya-1.0.5.exe", "size": 21_000_000,
     "browser_download_url":
         "https://github.com/o/r/releases/download/v1.0.5/Medizdeliya-1.0.5.exe"},
    {"name": "Medizdeliya-1.0.5.exe", "size": 99_000_000,
     "browser_download_url": "https://example.com/podmena.exe"},
    {"name": "notes.txt", "size": 900,
     "browser_download_url": "https://github.com/o/r/releases/download/v1.0.5/notes.txt"},
]
check("файл берут только с github", upd._pick_asset(_assets).get("size"), 21_000_000)
check("посторонних адресов нет", upd._pick_asset(_assets[1:2]), {})


def _swap_run(*, digest_ok: bool) -> tuple[bytes, bool, dict]:
    """Обновление в песочнице: настоящей загрузки нет, подмена настоящая."""

    body = b"MZ" + b"\x01" * upd.MIN_SIZE
    with tempfile.TemporaryDirectory() as tmp:
        exe = pathlib.Path(tmp) / "Medizdeliya.exe"
        exe.write_bytes(b"MZ" + b"\x00" * upd.MIN_SIZE)
        digest = hashlib.sha256(body).hexdigest()
        was = (upd.exe_path, upd.check, upd._asset, upd._installed)
        upd.exe_path = lambda: exe
        upd.check = lambda: asyncio.sleep(0, {"new": True, "latest": "1.0.5"})
        upd._asset = {"url": "https://github.com/o/r/releases/download/v1.0.5/x.exe",
                      "size": len(body),
                      "digest": "sha256:" + (digest if digest_ok else "0" * 64)}
        upd._installed = ""

        async def _fake(url, target):
            target.write_bytes(body)
            return hashlib.sha256(body).hexdigest(), len(body), body[:2]

        upd._download = _fake
        try:
            answer = asyncio.run(upd.install())
            left = list(exe.parent.glob("*.exe.old*"))
            upd.sweep()
            return exe.read_bytes(), bool(left), answer
        finally:
            upd.exe_path, upd.check, upd._asset, upd._installed = was


_body, _kept, _answer = _swap_run(digest_ok=True)
check("новый файл встал на место старого", _body[:3], b"MZ\x01")
check("прежняя версия отложена и убрана", _kept, True)
check("обновление отчиталось", _answer.get("ok"), True)

_body, _kept, _answer = _swap_run(digest_ok=False)
check("сумма не сошлась — файл не тронут", _body[:3], b"MZ\x00")
check("сумма не сошлась — сказано об этом", _answer.get("ok"), False)


section("3. Разбор наименования — примеры пользователя")

from app.enrich.nameparse import (extract_ru_numbers, parse_name,
                                  pick_main_ru, variants_from_registry)

STOM = ["Установка стоматологическая"]
LAMP = ["Светильник передвижной для проведения осмотра/терапевтических процедур"]
TONO = ["Аппарат для измерения артериального давления электрический "
        "с ручным нагнетением, портативный"]

CASES = [
    ("РЗН 2021/13711 Установка стоматологическая Mercury Safety с принадлежностями, "
     "в вариантах исполнения: II. Установка стоматологическая C2", STOM, "Mercury Safety C2"),
    ("Установка стоматологическая STERN WEBER с принадлежностями: Установка "
     "стоматологическая STERN WEBER, вариант исполнения S200 CONTINENTAL",
     STOM, "STERN WEBER S200 CONTINENTAL"),
    ("Установка стоматологическая DIPLOMAT с принадлежностями, модель ADEPT, "
     "вариант исполнения DA 290", STOM, "DIPLOMAT ADEPT (DA 290)"),
    ("Установка стоматологическая ANTHOS, вариант исполнения A3 PLUS", STOM, "ANTHOS A3 PLUS"),
    ("РЗН 2015/2631 Установка стоматологическая, вариант исполнения: INTEGO", STOM, "INTEGO"),
    ("Установка стоматологическая STOMADENT HARMONY с принадлежностями",
     STOM, "STOMADENT HARMONY"),
    ("Стоматологическая установка KLT, исполнения KLT-6210", STOM, "KLT-6210"),
    ("Установка стоматологическая MERCURY по ТУ 9452-001-90933304-2020", STOM, "MERCURY"),
    ("Аппарат наркозно-дыхательный WATO с принадлежностями, варианты исполнения WATO EX-35",
     ["Аппарат ингаляционной анестезии"], "WATO EX-35"),
    ("РЗН 2023/19677 Установка стоматологическая в вариантах исполнения:", STOM, ""),
    ("РЗН 2016/4979 (ЕРУЛ - Г004-00 Премьер 05)", STOM, "Премьер 05"),
    ("Установка стоматологическая Mercury 550", STOM, "Mercury 550"),
    ("Установка стоматологическая Mercury 550 (объект закупки является медицинским "
     "изделием, код НКМИ: 119630: Установка стоматологическая)", STOM, "Mercury 550"),
    ("Установка стоматологическая, варианты исполнения: AJ15;2010/07225", STOM, "AJ15"),
    ("Светильники медицинские АРМЕД по ТУ 9452-005-13391002-2014", LAMP, "АРМЕД"),
    ("Светильник медицинский АРМЕД по ТУ 9452-005- 13391002-2014, "
     "вариант исполнения: АРМЕД-ЛД-2 ЛЕД", LAMP, "АРМЕД-ЛД-2 ЛЕД"),
    ("Светильник бестеневой операционный «Конвелар» по ТУ 9452-016-74487176-2008 "
     "с принадлежностями: Передвижной: «Конвелар 1607ЛЭД»", LAMP, "Конвелар 1607ЛЭД"),
    ("Светильник передвижной \"ЭМАЛЕД\" в вариантах исполнения по ТУ "
     "9452-015-46655261-2011", LAMP, "ЭМАЛЕД"),

    ("Б. Браун", TONO, "Б. Браун"),
    ("Б. БРАУН", TONO, "Б. БРАУН"),
    ("Измерители артериального давления и частоты пульса автоматические OMRON: "
     "М2 Basic (HEM-7121-ALRU), М2 Basic (HEM-7121-RU), М2 Basic (HEM-7121-ARU), "
     "М2 Classic (HEM-7122-ALRU), М2 Classic (HEM-7122-LRU), М3 Eco (HEM-7131-ARU), "
     "М3 Expert (HEM-7132-ALRU), М3 Family (HEM-7133-ALRU), с принадлежностями",
     TONO, "OMRON"),
    ("РЕНЕКС Номер регистрации товарного знака: 210801", LAMP, "РЕНЕКС"),
]

print(f"  {'НАИМЕНОВАНИЕ':56s} | {'ОБОЗНАЧЕНИЕ':28s} | итог")
print("  " + "-" * 96)
for name, hints, want in CASES:
    got = parse_name(name, hints).full
    ok = got == want
    if not ok:
        FAILED.append(f"разбор: {name[:60]}\n      получено {got!r}, ждали {want!r}")
    print(f"  {name[:56]:56s} | {got[:28]:28s} | {'ok' if ok else 'FAIL'}")

print("\n номера РУ")
multi = ("Аппарат для ингаляционного наркоза «Орфей-М-03» по РУ РЗН 2018/7255 от 03 декабря "
         "2024 года и Компрессор медицинский КМ с принадлежностями по РУ РЗН 2018/6988")
nums = extract_ru_numbers(multi)
check("оба номера найдены", nums, ["РЗН 2018/7255", "РЗН 2018/6988"])
check("основной выбран верно",
      pick_main_ru(multi, nums, ["Аппарат для ингаляционного наркоза"]), "РЗН 2018/7255")

print("\n варианты исполнения из реестра")
DESC = ("в вариантах исполнения:<br>I. Установка стоматологическая, серия Х1, вариант "
        "исполнения X1, в составе:<br>1. Светильник светодиодный навесной для серии Х1 - 1 шт."
        "<br>II. Установка стоматологическая, серия 3, вариант исполнения X3")
got = variants_from_registry(DESC, STOM)
print("   ", got)
check("вариант X1 найден", any("X1" in g or "Х1" in g for g in got), True)
check("состав комплектации отброшен",
      any("шт" in g.lower() or "светильник" in g.lower() for g in got), False)

print("\n вид изделия: реестр и КТРУ зовут вещь разными словами")
from app.enrich.rzn import _same_device_kind

check("«Прибор» принят за «Аппарат»",
      _same_device_kind("Прибор для измерения артериального давления "
                        "и частоты пульса цифровой LD", TONO), True)
check("«Измеритель» принят за «Аппарат»",
      _same_device_kind("Измеритель артериального давления и частоты пульса "
                        "полуавтоматический OMRON", TONO), True)
check("носилки за тонометр не приняты",
      _same_device_kind("Носилки медицинские мягкие бескаркасные", TONO), False)
check("рентген за тонометр не принят",
      _same_device_kind("Система рентгеновская диагностическая стационарная", TONO), False)
check("короткая подсказка по-прежнему строгая",
      _same_device_kind("Установка терапевтическая ударноволновая", STOM), False)

print("\n срез реестра по коду вида: выбор из закрытого списка")
from app.enrich.kindmatch import (build_index, candidate_tokens,
                                  confirms, match_rules)
from app.enrich.rzn import RznRecord

SLICE = [
    RznRecord(producer='"Би.Велл Свисс АГ"', declarant='АО "Альфа-Медика"',
              ru_number="РЗН 2016/4964", status="Действует",
              ru_name="Приборы для измерения артериального давления и частоты пульса",
              models_description="варианты исполнения: PRO-30, PRO-33, MED-55"),
    RznRecord(producer='"Литл Доктор Интернешнл (С) Пте. Лтд."',
              producer_eng="Little Doctor Electronic (Nantong) Co., Ltd.",
              declarant='ООО "Фирма К и К"', ru_number="ФСЗ 2012/11647",
              status="Действует",
              ru_name="Прибор для измерения артериального давления и частоты пульса цифровой LD",
              models_description="вариант исполнения LD2, вариант исполнения LD3а"),
    RznRecord(producer="ОМРОН ХЕЛСКЭА Ко., Лтд.", declarant='ЗАО "КомплектСервис"',
              declarant_inn="7703012997", ru_number="ФСЗ 2008/02160",
              status="Действует",
              ru_name="Измеритель артериального давления и частоты пульса "
                      "полуавтоматический OMRON M1 Classic"),
    RznRecord(producer="OMRON HEALTHCARE Co., Ltd.", declarant='ЗАО "КомплектСервис"',
              ru_number="ФСЗ 2008/02162", status="Недействительно",
              ru_name="Измеритель артериального давления и частоты пульса "
                      "полуавтоматический OMRON M1 Classic"),
]
IDX = build_index(SLICE, TONO[0])

check("марка нашла свою регистрацию",
      match_rules(IDX, "B.Well PRO-30", "B.Well", "").record.ru_number, "РЗН 2016/4964")
check("исполнение из перечня тоже",
      match_rules(IDX, "Little Doctor LD2", "", "").record.ru_number, "ФСЗ 2012/11647")
check("одного короткого артикула мало", match_rules(IDX, "LD2", "", "").ok, False)
from app.enrich.kindmatch import one_firm

check("кириллица и латиница одного завода не спорят",
      one_firm([SLICE[2], SLICE[3]]), True)
check("разные заводы остаются разными",
      one_firm([SLICE[0], SLICE[2]]), False)
check("при выборе внутри завода берётся действующее РУ",
      match_rules(IDX, "OMRON Classic", "", "").record.ru_number, "ФСЗ 2008/02160")
check("по одному виду изделия выбора не делаем",
      match_rules(IDX, "", "", "Аппарат для измерения артериального давления").ok, False)
check("слово вида не идёт в признаки",
      any("давлен" in t.lower() for t in candidate_tokens(IDX, "", "", TONO[0])), False)
check("выбор без основания отбрасывается",
      confirms(IDX, SLICE[0], "Armed YE660B", "", ""), False)
check("выбор с основанием принимается",
      confirms(IDX, SLICE[1], "Little Doctor LD2", "", ""), True)

from app.enrich.kindmatch import contradicts

OMRON_M2 = ("Измерители артериального давления и частоты пульса автоматические "
            "OMRON: М2 Basic (HEM-7121-ALRU), М2 Classic (HEM-7122-ALRU), "
            "М3 Family (HEM-7133-ALRU), с принадлежностями")
M1 = RznRecord(producer="ОМРОН ХЕЛСКЭА Ко., Лтд.", ru_number="ФСЗ 2008/02157",
               status="Действует",
               ru_name="Измеритель артериального давления и частоты пульса "
                       "полуавтоматический OMRON M1 Eco (HEM-4011C-RU)")
check("чужой артикул того же завода — отказ", contradicts(M1, "OMRON", "", OMRON_M2), True)
check("и модель такой выбор сделать не может",
      confirms(build_index([M1], TONO[0]), M1, "OMRON", "", OMRON_M2), False)
check("номер ТУ за артикул не принимается",
      contradicts(RznRecord(ru_name="Светильник АРМЕД по ТУ 9452-005-13391002-2014"),
                  "АРМЕД", "", "Светильник АРМЕД по ТУ 9452-016-74487176-2008"), False)
check("молчание реестра об артикулах — не спор",
      contradicts(RznRecord(ru_name="Установка стоматологическая Mercury Safety "
                                    "с принадлежностями"),
                  "Mercury Safety М8", "", ""), False)


section("4. Корпус печатных форм из Downloads")

from app.eis.html_parser import parse_print_form

FORMS = sorted(p for p in glob.glob(os.path.join(CORPUS, "*.html"))
               if "Печатная форма" in os.path.basename(p))
if not FORMS:
    NOTES.append(f"корпус печатных форм не найден в {CORPUS} — раздел 4 пропущен")
    print(f"  корпуса нет ({CORPUS}) — пропускаю")
else:
    c = collections.Counter()
    npos = 0
    t0 = time.time()
    for p in FORMS:
        try:
            _meta, poss = parse_print_form(
                open(p, encoding="utf-8", errors="replace").read(), source_ref=p)
        except Exception as e:
            c["исключение"] += 1
            FAILED.append(f"печатная форма {os.path.basename(p)[:40]}: {type(e).__name__}: {e}")
            continue
        if poss:
            c["разобрано"] += 1
            npos += len(poss)
        else:
            c["без позиций"] += 1
    dt = time.time() - t0
    print(f"  файлов: {len(FORMS)}, разобрано {c['разобрано']}, "
          f"без позиций {c['без позиций']} (это формы доп. соглашений)")
    print(f"  позиций: {npos}, за {dt:.1f}с")
    check("исключений при разборе нет", c["исключение"], 0)
    check("разобрано не меньше 600 форм", c["разобрано"] >= 600, True)

    names = []
    for p in FORMS:
        t = re.sub(r"<[^>]+>", " ", open(p, encoding="utf-8", errors="replace").read())
        t = re.sub(r"\s+", " ", t.replace("\xa0", " "))
        for m in re.finditer(
                r"наименование в соответствии с РУ:\s*([^)]{5,400}?)\)\s*(?:Товарн|<|$)", t):
            names.append(m.group(1).strip())
    names = list(dict.fromkeys(names))

    GENERIC = re.compile(r"^(установк|аппарат|систем|стол|кресл|прибор|комплект|набор|"
                         r"устройств|издели|оборудован|медицинск|принадлежн)", re.I)
    found = junk = 0
    junk_examples: list[str] = []
    for n in names:
        got = parse_name(n, STOM).full
        if not got:
            continue
        found += 1
        if GENERIC.match(got) or re.fullmatch(r"[\d\s.\-/]+", got):
            junk += 1
            if len(junk_examples) < 6:
                junk_examples.append(f"{got!r}  <-  {n[:70]}")
    print(f"\n  корпус наименований: {len(names)}")
    print(f"  обозначение определено: {found} ({100 * found / max(1, len(names)):.1f}%)")
    print(f"  подозрительных: {junk}")
    for j in junk_examples:
        print("     ", j)
    check("определяемость не ниже 88%", found >= len(names) * 0.88, True)
    check("подозрительных не больше 3%", junk <= len(names) * 0.03, True)


if not ONLINE:
    section("5. Сеть — пропущено (запустите с --online)")
else:
    import asyncio

    section("5. ЕИС и реестр Росздравнадзора")

    from app.eis.client import EisClient
    from app.eis.documents import fetch_contract_xml
    from app.eis.ktru_card import check_codes
    from app.eis.search import search_ktru
    from app.eis.xml_parser import parse_contract_xml
    from app.enrich.rzn import RznEnricher

    KTRU = "32.50.11.000-00000080"

    async def online() -> None:
        async with EisClient() as client:
            print("\n проверка кодов КТРУ")
            got = await check_codes(client, [KTRU, "11.11.11.111-11111111"])
            by_code = {g.code: g for g in got}
            check("настоящий код подтверждён", by_code[KTRU].ok, True)
            check("наименование получено", bool(by_code[KTRU].name), True)
            check("несуществующий отвергнут", by_code["11.11.11.111-11111111"].ok, False)
            print(f"    {KTRU} — {by_code[KTRU].name} — {by_code[KTRU].contracts} контрактов")

            print("\n поиск контрактов")
            metas, total = await search_ktru(client, KTRU, limit=10)
            check("контракты найдены", len(metas) > 0, True)
            check("реестровый номер 19 цифр",
                  all(re.fullmatch(r"\d{19}", m.reestr_number) for m in metas), True)
            check("дата заключения есть",
                  all(m.conclusion_date for m in metas), True)
            print(f"    всего по коду: {total}, взято {len(metas)}")

            print("\n загрузка и разбор XML")
            ok = 0
            for m in metas[:5]:
                data, note = await fetch_contract_xml(client, m.reestr_number)
                if data is None:
                    print(f"    {m.reestr_number}: {note}")
                    continue
                _meta, poss = parse_contract_xml(data, m)
                if poss:
                    ok += 1
            check("XML разобран хотя бы у 4 из 5", ok >= 4, True)

        print("\n реестр Росздравнадзора")
        RZN_CASES = [
            ("ФСЗ 2011/10543", "", STOM[0], "ВЕРНО"),
            ("РЗН 2019/8714", "Установка стоматологическая DIPLOMAT", STOM[0], "ВЕРНО"),
            ("ФСЗ 2007/00486", "Стоматологические установки Friend Plus", STOM[0], "ВЕРНО"),
            ("", "Комплекс рентгеновский диагностический «Диаком» "
                 "по ТУ 9442-001-86112671-2009",
             "Комплекс рентгеновский диагностический", "ВЕРНО"),
            ("", "Аппарат для УВЧ-терапии УВЧ-60 по ТУ 9444-002-56812193-2002",
             "Аппарат для УВЧ-терапии", "ВЕРНО"),
            ("", "Носилки медицинские", "Носилки медицинские", "ПУСТО"),
            ("", "Стул врача-стоматолога", "Стул врача-стоматолога", "ПУСТО"),
            ("", "Стол манипуляционный", "Стол манипуляционный", "ПУСТО"),
            ("", "Устройство для дренирования", "Устройство для дренирования", "ПУСТО"),
        ]
        from app.enrich.nameparse import extract_tu

        right = false = empty = 0
        async with RznEnricher() as rzn:
            if not await rzn.available():
                NOTES.append("реестр РЗН не отвечает — раздел пропущен")
                print("  реестр не отвечает, пропускаю")
                return
            for ru, name, hint, want in RZN_CASES:
                rec = await rzn.lookup(ru_number=ru, ru_name=name,
                                       tu_number=extract_tu(name), type_hints=[hint])
                producer = rec.producer if rec else ""
                if want == "ВЕРНО":
                    if producer:
                        right += 1
                        mark = "ok  "
                    else:
                        empty += 1
                        mark = "пусто"
                else:
                    if producer:
                        false += 1
                        mark = "ЛОЖНО"
                    else:
                        right += 1
                        mark = "ok  "
                print(f"  {mark} {(name or ru)[:52]:52s} -> {producer[:34]}")
        print(f"\n  верно {right}, пусто {empty}, ЛОЖНЫХ {false}")
        check("ложных совпадений нет", false, 0)
        NOTES.append(f"реестр: верно {right}, пусто {empty}, ложных {false}")

    asyncio.run(online())


print(f"\n{'═' * 74}")
for n in NOTES:
    print(f"  примечание: {n}")
if FAILED:
    print(f"\n  ПРОВАЛЕНО: {len(FAILED)}")
    for f in FAILED:
        print("   -", f)
    sys.exit(1)
print("\n  все проверки пройдены")
