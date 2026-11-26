"""Shared offline test helpers. No test touches the live site or a paid model API."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest

from catalog_agent.adapters import safco
from catalog_agent.config import AppConfig, load_config
from catalog_agent.contracts import ErrorCode, FetchError, PageSnapshot
from catalog_agent.extraction.llm import ModelReply, ToolCall
from catalog_agent.runtime.fetcher import ReplayFetcher, make_snapshot

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "tests" / "fixtures" / "live"
BASE = "https://www.safcodental.com"
URLS = {
    "catalog": f"{BASE}/catalog",
    "listing_gloves": f"{BASE}/catalog/gloves",
    "listing_sutures": f"{BASE}/catalog/sutures-surgical-products",
    "listing_sutures_sutures": f"{BASE}/catalog/sutures-surgical-products/sutures",
    "product_surpass_gloves": f"{BASE}/product/safco-nitrilex-surpass-trade",
    "product_surestitch_sutures": f"{BASE}/product/safco-surestitch-trade-sutures",
    "product_trimmer_incomplete_render": f"{BASE}/product/soft-tissue-trimmer",
}


def fixture_html(name: str) -> str:
    with gzip.open(LIVE / f"{name}.html.gz", "rt", encoding="utf-8") as f:
        return f.read()


def snapshot(name: str, html: str | None = None, url: str | None = None) -> PageSnapshot:
    """Snapshot of a real fixture; pass `html` to build a labeled SYNTHETIC variant of it."""
    u = url or URLS[name]
    return make_snapshot(u, u, 200, html if html is not None else fixture_html(name))


def make_config(tmp_path: Path, **crawler: Any) -> AppConfig:
    cfg = load_config(ROOT / "configs" / "safco.yaml")
    cfg.storage.database = str(tmp_path / "catalog.db")
    cfg.storage.snapshot_dir = str(tmp_path / "snapshots")
    cfg.storage.log_dir = str(tmp_path / "logs")
    cfg.crawler.request_interval_seconds = 1.0
    cfg.crawler.backoff_base_seconds = 0.0
    for k, v in crawler.items():
        setattr(cfg.crawler, k, v)
    return cfg


class ScriptedFetcher(ReplayFetcher):
    """Replay fetcher with injectable failures: {url: [FetchError | 'ok' | BaseException, ...]}."""

    def __init__(self, failures: dict[str, list[Any]] | None = None, crash_after: int | None = None):
        super().__init__(
            LIVE,
            safco.READY_SELECTORS,
            ["www.safcodental.com", "safcodental.com"],
            fallback_selectors=safco.READY_FALLBACK_SELECTORS,
        )
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.crash_after = crash_after
        self.calls = 0

    async def fetch(self, url: str, kind: str, require_ready: bool = True) -> PageSnapshot:
        self.calls += 1
        if self.crash_after is not None and self.calls > self.crash_after:
            raise SimulatedCrash(url)
        queue = self.failures.get(url)
        if queue:
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
        return await super().fetch(url, kind, require_ready)


class SimulatedCrash(BaseException):
    """Stands in for a process kill: not an Exception, so nothing in the runtime handles it."""


def throttled(retry_after: float) -> FetchError:
    return FetchError(ErrorCode.THROTTLED, "429", 429, retry_after)


class FakeModel:
    """Scripted model: each invoke() pops the next reply. Records what it was sent."""

    model_id = "fake:scripted"

    def __init__(self, replies: list[ModelReply | Exception]):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.replies:
            return ModelReply(
                tool_calls=[ToolCall(id=f"t{len(self.calls)}", name="submit_patch", args={"variants": []})]
            )
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ExplodingModel:
    model_id = "fake:must-not-be-called"

    async def invoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        raise AssertionError("model must not be called")


def tool(name: str, i: int = 0, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{name}_{i}", name=name, args=args)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)
