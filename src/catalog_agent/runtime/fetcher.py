"""Fetcher: Playwright rendering behind host/scope rules, robots.txt, pacing and typed errors."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from catalog_agent.contracts import ErrorCode, FetchError, PageSnapshot
from catalog_agent.runtime.robots import RobotsPolicy


class Fetcher(Protocol):
    async def fetch(self, url: str, kind: str, require_ready: bool = True) -> PageSnapshot: ...

    async def close(self) -> None: ...


class SnapshotStore:
    """Immutable snapshots: <dir>/<snapshot_id>.html.gz plus a small JSON metadata file."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, snap: PageSnapshot) -> PageSnapshot:
        rel = f"{snap.snapshot_id}.html.gz"
        path = self.root / rel
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                f.write(snap.html)
            tmp.rename(path)
            meta = snap.model_dump(exclude={"html"})
            meta["path"] = rel
            (self.root / f"{snap.snapshot_id}.json").write_text(json.dumps(meta, indent=1))
        return snap.model_copy(update={"path": rel})

    def load(self, rel: str) -> PageSnapshot:
        meta = json.loads((self.root / rel.replace(".html.gz", ".json")).read_text())
        with gzip.open(self.root / rel, "rt", encoding="utf-8") as f:
            meta["html"] = f.read()
        return PageSnapshot.model_validate(meta)


def make_snapshot(url: str, final_url: str, status: int, html: str) -> PageSnapshot:
    content_hash = hashlib.sha256(html.encode()).hexdigest()
    return PageSnapshot(
        snapshot_id=PageSnapshot.make_id(final_url, content_hash),
        url=url,
        final_url=final_url,
        status=status,
        fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        content_hash=content_hash,
        html=html,
    )


