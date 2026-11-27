"""Safco Dental adapter: every Safco-specific URL pattern, selector and parsing rule lives here.

Observed in the M0 spike (2026-11-26):
- /catalog lists the category roots (/catalog/<slug>) in its main content.
- Listing pages render products client-side (Algolia InstantSearch, 12 hits per page);
  pagination links are ?page=N, which robots.txt disallows.
- Product pages embed a schema.org ProductGroup (hasVariant with sku/price/availability) and
  render a grouped variant table (#pdp-grouped .table-grid-pdp) with Item #, Description,
  Stock Availability and quantity-tier prices.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from catalog_agent.contracts import (
    LinkCandidate,
    PageSnapshot,
    PageType,
    ProductCandidate,
    Row,
    Section,
    VariantObservation,
)
from catalog_agent.extraction.normalize import canonicalize_url, clean_text, parse_pack

SOURCE = "safco"

READY_SELECTORS = {
    "discovery": "a[href*='/catalog/']",
    "listing": ".ais-Hits-item, .ais-Hits--empty, .category-cms",
    "product": "#pdp-grouped .product-item-sku",
}
# If the primary signal never appears, these still make a snapshot usable (checked after the wait).
# M5 run: five bone-graft pages never rendered their variant table on any of three attempts, but their
# ProductGroup JSON-LD (with variant SKUs and prices) is always present.
READY_FALLBACK_SELECTORS = {
    "product": 'script[type="application/ld+json"]',
}

# Selectors the parsers depend on, per page template. Repair suggestions (P1, FR-12) propose
# replacements for these; nothing here is ever changed automatically.
FIELD_SELECTORS: dict[str, dict[str, str]] = {
    "product": {"variant_rows": "#pdp-grouped .table-grid-pdp .product-item-sku"},
    "listing": {"listing_products": ".ais-Hits-item a.result"},
}


def field_failures(snapshot: PageSnapshot, template: str) -> list[str]:
    """Fields whose selector matched nothing although the page shows the data exists."""
    soup = soup_for(snapshot)
    failed: list[str] = []
    if template == "product":
        ld = _ld_of_type(soup, "ProductGroup", "Product")
        has_variants = bool(ld and (ld.get("hasVariant") or ld.get("sku")))
        if has_variants and not soup.select(FIELD_SELECTORS["product"]["variant_rows"]):
            failed.append("variant_rows")
    elif template == "listing":
        stats = _text(soup.select_one(".ais-Stats")) or ""
        m = re.search(r"of\s+(\d+)", stats)
        if m and int(m.group(1)) > 0 and not soup.select(FIELD_SELECTORS["listing"]["listing_products"]):
            failed.append("listing_products")
    return failed


def dom_outline(snapshot: PageSnapshot, max_chars: int = 3000) -> str:
    """Bounded tag/id/class outline of the main content (no text), for selector repair."""
    soup = soup_for(snapshot)
    root = soup.select_one("#maincontent") or soup.select_one("main") or soup.body
    lines: list[str] = []

    def walk(el: Tag, depth: int) -> None:
        if len("\n".join(lines)) > max_chars or depth > 14:
            return
        for child in el.find_all(recursive=False):
            if child.name in ("script", "style", "svg", "template", "img", "path"):
                continue
            ident = child.name + (f"#{child['id']}" if child.get("id") else "")
            cls = [c for c in child.get("class", []) if re.fullmatch(r"[A-Za-z][\w-]{1,40}", c)][:4]
            attrs = [f"[{k}]" for k in ("data-attr", "x-text", "itemprop") if child.get(k)]
            lines.append("  " * depth + ident + "".join(f".{c}" for c in cls) + "".join(attrs))
            walk(child, depth + 1)

    if root is not None:
        walk(root, 0)
    return "\n".join(lines)[:max_chars]


# Resources not needed for extraction; blocking them reduces load on third parties and the host.
BLOCKED_RESOURCE_TYPES = ("image", "font", "media")
BLOCKED_URL_PARTS = (
    "google",
    "doubleclick",
    "fullstory",
    "clarity.ms",
    "pinimg",
    "pinterest",
    "nr-data",
    "hubspot",
    "hs-scripts",
    "hsforms",
    "hubapi",
    "nytrng",
    "shop.pe",
    "safeopt",
    "yotpo",
    "facebook",
    "rum.hlx.page",
    "commerce.adobe.io",
)

_BLOCK_TEXT = re.compile(r"(captcha|access denied|are you a robot|unusual traffic|request blocked)", re.IGNORECASE)
_LOGIN_PRICE = re.compile(r"((sign|log)\s*in|login)\s*(to|for)\s*(see|view|get)?\s*(the\s*)?pric", re.IGNORECASE)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


@lru_cache(maxsize=64)
def _cached_soup(snapshot_id: str, html: str) -> BeautifulSoup:
    return _soup(html)


def soup_for(snapshot: PageSnapshot) -> BeautifulSoup:
    return _cached_soup(snapshot.snapshot_id, snapshot.html)


def _text(el: Tag | None) -> str | None:
    if el is None:
        return None
    return clean_text(el.get_text(" ", strip=True))


def _jsonld(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                out.append(item)
    return out


def _ld_of_type(soup: BeautifulSoup, *types: str) -> dict | None:
    return next((d for d in _jsonld(soup) if d.get("@type") in types), None)


# ---------------------------------------------------------------------------
# URLs and classification
# ---------------------------------------------------------------------------


def path_of(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


def is_product_url(url: str) -> bool:
    return bool(re.fullmatch(r"/product/[^/]+", path_of(url)))


def is_category_url(url: str) -> bool:
    p = path_of(url)
    return p == "/catalog" or bool(re.fullmatch(r"/catalog(/[a-z0-9-]+)+", p))


def type_signals(snapshot: PageSnapshot) -> dict[PageType, bool]:
    """DOM signals each page type requires. The classifier's model fallback is accepted only if
    the signals for the type it proposes are present (the model cannot invent a page type)."""
    soup = soup_for(snapshot)
    title = _text(soup.title) or ""
    body_head = (soup.body.get_text(" ", strip=True)[:3000] if soup.body else "") + " " + title
    has_product = _ld_of_type(soup, "ProductGroup", "Product") is not None or (
        soup.select_one("#product_addtocart_form") is not None
    )
    has_hits = soup.select_one(".ais-Hits, .ais-Hits-list") is not None
    has_category = soup.select_one(".category-cms") is not None
    return {
        PageType.PRODUCT: has_product,
        PageType.LISTING: has_hits,
        PageType.CATEGORY: has_category or has_hits,
        PageType.BLOCKED: not (has_product or has_hits) and bool(_BLOCK_TEXT.search(body_head)),
        PageType.OTHER: True,
    }


def classify_signals(snapshot: PageSnapshot) -> tuple[PageType, str]:
    if snapshot.status in (401, 403, 429):
        return PageType.BLOCKED, f"http {snapshot.status}"
    sig = type_signals(snapshot)
    soup = soup_for(snapshot)
    url = snapshot.final_url
    if sig[PageType.BLOCKED]:
        return PageType.BLOCKED, "block/captcha text without catalog signals"
    if is_product_url(url):
        if sig[PageType.PRODUCT]:
            return PageType.PRODUCT, "product url + ProductGroup/add-to-cart form"
        return PageType.UNKNOWN, "product url without product DOM signals"
    if is_category_url(url):
        if path_of(url) == "/catalog":
            return PageType.CATEGORY, "catalog index"
        if sig[PageType.LISTING]:
            return PageType.LISTING, "category url + Algolia hits container"
        if soup.select_one(".category-cms, nav.breadcrumbs"):
            return PageType.CATEGORY, "category url + category content"
        return PageType.UNKNOWN, "category url without listing signals"
    if sig[PageType.PRODUCT] or sig[PageType.LISTING]:
        return PageType.UNKNOWN, "catalog DOM signals at an unrecognised URL"
    return PageType.OTHER, "url outside catalog patterns"


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def breadcrumb(snapshot: PageSnapshot) -> list[str]:
    """Breadcrumb names without 'Home'."""
    soup = soup_for(snapshot)
    nav = soup.select_one("nav.breadcrumbs")
    names: list[str] = []
    if nav is not None:
        for li in nav.select("li"):
            el = li.select_one("a, span[aria-current]")
            t = _text(el)
            if t:
                names.append(t)
    else:
        ld = _ld_of_type(soup, "BreadcrumbList")
        if ld:
            names = [clean_text(i.get("name")) or "" for i in ld.get("itemListElement", [])]
    return [n for n in names if n and n.lower() != "home"]


def extract_links(snapshot: PageSnapshot, page_type: PageType) -> list[LinkCandidate]:
    soup = soup_for(snapshot)
    base = snapshot.final_url
    links: list[LinkCandidate] = []
    if page_type == PageType.CATEGORY and path_of(base) == "/catalog":
        main = soup.select_one("#maincontent") or soup.select_one("main") or soup
        for a in main.select("a[href]"):
            u = canonicalize_url(a["href"], base)
            if u and re.fullmatch(r"/catalog/[a-z0-9-]+", path_of(u)):
                links.append(LinkCandidate(url=u, kind="root", label=_text(a)))
        return links
    here = path_of(base)
    for a in soup.select("a[href]"):
        u = canonicalize_url(a["href"], base)
        if not u:
            continue
        p = path_of(u)
        if p.startswith(here + "/") and p.count("/") == here.count("/") + 1 and is_category_url(u):
            links.append(LinkCandidate(url=u, kind="subcategory", label=_text(a)))
    for a in soup.select(".ais-Hits-item a.result[href], .ais-Hits-item a[href*='/product/']"):
        u = canonicalize_url(a["href"], base)
        if u and is_product_url(u):
            links.append(LinkCandidate(url=u, kind="product", label=_text(a)))
    nxt = soup.select_one(".ais-Pagination-item--nextPage:not(.ais-Pagination-item--disabled) a[href]")
    if nxt is not None:
        u = canonicalize_url(nxt["href"], base, keep_query=True)
        if u:
            links.append(LinkCandidate(url=u, kind="next_page", label="Next page"))
    return links


def listing_stats(snapshot: PageSnapshot) -> str | None:
    return _text(soup_for(snapshot).select_one(".ais-Stats"))


# ---------------------------------------------------------------------------
# Sections (read-only, deterministic; used by parsers, model tools and the Validator)
# ---------------------------------------------------------------------------


def _price_tiers(row: Tag) -> list[tuple[str, str]]:
    cell = row.select_one('[data-attr="Price"]')
    if cell is None:
        return []
    tiers: list[tuple[str, str]] = []
    for ul in cell.select("ul"):
        lis = ul.find_all("li", recursive=False)
        if len(lis) != 2 or "qty-price-header" in " ".join(lis[0].get("class", [])):
            continue
        qty = _text(lis[0])
        price_el = lis[1].select_one(".price-wrapper .price") or lis[1].select_one(".price")
        price = _text(price_el)
        if qty and price:
            tiers.append((qty, price))
    return tiers


def build_sections(snapshot: PageSnapshot) -> list[Section]:
    return list(_build_sections_cached(snapshot.snapshot_id, snapshot.html))


@lru_cache(maxsize=64)
def _build_sections_cached(snapshot_id: str, html: str) -> tuple[Section, ...]:
    soup = _cached_soup(snapshot_id, html)
    sections: list[Section] = []

    # Header: name, brand as displayed.
    h1 = soup.select_one("h1")
    sections.append(Section(section_id="header", kind="header", text=_text(h1) or ""))

    bc = soup.select_one("nav.breadcrumbs")
    sections.append(Section(section_id="breadcrumb", kind="breadcrumb", text=_text(bc) or ""))

    desc = soup.select_one("section#description")
    if desc is not None:
        items = [t for li in desc.select("li") if (t := _text(li))]
        sections.append(
            Section(
                section_id="description",
                kind="description",
                text=_text(desc.select_one(".prose") or desc) or "",
                data={"list_items": items},
            )
        )

    ld = _ld_of_type(soup, "ProductGroup", "Product")
    if ld is not None:
        rows = []
        variants = ld.get("hasVariant") or []
        if not variants and ld.get("sku"):
            variants = [ld]
        for i, v in enumerate(variants):
            offers = v.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            cells = {
                "sku": str(v.get("sku") or ""),
                "name": clean_text(v.get("name")) or "",
                "price": "" if offers.get("price") in (None, "") else str(offers.get("price")),
                "currency": str(offers.get("priceCurrency") or ""),
                "availability": str(offers.get("availability") or ""),
            }
            rows.append(Row(row_id=f"ld-{i + 1}", cells=cells))
        data = {k: ld.get(k) for k in ("name", "description", "sku", "productGroupID", "category", "url") if k in ld}
        brand = ld.get("brand")
        data["brand"] = brand.get("name") if isinstance(brand, dict) else brand
        img = ld.get("image")
        data["image"] = img if isinstance(img, list) else ([img] if img else [])
        sections.append(Section(section_id="structured-data", kind="jsonld", text="", rows=rows, data=data))

    grouped = soup.select_one("#pdp-grouped")
    if grouped is not None:
        rows = []
        for container in grouped.select(".group-container") or [grouped]:
            heading_el = container.find(["span", "strong", "h3", "h4"], recursive=False)
            heading = _text(heading_el) if heading_el is not None else None
            for r in container.select(".table-grid-pdp"):
                sku_el = r.select_one(".product-item-sku")
                if sku_el is None:
                    continue  # header row
                tiers = _price_tiers(r)
                cells = {
                    "item_no": _text(sku_el) or "",
                    "product_name": _text(r.select_one('[data-attr="Product Name"]')) or "",
                    "mfr_no": _text(r.select_one('[data-attr="Mfr #"]')) or "",
                    # Only the label itself; the cell also holds SDS links and a "Details" tooltip.
                    "description": _text(r.select_one('[data-attr="Description"] .product-item-name'))
                    or _text(r.select_one('[data-attr="Description"]'))
                    or "",
                    "stock": _text(r.select_one(".stock-status")) or "",
                    "price": tiers[0][1] if tiers else "",
                    "price_tiers": "; ".join(f"{q}={p}" for q, p in tiers),
                    "group": heading or "",
                }
                name_el = r.select_one('[data-attr="Product Name"] .product-item-name')
                if name_el is not None:
                    cells["product_name"] = _text(name_el) or cells["product_name"]
                rows.append(Row(row_id=f"row-{len(rows) + 1}", cells=cells))
        text = "" if rows else (_text(grouped) or "")[:500]
        sections.append(Section(section_id="variant-table", kind="variant_table", text=text, rows=rows))

    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    hits = soup.select(".ais-Hits-item")
    if hits:
        sections.append(
            Section(
                section_id="listing-hits",
                kind="listing",
                text=_text(soup.select_one(".ais-Stats")) or "",
                rows=[
                    Row(row_id=f"hit-{i + 1}", cells={"name": _text(h.select_one("[itemprop=name]")) or ""})
                    for i, h in enumerate(hits[:24])
                ],
            )
        )
    sections.append(
        Section(
            section_id="page-text",
            kind="page_text",
            title=_text(soup.title),
            text=clean_text(body_text)[:1500] if body_text else "",
        )
    )
    if _LOGIN_PRICE.search(body_text):
        sections.append(Section(section_id="price-notice", kind="notice", text="login required for price"))
    return tuple(sections)


def section_by_id(snapshot: PageSnapshot, section_id: str) -> Section | None:
    return next((s for s in build_sections(snapshot) if s.section_id == section_id), None)


# Which cell of a row holds the SKU / price, per section kind (used by the Validator).
SKU_CELL = {"variant-table": "item_no", "structured-data": "sku"}
PRICE_CELL = {"variant-table": "price", "structured-data": "price"}
AVAILABILITY_CELL = {"variant-table": "stock", "structured-data": "availability"}
GROUP_CELL = {"variant-table": "group"}


# ---------------------------------------------------------------------------
# Deterministic product parsing
# ---------------------------------------------------------------------------


def product_key(snapshot: PageSnapshot) -> str:
    sd = section_by_id(snapshot, "structured-data")
    if sd and sd.data and sd.data.get("productGroupID"):
        return str(sd.data["productGroupID"])
    return path_of(snapshot.final_url)


def _kv_attributes(items: list[str]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for item in items:
        m = re.fullmatch(r"([A-Za-z][A-Za-z /&()-]{1,30}):\s*(.+)", item)
        if m:
            attrs.setdefault(m.group(1).strip(), m.group(2).strip())
    return attrs


def parse_product(snapshot: PageSnapshot) -> ProductCandidate:
    secs = {s.section_id: s for s in build_sections(snapshot)}
    sd = secs.get("structured-data")
    table = secs.get("variant-table")
    desc = secs.get("description")
    data = (sd.data or {}) if sd else {}

    name = clean_text(data.get("name")) or (secs["header"].text or None)
    description = clean_text(desc.text) if desc and desc.text else clean_text(data.get("description"))
    attributes = _kv_attributes((desc.data or {}).get("list_items", [])) if desc else {}

    variants: list[VariantObservation] = []
    seen: set[str] = set()
    ld_rows = {r.cells.get("sku"): r for r in (sd.rows if sd else []) if r.cells.get("sku")}

    if table and table.rows:
        for r in table.rows:
            sku = r.cells.get("item_no", "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)
            label = r.cells.get("description") or None
            pack_label, _, _ = parse_pack(label or r.cells.get("product_name"))
            attrs = {"Group": r.cells["group"]} if r.cells.get("group") else {}
            price_raw = r.cells.get("price") or None
            variants.append(
                VariantObservation(
                    sku=sku,
                    row_id=r.row_id,
                    section_id="variant-table",
                    sku_raw=r.cells["item_no"],
                    variant_name=label,
                    attributes=attrs,
                    pack_label=pack_label,
                    price_raw=price_raw,
                    price_section_id="variant-table" if price_raw else None,
                    price_row_id=r.row_id if price_raw else None,
                    availability_raw=r.cells.get("stock") or None,
                )
            )
    elif sd and sd.rows:
        # Variant table not rendered: structured data is the observed source.
        for r in sd.rows:
            sku = r.cells.get("sku", "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)
            pack_label, _, _ = parse_pack(r.cells.get("name"))
            price_raw = r.cells.get("price") or None
            variants.append(
                VariantObservation(
                    sku=sku,
                    row_id=r.row_id,
                    section_id="structured-data",
                    sku_raw=r.cells["sku"],
                    variant_name=r.cells.get("name") or None,
                    pack_label=pack_label,
                    price_raw=price_raw,
                    price_section_id="structured-data" if price_raw else None,
                    price_row_id=r.row_id if price_raw else None,
                    availability_raw=r.cells.get("availability") or None,
                )
            )

    expected = list(
        dict.fromkeys([*(r.cells.get("item_no", "") for r in (table.rows if table else [])), *ld_rows.keys()])
    )
    brand = clean_text(data.get("brand")) if data.get("brand") else None
    return ProductCandidate(
        snapshot_id=snapshot.snapshot_id,
        url=canonicalize_url(snapshot.final_url) or snapshot.final_url,
        product_key=product_key(snapshot),
        name=name,
        brand=brand,
        manufacturer=None,  # Safco shows one manufacturer label, identical to the brand.
        description=description,
        breadcrumb=breadcrumb(snapshot),
        image_urls=[u for u in data.get("image", []) if isinstance(u, str) and u.startswith("http")],
        attributes=attributes,
        alternatives=[],
        variants=variants,
        expected_skus=[s for s in expected if s],
        login_required_price="price-notice" in secs,
    )
