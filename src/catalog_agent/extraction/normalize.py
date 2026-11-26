"""Documented deterministic normalization. Raw values are always kept beside normalized ones."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit

from catalog_agent.contracts import Availability

_PRICE_RE = re.compile(r"^\s*(?:US)?\$?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(\.[0-9]{1,2})?\s*$")


def normalize_price(raw: str | None) -> str | None:
    """'$1,234.50' -> '1234.50'. Returns None unless the raw text is exactly one USD amount."""
    if raw is None:
        return None
    m = _PRICE_RE.match(raw)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    frac = (m.group(2) or ".00")[1:].ljust(2, "0")
    try:
        value = Decimal(f"{whole}.{frac}")
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return f"{value:.2f}"


_PACK_UNITS = {
    "box": "box",
    "bx": "box",
    "case": "case",
    "cs": "case",
    "pk": "pack",
    "pack": "pack",
    "pkg": "pack",
    "each": "each",
    "ea": "each",
    "bag": "bag",
    "bottle": "bottle",
    "btl": "bottle",
    "kit": "kit",
    "roll": "roll",
    "tube": "tube",
    "jar": "jar",
    "vial": "vial",
    "pouch": "pouch",
    "cartridge": "cartridge",
    "carton": "carton",
    "dozen": "dozen",
    "set": "set",
    "syringe": "syringe",
    "tray": "tray",
}
_PACK_RE = re.compile(r"(\d[\d,]*)\s*/\s*([A-Za-z]+)\b")


_BOX_OF_RE = re.compile(r"\b(box|case|pack|bag|carton)\s+of\s+(\d[\d,]*)\b", re.IGNORECASE)
_N_PACK_RE = re.compile(r"\b(\d[\d,]*)\s*-\s*(pack|pk|count|ct)\b", re.IGNORECASE)


def parse_pack(text: str | None) -> tuple[str | None, int | None, str | None]:
    """Pack from the last 'N/unit' token ('300/box'), else 'Box of N' or 'N-pack'. Unknown units keep the label."""
    if not text:
        return None, None, None
    matches = list(_PACK_RE.finditer(text))
    if not matches:
        m = _BOX_OF_RE.search(text)
        if m:
            return m.group(0), int(m.group(2).replace(",", "")), _PACK_UNITS[m.group(1).lower()]
        m = _N_PACK_RE.search(text)
        if m:
            unit = "pack" if m.group(2).lower() in ("pack", "pk") else "each"
            return m.group(0), int(m.group(1).replace(",", "")), unit
        return None, None, None
    m = matches[-1]
    label = m.group(0)
    unit = _PACK_UNITS.get(m.group(2).lower())
    if unit is None:
        return label, None, None
    try:
        qty = int(m.group(1).replace(",", ""))
    except ValueError:
        return label, None, None
    return label, qty, unit


def normalize_availability(raw: str | None) -> Availability:
    if not raw:
        return Availability.UNKNOWN
    t = raw.strip().lower()
    if "schema.org/" in t:
        t = t.rsplit("/", 1)[-1]
    t = t.replace(" ", "").replace("-", "").replace("_", "")
    if t.startswith("outofstock") or t in {"soldout", "discontinued"}:
        return Availability.OUT_OF_STOCK
    if t.startswith("backorder") or t.startswith("preorder"):
        return Availability.BACKORDER
    if t.startswith("limited") or t.startswith("lowstock"):
        return Availability.LIMITED
    if t.startswith("instock"):
        return Availability.IN_STOCK
    return Availability.UNKNOWN


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t or None


def canonicalize_url(url: str, base: str | None = None, keep_query: bool = False) -> str | None:
    """Resolve, drop fragments and (by default) query strings, lowercase host, strip trailing '/'."""
    if not url:
        return None
    absolute = urljoin(base, url) if base else url
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = parts.query if keep_query else ""
    return urlunsplit(("https" if parts.scheme == "https" else "http", parts.netloc.lower(), path, query, ""))
