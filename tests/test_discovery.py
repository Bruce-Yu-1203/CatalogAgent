"""Discovery: root selection, in-scope traversal, dedup, pagination stop, readiness on a delayed container."""

from __future__ import annotations

import asyncio

import pytest
from conftest import BASE, LIVE, URLS, fixture_html, snapshot

from catalog_agent.adapters import safco
from catalog_agent.agents.navigator import NavigatorAgent, NavigatorInput, select_roots
from catalog_agent.contracts import CrawlTask, ErrorCode, FetchError, PageType, TaskType
from catalog_agent.runtime.fetcher import PlaywrightFetcher
from catalog_agent.runtime.robots import RobotsPolicy

ROOTS = [f"{BASE}/catalog/gloves", f"{BASE}/catalog/sutures-surgical-products"]
ROBOTS = RobotsPolicy((LIVE / "robots.txt").read_text())


def robots_check(url: str) -> str | None:
    if not url.startswith(BASE + "/"):
        return "off_scope"
    return None if ROBOTS.allowed(url) else "robots_disallowed"


def nav_input(
    name: str,
    task_type: TaskType,
    page_type: PageType,
    scope: str = "selected",
    check=robots_check,
    root: str | None = None,
) -> NavigatorInput:
    url = URLS[name]
    task = CrawlTask(
        task_id=1,
        run_id="r",
        task_key=f"{task_type.value}:{url}",
        task_type=task_type,
        url=url,
        root=root,
        depth=0 if task_type == TaskType.DISCOVERY else 1,
    )
    return NavigatorInput(
        task=task,
        snapshot=snapshot(name),
        page_type=page_type,
        scope=scope,
        configured_roots=ROOTS,
        allowed_hosts=["www.safcodental.com"],
        url_check=check,
    )


async def test_selected_seeds_exactly_the_two_configured_roots():
    res = await NavigatorAgent(safco).run(nav_input("catalog", TaskType.DISCOVERY, PageType.CATEGORY))
    urls = [c.url for c in res.output.children]
    assert urls == ROOTS
    assert all(c.task_type == TaskType.LISTING and c.root == c.url for c in res.output.children)
    assert res.output.degraded_discovery is False


async def test_all_seeds_every_permitted_root_on_the_saved_navigation_snapshot():
    res = await NavigatorAgent(safco).run(nav_input("catalog", TaskType.DISCOVERY, PageType.CATEGORY, scope="all"))
    urls = [c.url for c in res.output.children]
    assert len(urls) == len(set(urls)) == 20  # 20 category roots on /catalog (observed 2026-11-26)
    assert set(ROOTS) <= set(urls)
    assert all(safco.path_of(u).count("/") == 2 for u in urls)


def test_select_roots_reports_degraded_discovery_when_a_root_is_missing():
    roots, degraded = select_roots([ROOTS[0]], "selected", ROOTS)
    assert roots == ROOTS and degraded is True


async def test_listing_schedules_in_scope_children_once_with_hierarchy():
    res = await NavigatorAgent(safco).run(
        nav_input("listing_gloves", TaskType.LISTING, PageType.LISTING, root=URLS["listing_gloves"])
    )
    out = res.output
    products = [c for c in out.children if c.task_type == TaskType.PRODUCT]
    subcats = [c for c in out.children if c.task_type == TaskType.LISTING]
    assert len(products) == 12 and len({c.url for c in products}) == 12  # duplicate links scheduled once
    assert all(c.lineage == [["Dental Supplies", "Dental Exam Gloves"]] for c in products)
    assert len(subcats) == 6
    assert all(c.url.startswith(f"{BASE}/catalog/gloves/") for c in subcats)  # other roots in the menu ignored
    assert all(c.root == URLS["listing_gloves"] for c in out.children)
    # Pagination is detected but robots.txt disallows ?page=N, so it is recorded as the stop reason.
    assert out.pagination_stop == "next_page_robots_disallowed"
    assert (f"{BASE}/catalog/gloves?page=2", "robots_disallowed") in out.rejected


