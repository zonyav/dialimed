

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from selectolax.parser import HTMLParser

from ..config import settings
from .client import EisClient

log = logging.getLogger(__name__)

DOC_TAB = f"{settings.eis_base}/epz/contract/contractCard/document-info.html"
PRINT_FORM = f"{settings.eis_base}/epz/contract/printForm/view.html"

_WITH_AGREEMENTS = re.compile(r"с\s*уч[её]том\s*доп\w*\.?\s*соглашени\w*\s*([\d,\s]*)", re.I)

_AGREEMENT_DOC = re.compile(r"^\s*(?:доп\w*\.?\s*соглашени|дс\s*№?\s*\d)", re.I)

_EXCLUDE = re.compile(
    r"платёжн|платежн|поручени|\bо\s*приемк|\bо\s*приёмк|UPD_|извещени|протокол|"
    r"накладн|\bсч[её]т(?:а|ах|ам|ов|у|е|ом)?\b|\bакт(?:а|ы|ов|у|е|ом)?\b",
    re.I,
)

_SIZE_SUFFIX = re.compile(
    r"\s*\(\s*[\d\s.,]+\s*(?:[кмгт]?б|[kmgt]?b|байт|bytes?)\.?\s*\)\s*$", re.I)

_XML_NAME = re.compile(r"\.xml\s*$", re.I)


@dataclass(slots=True)
class Attachment:
    filename: str
    url: str
    rank: int = -1


def clean_filename(title: str) -> str:
    return _SIZE_SUFFIX.sub("", (title or "").strip()).strip()


def rank_filename(filename: str) -> int:

    fn = (filename or "").strip()
    if not _XML_NAME.search(fn):
        return -1
    if _EXCLUDE.search(fn):
        return -1
    if _AGREEMENT_DOC.search(fn):
        return -1
    m = _WITH_AGREEMENTS.search(fn)
    if m:
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        return 1000 + (max(nums) if nums else 1)
    if re.search(r"электронн\w*\s*контракт", fn, re.I):
        return 500
    if re.search(r"\bконтракт", fn, re.I):
        return 100
    return -1


def is_amended(filename: str) -> bool:

    return rank_filename(filename) >= 1000


def parse_attachments(html: str) -> list[Attachment]:
    tree = HTMLParser(html)
    by_url: dict[str, Attachment] = {}
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if "filestore" not in href:
            continue
        if href.startswith("/"):
            href = settings.eis_base + href
        name = clean_filename(a.attributes.get("title") or "")
        if not name:
            name = a.text(strip=True).split("\n")[0].strip()
        prev = by_url.get(href)
        if prev is None or ("." in name and "." not in prev.filename):
            by_url[href] = Attachment(filename=name, url=href, rank=rank_filename(name))
    return list(by_url.values())


async def fetch_contract_xml(
    client: EisClient, reestr_number: str
) -> tuple[Optional[bytes], str]:
    url = f"{DOC_TAB}?reestrNumber={reestr_number}"
    try:
        html = await client.fetch_text(url)
    except Exception as e:
        return None, f"вкладка документов недоступна: {e}"

    atts = parse_attachments(html)
    if not atts:
        return None, "вложений нет"

    usable = [a for a in atts if a.rank >= 0]
    if not usable:
        names = ", ".join(a.filename for a in atts if a.filename)[:180]
        return None, f"XML контракта нет среди вложений: {names}"

    best = max(usable, key=lambda a: a.rank)
    try:
        body, _ = await client.fetch(best.url)
    except Exception as e:
        return None, f"не удалось скачать {best.filename}: {e}"
    head = body[:400].lstrip()
    if not head.startswith(b"<"):
        return None, f"{best.filename}: это не XML"
    if b"<html" in head[:200].lower() or b"<!doctype html" in head[:200].lower():
        return None, f"{best.filename}: вместо XML пришла HTML-страница"
    return body, best.filename


async def fetch_print_form(client: EisClient, reestr_number: str) -> tuple[Optional[bytes], str]:

    url = f"{DOC_TAB}?reestrNumber={reestr_number}"
    try:
        html = await client.fetch_text(url)
        for a in parse_attachments(html):
            if re.search(r"печатн\w*\s*форм\w*\s*электронн\w*\s*контракт", a.filename, re.I):
                body, _ = await client.fetch(a.url)
                return body, a.filename
    except Exception as e:
        log.debug("ПФ из вложений %s: %s", reestr_number, e)

    try:
        body, _ = await client.fetch(print_form_url(reestr_number))
        return body, "печатная форма (реестр контрактов)"
    except Exception as e:
        return None, f"печатная форма недоступна: {e}"


def print_form_url(reestr_number: str) -> str:
    return f"{PRINT_FORM}?contractReestrNumber={reestr_number}"


_SPEC_SKIP = re.compile(r"платёжн|платежн|поручени|о\s*приемк|о\s*приёмк|подпис|"
                        r"извещени|протокол|\bсч[её]т", re.I)


async def fetch_spec_documents(client: EisClient, reestr_number: str,
                               limit: int = 2) -> list[tuple[str, bytes]]:

    url = f"{DOC_TAB}?reestrNumber={reestr_number}"
    try:
        html = await client.fetch_text(url)
    except Exception as e:
        log.debug("вкладка документов %s: %s", reestr_number, e)
        return []

    seen: set[str] = set()
    picked: list[Attachment] = []
    for a in parse_attachments(html):
        fn = a.filename.lower()
        if not fn.endswith(".docx") or _SPEC_SKIP.search(a.filename):
            continue
        if fn in seen:
            continue
        seen.add(fn)
        picked.append(a)

    out: list[tuple[str, bytes]] = []
    for a in picked[:limit]:
        try:
            body, _ = await client.fetch(a.url)
            out.append((a.filename, body))
        except Exception as e:
            log.debug("вложение %s: %s", a.filename[:40], e)
    return out