class HostPacer:
    """Per-host minimum interval between requests; widened when the host throttles."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, host: str) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next_at.get(host, 0.0))
            self._next_at[host] = at + self.interval + random.uniform(0, 0.25 * self.interval)
        delay = at - now
        if delay > 0:
            await asyncio.sleep(delay)

    def slow_down(self, host: str, seconds: float) -> None:
        self.interval = min(self.interval * 2, 30.0)
        self._next_at[host] = max(self._next_at.get(host, 0.0), time.monotonic() + seconds)


class PlaywrightFetcher:
    def __init__(
        self,
        allowed_hosts: list[str],
        user_agent: str,
        interval: float,
        timeout_s: float,
        readiness_timeout_s: float,
        ready_selectors: dict[str, str],
        blocked_resource_types: tuple[str, ...] = (),
        blocked_url_parts: tuple[str, ...] = (),
        robots_url: str | None = None,
        fallback_selectors: dict[str, str] | None = None,
    ):
        self.fallback_selectors = fallback_selectors or {}
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.user_agent = user_agent
        self.pacer = HostPacer(interval)
        self.timeout_ms = int(timeout_s * 1000)
        self.ready_ms = int(readiness_timeout_s * 1000)
        self.ready_selectors = ready_selectors
        self.blocked_types = blocked_resource_types
        self.blocked_parts = blocked_url_parts
        self.robots_url = robots_url
        self.robots: RobotsPolicy | None = None
        self._pw = None
        self._browser = None
        self._context = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch()
        self._context = await self._browser.new_context(
            user_agent=self.user_agent, viewport={"width": 1400, "height": 1000}
        )
        await self._context.route("**/*", self._route)
        if self.robots_url:
            host = urlsplit(self.robots_url).hostname or ""
            await self.pacer.wait(host)
            resp = await self._context.request.get(self.robots_url, timeout=self.timeout_ms)
            if resp.status >= 400:
                raise FetchError(ErrorCode.ACCESS_DENIED, f"robots.txt returned {resp.status}", resp.status)
            self.robots = RobotsPolicy(await resp.text(), self.user_agent)

    async def _route(self, route) -> None:  # noqa: ANN001
        req = route.request
        if req.resource_type in self.blocked_types or any(p in req.url for p in self.blocked_parts):
            await route.abort()
        else:
            await route.continue_()

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in self.allowed_hosts:
            raise FetchError(ErrorCode.OFF_SCOPE_REDIRECT, f"host not allowed: {url}")
        if self.robots is not None and not self.robots.allowed(url):
            raise FetchError(ErrorCode.ROBOTS_DISALLOWED, f"robots.txt disallows {url}")

    async def fetch(self, url: str, kind: str, require_ready: bool = True) -> PageSnapshot:
        assert self._context is not None, "call start() first"
        self.check_url(url)
        host = urlsplit(url).hostname or ""
        await self.pacer.wait(host)
        page = await self._context.new_page()
        try:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            except Exception as e:  # playwright raises generic errors
                msg = str(e)
                code = ErrorCode.TIMEOUT if "Timeout" in msg else ErrorCode.CONNECTION
                raise FetchError(code, msg[:300]) from e
            status = resp.status if resp else 0
            final_url = page.url
            try:
                self.check_url(final_url)
            except FetchError as e:
                raise FetchError(ErrorCode.OFF_SCOPE_REDIRECT, f"redirected to {final_url}") from e
            if status == 429:
                ra = resp.headers.get("retry-after") if resp else None
                retry_after = float(ra) if ra and ra.isdigit() else None
                self.pacer.slow_down(host, retry_after or 30.0)
                raise FetchError(ErrorCode.THROTTLED, "429 Too Many Requests", status, retry_after)
            if status in (401, 403):
                raise FetchError(ErrorCode.ACCESS_DENIED, f"http {status}", status)
            if status == 404 or status == 410:
                raise FetchError(ErrorCode.NOT_FOUND, f"http {status}", status)
            if status >= 500:
                raise FetchError(ErrorCode.HTTP_5XX, f"http {status}", status)
            selector = self.ready_selectors.get(kind)
            if selector:
                try:
                    await page.wait_for_selector(selector, state="attached", timeout=self.ready_ms)
                except Exception as e:
                    fallback = self.fallback_selectors.get(kind)
                    usable = fallback is not None and await page.query_selector(fallback) is not None
                    if require_ready and not usable:
                        raise FetchError(ErrorCode.INCOMPLETE_RENDER, f"readiness signal missing: {selector}") from e
            html = await page.content()
            return make_snapshot(url, final_url, status, html)
        finally:
            await page.close()

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


class ReplayFetcher:
    """Offline fetcher over saved snapshots: `manifest.json` maps URL -> gzipped HTML file.

    Used for fixture replay (tests, smoke script). Unknown URLs behave like HTTP 404. Readiness
    signals are checked on the saved DOM, so a snapshot captured before rendering finished behaves
    like an incomplete render until the final attempt.
    """

    def __init__(
        self,
        fixtures_dir: str | Path,
        ready_selectors: dict[str, str],
        allowed_hosts: list[str],
        robots_file: str | None = "robots.txt",
        fallback_selectors: dict[str, str] | None = None,
    ):
        self.fallback_selectors = fallback_selectors or {}
        self.dir = Path(fixtures_dir)
        self.manifest: dict[str, str] = json.loads((self.dir / "manifest.json").read_text())
        self.ready_selectors = ready_selectors
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        robots_path = self.dir / robots_file if robots_file else None
        self.robots = RobotsPolicy(robots_path.read_text()) if robots_path and robots_path.exists() else None
        self.fetched: list[str] = []

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in self.allowed_hosts:
            raise FetchError(ErrorCode.OFF_SCOPE_REDIRECT, f"host not allowed: {url}")
        if self.robots is not None and not self.robots.allowed(url):
            raise FetchError(ErrorCode.ROBOTS_DISALLOWED, f"robots.txt disallows {url}")

    async def fetch(self, url: str, kind: str, require_ready: bool = True) -> PageSnapshot:
        self.check_url(url)
        self.fetched.append(url)
        name = self.manifest.get(url)
        if name is None:
            raise FetchError(ErrorCode.NOT_FOUND, f"no fixture for {url}", 404)
        with gzip.open(self.dir / name, "rt", encoding="utf-8") as f:
            html = f.read()
        selector = self.ready_selectors.get(kind)
        if selector and require_ready:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            fallback = self.fallback_selectors.get(kind)
            if soup.select_one(selector) is None and (fallback is None or soup.select_one(fallback) is None):
                raise FetchError(ErrorCode.INCOMPLETE_RENDER, f"readiness signal missing: {selector}")
        return make_snapshot(url, url, 200, html)

    async def close(self) -> None:
        return None