async def test_off_scope_links_are_rejected():
    def no_products(url: str) -> str | None:
        return "off_scope" if "/product/" in url else robots_check(url)

    res = await NavigatorAgent(safco).run(
        nav_input("listing_gloves", TaskType.LISTING, PageType.LISTING, check=no_products, root=URLS["listing_gloves"])
    )
    assert not [c for c in res.output.children if c.task_type == TaskType.PRODUCT]
    assert sum(1 for _, why in res.output.rejected if why == "off_scope") == 12


async def test_permitted_pagination_is_followed():
    res = await NavigatorAgent(safco).run(
        nav_input(
            "listing_gloves", TaskType.LISTING, PageType.LISTING, check=lambda u: None, root=URLS["listing_gloves"]
        )
    )
    assert f"{BASE}/catalog/gloves?page=2" in [c.url for c in res.output.children]
    assert res.output.pagination_stop == "next_page_enqueued"


async def test_last_page_stop_reason():
    res = await NavigatorAgent(safco).run(
        nav_input("listing_sutures_sutures", TaskType.LISTING, PageType.LISTING, root=URLS["listing_sutures"])
    )
    assert res.output.pagination_stop == "last_page"
    assert len([c for c in res.output.children if c.task_type == TaskType.PRODUCT]) == 5


def test_robots_wildcards_match_live_rules():
    assert ROBOTS.allowed(f"{BASE}/catalog/gloves")
    assert ROBOTS.allowed(f"{BASE}/product/safco-nitrilex-surpass-trade")
    assert not ROBOTS.allowed(f"{BASE}/catalog/gloves?page=2")
    assert not ROBOTS.allowed(f"{BASE}/catalog/gloves?p=2")
    assert not ROBOTS.allowed(f"{BASE}/catalog/product/view/id/5")
    assert not ROBOTS.allowed(f"{BASE}/index.php")  # Disallow: /*.php$


async def _delayed_fetch(selector_delay_ms: int, readiness_s: float) -> tuple[object, list[str]]:
    """Serve the real gloves listing from a local route, with its product grid injected after a delay."""
    html = fixture_html("listing_gloves")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    hits = soup.select_one(".ais-Hits")
    grid = str(hits)
    hits.decompose()
    for s in soup.select("script"):
        s.decompose()
    shell = str(soup).replace(
        "</body>",
        f"<script>setTimeout(()=>{{document.body.insertAdjacentHTML('beforeend', {grid!r})}}, {selector_delay_ms});"
        "</script></body>",
    )
    f = PlaywrightFetcher(["www.safcodental.com"], "test", 1.0, 10, readiness_s, safco.READY_SELECTORS)
    await f.start()
    seen: list[str] = []

    async def fulfil(route):  # offline: every request is answered locally or aborted
        seen.append(route.request.url)
        if route.request.url.rstrip("/") == URLS["listing_gloves"]:
            await route.fulfill(status=200, content_type="text/html", body=shell)
        else:
            await route.abort()

    await f._context.unroute("**/*")
    await f._context.route("**/*", fulfil)
    try:
        try:
            snap = await f.fetch(URLS["listing_gloves"], "listing", require_ready=True)
        except FetchError as e:
            snap = e
    finally:
        await f.close()
    return snap, seen


async def test_fetcher_waits_for_delayed_listing_container_and_reports_incomplete_render():
    snap, _ = await _delayed_fetch(800, 8)
    assert not isinstance(snap, FetchError)
    links = safco.extract_links(snap, PageType.LISTING)
    assert len([x for x in links if x.kind == "product"]) == 12  # loaded listing, not the placeholder
    snap2, _ = await _delayed_fetch(5000, 1)
    assert isinstance(snap2, FetchError) and snap2.code == ErrorCode.INCOMPLETE_RENDER


async def test_fetcher_rejects_disallowed_and_off_host_urls_without_requesting():
    f = PlaywrightFetcher(["www.safcodental.com"], "test", 1.0, 10, 1, {})
    f.robots = ROBOTS
    with pytest.raises(FetchError) as e1:
        f.check_url(f"{BASE}/catalog/gloves?page=2")
    with pytest.raises(FetchError) as e2:
        f.check_url("https://evil.example.com/product/x")
    assert e1.value.code == ErrorCode.ROBOTS_DISALLOWED and e2.value.code == ErrorCode.OFF_SCOPE_REDIRECT
    await asyncio.sleep(0)
