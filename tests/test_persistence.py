"""Persistence: idempotency, atomic commits, crash recovery under the lock, single-process ownership, run keys."""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import URLS, ScriptedFetcher, SimulatedCrash, make_config, snapshot

from catalog_agent.adapters import safco
from catalog_agent.agents.validator import ValidatorAgent
from catalog_agent.contracts import ChildTask, TaskStatus, TaskType
from catalog_agent.runtime.lock import DatabaseLock, LockHeldError
from catalog_agent.runtime.runner import execute
from catalog_agent.storage.repository import InjectedFailure, Repository


def factory(fetcher):
    async def make(_cfg):
        return fetcher

    return make


async def run_replay(cfg, run_id, fetcher=None, **kw):
    return await execute(
        database=cfg.storage.database,
        cfg=cfg,
        mode="deterministic",
        run_id=run_id,
        fetcher_factory=factory(fetcher or ScriptedFetcher()),
        quiet_logs=True,
        **kw,
    )


def product_for(run_id: str):
    snap = snapshot("product_surpass_gloves")
    cand = safco.parse_product(snap)
    out, _ = ValidatorAgent(safco, "safco").validate(cand, snap, run_id, [])
    return out.product, snap


async def _claimed_product_task(repo: Repository, run_id: str):
    await repo.create_run(run_id, "deterministic", {}, 100, 10, 0)
    await repo.enqueue(
        run_id,
        [ChildTask(task_type=TaskType.PRODUCT, url=URLS["product_surpass_gloves"], root="r", lineage=[["A"]], depth=1)],
    )
    task, _ = await repo.claim_next(run_id, 0)
    return task


async def test_same_task_twice_keeps_unique_counts(tmp_path):
    repo = await Repository.open(tmp_path / "db.sqlite")
    task = await _claimed_product_task(repo, "run_a")
    child = ChildTask(
        task_type=TaskType.PRODUCT, url=URLS["product_surpass_gloves"], root="r", lineage=[["B"]], depth=1
    )
    assert await repo.enqueue("run_a", [child, child]) == 0  # known task: lineage merged, nothing inserted
    product, snap = product_for("run_a")
    meta = {"snapshot_id": snap.snapshot_id}
    await repo.complete_product(task, product, TaskStatus.COMPLETED, {}, meta, 0, 0, 0)
    first = await repo.count_unique("run_a")
    # At-least-once replay of the same task: re-run and commit the same effect again.
    await repo.conn.execute("UPDATE crawl_tasks SET status='RUNNING' WHERE task_id=?", (task.task_id,))
    await repo.complete_product(task, product, TaskStatus.COMPLETED, {}, meta, 0, 0, 0)
    assert await repo.count_unique("run_a") == first == (1, 5)
    tasks = await repo.tasks("run_a")
    assert len(tasks) == 1 and '["B"]' in tasks[0]["lineage_json"]
    await repo.close()


async def test_failure_before_commit_leaves_nothing_partially_committed(tmp_path):
    repo = await Repository.open(tmp_path / "db.sqlite")
    task = await _claimed_product_task(repo, "run_b")
    product, snap = product_for("run_b")
    repo.fail_before_commit = "complete_product"
    with pytest.raises(InjectedFailure):
        await repo.complete_product(task, product, TaskStatus.COMPLETED, {}, {"snapshot_id": snap.snapshot_id}, 0, 0, 0)
    repo.fail_before_commit = None
    assert await repo.count_unique("run_b") == (0, 0)
    assert (await repo.get_task(task.task_id)).status == TaskStatus.RUNNING  # rolled back, still owned
    requeued, failed = await repo.reconcile_running("run_b", max_attempts=3)
    assert (requeued, failed) == (1, 0)
    assert (await repo.get_task(task.task_id)).status == TaskStatus.PENDING
    await repo.close()


