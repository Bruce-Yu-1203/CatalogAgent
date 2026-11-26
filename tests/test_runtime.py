"""Runtime: 429/Retry-After, 403 and CAPTCHA (no bypass), page budget, resume with and without extensions."""

from __future__ import annotations

import random
import time

from conftest import URLS, ScriptedFetcher, make_config, throttled

from catalog_agent.agents.recovery import RecoveryAgent
from catalog_agent.contracts import ErrorCode, FetchError
from catalog_agent.runtime.runner import Extensions, execute
from catalog_agent.storage.repository import Repository


def factory(fetcher):
    async def make(_cfg):
        return fetcher

    return make


async def crawl(cfg, run_id, fetcher):
    return await execute(
        database=cfg.storage.database,
        cfg=cfg,
        mode="deterministic",
        run_id=run_id,
        fetcher_factory=factory(fetcher),
        quiet_logs=True,
    )


async def resume(cfg, run_id, fetcher, ext=None):
    return await execute(
        database=cfg.storage.database,
        run_id=run_id,
        resume=True,
        extensions=ext,
        fetcher_factory=factory(fetcher),
        quiet_logs=True,
    )


async def test_backoff_is_bounded_exponential_with_jitter():
    r = RecoveryAgent(max_attempts=3, backoff_base=2, backoff_max=10, rng=random.Random(1))
    delays = [r.backoff(n) for n in (1, 2, 3, 6)]
    assert 2 <= delays[0] <= 2.5 and 4 <= delays[1] <= 5 and 8 <= delays[2] <= 10 and 10 <= delays[3] <= 12.5
    d = (await r.run((ErrorCode.TIMEOUT, "t", 3, None))).output
    assert d.action == "fail"  # attempts exhausted


async def test_429_honors_retry_after_then_succeeds(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1)
    fetcher = ScriptedFetcher({URLS["listing_gloves"]: [throttled(0.6)]})
    t0 = time.monotonic()
    res = await crawl(cfg, "run_429", fetcher)
    repo = await Repository.open(cfg.storage.database)
    gl = next(t for t in await repo.tasks("run_429") if t["url"] == URLS["listing_gloves"])
    assert gl["status"] == "COMPLETED" and gl["attempts"] == 2 and gl["error_code"] is None
    assert fetcher.fetched.count(URLS["listing_gloves"]) == 1  # the 429 attempt never reached the page
    assert time.monotonic() - t0 >= 0.6  # waited at least Retry-After
    assert res["status"] in ("BUDGET_EXHAUSTED", "COMPLETED_WITH_ERRORS")
    await repo.close()


async def test_403_pauses_the_scope_without_bypass(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1)
    denied = FetchError(ErrorCode.ACCESS_DENIED, "http 403", 403)
    fetcher = ScriptedFetcher({URLS["listing_gloves"]: [denied]})
    res = await crawl(cfg, "run_403", fetcher)
    assert res["status"] == "PAUSED"
    repo = await Repository.open(cfg.storage.database)
    run = await repo.get_run("run_403")
    assert run["termination_reason"] == "ACCESS_RESTRICTED"
    gl = next(t for t in await repo.tasks("run_403") if t["url"] == URLS["listing_gloves"])
    assert gl["status"] == "RETRY_WAIT" and gl["error_code"] == "ACCESS_DENIED"  # kept resumable, not retried now
    assert fetcher.calls == 2  # discovery + the denied page; nothing after the restriction
    await repo.close()


async def test_captcha_page_is_treated_as_restriction(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1)
    fetcher = ScriptedFetcher()
    captcha = "<html><body><h1>Please complete the CAPTCHA</h1><p>Are you a robot?</p></body></html>"

    async def fake_fetch(url, kind, require_ready=True, _orig=fetcher.fetch):
        if url == URLS["listing_gloves"]:
            from catalog_agent.runtime.fetcher import make_snapshot

            return make_snapshot(url, url, 200, captcha)  # SYNTHETIC block page
        return await _orig(url, kind, require_ready)

    fetcher.fetch = fake_fetch
    res = await crawl(cfg, "run_captcha", fetcher)
    assert res["status"] == "PAUSED"


async def test_page_budget_and_resume_semantics(tmp_path):
    cfg = make_config(tmp_path, max_pages=4, max_products_per_category=5, concurrency=1)
    res = await crawl(cfg, "run_budget", ScriptedFetcher())
    assert res["status"] == "BUDGET_EXHAUSTED"
    repo = await Repository.open(cfg.storage.database)
    run = await repo.get_run("run_budget")
    assert run["pages_used"] == 4 and run["termination_reason"] == "MAX_PAGES"
    pending = (await repo.task_counts("run_budget")).get("PENDING", 0)
    assert pending > 0  # unfinished work is kept
    await repo.close()

    # Plain resume exits immediately with the same status and fetches nothing.
    f2 = ScriptedFetcher()
    res2 = await resume(cfg, "run_budget", f2)
    assert res2["status"] == "BUDGET_EXHAUSTED" and f2.calls == 0

    # Edited YAML does not change an existing run: resume uses the stored configuration.
    cfg.crawler.max_pages = 999

    f3 = ScriptedFetcher()
    res3 = await resume(cfg, "run_budget", f3, Extensions(add_pages=3))
    repo = await Repository.open(cfg.storage.database)
    run = await repo.get_run("run_budget")
    assert f3.calls == 3 and run["pages_used"] == 7 and run["max_pages"] == 7
    assert res3["status"] == "BUDGET_EXHAUSTED"
    assert '"add_pages": 3' in run["extensions_json"]
    await repo.close()


async def test_time_limit_pauses_and_plain_resume_continues(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1)
    cfg.crawler.max_invocation_seconds = 0.001
    res = await crawl(cfg, "run_time", ScriptedFetcher())
    assert res["status"] == "PAUSED"
    repo = await Repository.open(cfg.storage.database)
    assert (await repo.get_run("run_time"))["termination_reason"] == "TIME_LIMIT"
    await repo.update_run(
        "run_time",
        config_json=(await repo.get_run("run_time"))["config_json"].replace(
            '"max_invocation_seconds": 0.001', '"max_invocation_seconds": 120.0'
        ),
    )
    await repo.close()
    f = ScriptedFetcher()
    res2 = await resume(cfg, "run_time", f)  # no extension needed after a time limit
    assert f.calls > 0 and res2["status"] != "PAUSED"


async def test_transient_errors_retry_with_bounded_attempts(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1, max_attempts=2)
    t = FetchError(ErrorCode.TIMEOUT, "timeout")
    fetcher = ScriptedFetcher({URLS["listing_sutures"]: [t, t, t]})
    await crawl(cfg, "run_retry", fetcher)
    repo = await Repository.open(cfg.storage.database)
    task = next(x for x in await repo.tasks("run_retry") if x["url"] == URLS["listing_sutures"])
    assert task["status"] == "FAILED" and task["attempts"] == 2 and task["error_code"] == "TIMEOUT"
    await repo.close()
