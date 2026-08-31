

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from ..config import settings

log = logging.getLogger(__name__)

BASE = "https://elk.roszdravnadzor.gov.ru"
CATALOG_EP = ("/public-gateway/nsi-search/public/v1/catalogs"
              "/nomClassifierMedicalRF/records")

NKMI_RE = re.compile(r"^\d{3,8}$")

_CACHE: dict[str, "NkmiKind"] = {}
_LOCK = asyncio.Lock()


@dataclass(slots=True)
class NkmiKind:
    code: str = ""
    name: str = ""
    description: str = ""
    status: str = ""
    found: bool = False
    note: str = ""
    record_id: str = ""


def parse_nkmi_list(text: str) -> list[str]:

    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip(" .,;:—–-")
        if NKMI_RE.match(token) and token not in seen:
            seen.add(token)
            out.append(token)
    return out


async def lookup(code: str) -> NkmiKind:
    code = (code or "").strip()
    if not NKMI_RE.match(code):
        return NkmiKind(code=code, note="код вида — это от трёх до восьми цифр")
    async with _LOCK:
        if code in _CACHE:
            return _CACHE[code]
    res = await _fetch(code)
    async with _LOCK:
        _CACHE[code] = res
    return res


async def _fetch(code: str) -> NkmiKind:
    params = {"size": "10", "page": "0", "term": code}
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.rzn_timeout, connect=15.0),
                verify=False,
                headers={"User-Agent": settings.user_agent,
                         "Accept": "application/json, text/plain, */*",
                         "Referer": f"{BASE}/widget/"}) as client:
            r = await client.get(f"{BASE}{CATALOG_EP}", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("справочник НКМИ %s: %s: %s", code, type(e).__name__, e)
        return NkmiKind(code=code,
                        note="справочник видов медизделий сейчас не отвечает")

    for item in (data.get("content") or []):
        a = item.get("attributeSet") or {}
        if str(a.get("code") or "").strip() != code:
            continue
        return NkmiKind(code=code, name=(a.get("name") or "").strip(),
                        description=(a.get("description") or "").strip(),
                        status=(a.get("status") or "").strip(), found=True,
                        record_id=str(item.get("recordId") or "").strip())
    return NkmiKind(code=code, note="такого кода вида нет в номенклатуре "
                                    "Росздравнадзора — проверьте цифры")