async def test_navigation_children_and_completion_commit_together(tmp_path):
    repo = await Repository.open(tmp_path / "db.sqlite")
    await repo.create_run("run_n", "deterministic", {}, 100, 10, 0)
    await repo.enqueue(
        "run_n", [ChildTask(task_type=TaskType.LISTING, url="https://x/a", root="https://x/a", lineage=[], depth=1)]
    )
    task, _ = await repo.claim_next("run_n", 0)
    kids = [
        ChildTask(task_type=TaskType.PRODUCT, url=f"https://x/p{i}", root="https://x/a", lineage=[], depth=2)
        for i in range(3)
    ]
    repo.fail_before_commit = "complete_navigation"
    with pytest.raises(InjectedFailure):
        await repo.complete_navigation(task, kids, {}, {}, TaskStatus.COMPLETED)
    assert len(await repo.tasks("run_n")) == 1  # no orphan children, parent not completed
    repo.fail_before_commit = None
    assert await repo.complete_navigation(task, kids, {}, {}, TaskStatus.COMPLETED) == 3
    await repo.close()


async def test_interrupt_and_resume_recovers_running_tasks_only_after_lock(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1)
    with pytest.raises(SimulatedCrash):
        await run_replay(cfg, "run_crash", ScriptedFetcher(crash_after=3))
    repo = await Repository.open(cfg.storage.database)
    before = {t["task_id"]: t for t in await repo.tasks("run_crash")}
    running = [t for t in before.values() if t["status"] == "RUNNING"]
    completed = [t for t in before.values() if t["status"] == "COMPLETED"]
    assert len(running) == 1 and len(completed) == 3 and any(t["status"] == "PENDING" for t in before.values())
    await repo.close()

    # Another process owns the database: resume must fail fast and must not touch the RUNNING task.
    holder = DatabaseLock(cfg.storage.database).acquire()
    with pytest.raises(LockHeldError):
        await execute(
            database=cfg.storage.database,
            run_id="run_crash",
            resume=True,
            fetcher_factory=factory(ScriptedFetcher()),
            quiet_logs=True,
        )
    repo = await Repository.open(cfg.storage.database)
    assert (await repo.get_task(running[0]["task_id"])).status == TaskStatus.RUNNING
    await repo.close()
    holder.release()

    res = await execute(
        database=cfg.storage.database,
        run_id="run_crash",
        resume=True,
        fetcher_factory=factory(ScriptedFetcher()),
        quiet_logs=True,
    )
    repo = await Repository.open(cfg.storage.database)
    after = {t["task_id"]: t for t in await repo.tasks("run_crash")}
    for t in completed:  # completed tasks untouched on resume
        assert after[t["task_id"]]["updated_at"] == t["updated_at"] and after[t["task_id"]]["attempts"] == 1
    assert after[running[0]["task_id"]]["status"] != "RUNNING"
    assert after[running[0]["task_id"]]["attempts"] == 2  # interrupted attempt counted as consumed
    run = await repo.get_run("run_crash")
    assert run["pages_used"] == sum(t["attempts"] for t in after.values())
    assert res["status"] in ("BUDGET_EXHAUSTED", "COMPLETED_WITH_ERRORS")
    await repo.close()


def test_second_process_on_the_same_database_fails_fast(tmp_path):
    db = tmp_path / "catalog.db"
    lock = DatabaseLock(db).acquire()
    code = (
        "import sys; from catalog_agent.runtime.lock import DatabaseLock, LockHeldError\n"
        f"try:\n    DatabaseLock({str(db)!r}).acquire()\nexcept LockHeldError:\n    sys.exit(3)\nsys.exit(0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], timeout=20)
    lock.release()
    assert proc.returncode == 3
    proc = subprocess.run([sys.executable, "-c", code], timeout=20)
    assert proc.returncode == 0  # released: a new owner can acquire it


async def test_same_product_in_two_runs_keeps_separate_rows(tmp_path):
    cfg = make_config(tmp_path, max_products_per_category=1)
    await run_replay(cfg, "run_one")
    await run_replay(cfg, "run_two")
    repo = await Repository.open(cfg.storage.database)
    cur = await repo.conn.execute(
        "SELECT product_id, COUNT(DISTINCT run_id) FROM products GROUP BY product_id HAVING COUNT(DISTINCT run_id) = 2"
    )
    shared = await cur.fetchall()
    assert shared, "same product_id should be stored once per run"
    p1 = {p["product_id"]: p for p in await repo.products("run_one")}
    p2 = {p["product_id"]: p for p in await repo.products("run_two")}
    assert set(p1) == set(p2)
    assert {v["variant_id"] for p in p1.values() for v in p["variants"]} == {
        v["variant_id"] for p in p2.values() for v in p["variants"]
    }
    await repo.close()
