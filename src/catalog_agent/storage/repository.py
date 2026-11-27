"""SQLite repository: frontier tasks, run state and catalog records.

Every multi-row effect commits in one short transaction. No transaction is held across network or
model I/O: callers fetch/extract first, then call one of the commit methods.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from catalog_agent.contracts import (
    ChildTask,
    CrawlTask,
    Product,
    RunStatus,
    TaskStatus,
    TaskType,
)

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
PRIORITY = {TaskType.DISCOVERY: 0, TaskType.LISTING: 1, TaskType.PRODUCT: 2}
OPEN_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAIT)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_key(task_type: TaskType, url: str) -> str:
    return f"{task_type.value}:{url}"


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _merge_lineage(existing: list[list[str]], new: Iterable[list[str]]) -> list[list[str]]:
    out = [list(p) for p in existing]
    for p in new:
        if p and list(p) not in out:
            out.append(list(p))
    return out


class InjectedFailure(Exception):
    """Raised by tests to simulate a crash inside a transaction."""


class Repository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self._tx_lock = asyncio.Lock()
        self.fail_before_commit: str | None = None  # test hook: name of the commit to break

    @classmethod
    async def open(cls, path: str | Path) -> Repository:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(SCHEMA)
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    @asynccontextmanager
    async def tx(self, name: str = "tx") -> AsyncIterator[aiosqlite.Connection]:
        async with self._tx_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                if self.fail_before_commit == name:
                    raise InjectedFailure(name)
                await self.conn.execute("COMMIT")
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ runs

    async def create_run(
        self, run_id: str, mode: str, config: dict, max_pages: int, max_products: int, max_model: int
    ) -> None:
        now = utcnow()
        async with self.tx("create_run") as c:
            await c.execute(
                "INSERT INTO crawl_runs(run_id, mode, config_json, status, started_at, updated_at,"
                " max_pages, max_products_per_category, max_model_calls)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, mode, _dumps(config), RunStatus.RUNNING, now, now, max_pages, max_products, max_model),
            )

    async def get_run(self, run_id: str) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_run_id(self) -> str | None:
        cur = await self.conn.execute("SELECT run_id FROM crawl_runs ORDER BY started_at DESC, rowid DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else None

    async def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        async with self.tx("update_run") as c:
            await c.execute(f"UPDATE crawl_runs SET {cols} WHERE run_id = ?", (*fields.values(), run_id))

    async def extend_run(self, run_id: str, add_pages: int, add_model_calls: int, add_products: int) -> None:
        run = await self.get_run(run_id)
        assert run is not None
        exts = json.loads(run["extensions_json"])
        exts.append(
            {"at": utcnow(), "add_pages": add_pages, "add_model_calls": add_model_calls, "add_products": add_products}
        )
        async with self.tx("extend_run") as c:
            await c.execute(
                "UPDATE crawl_runs SET max_pages = max_pages + ?, max_model_calls = max_model_calls + ?,"
                " max_products_per_category = max_products_per_category + ?, extensions_json = ?,"
                " updated_at = ? WHERE run_id = ?",
                (add_pages, add_model_calls, add_products, _dumps(exts), utcnow(), run_id),
            )

    async def record_invocation(self, run_id: str, entry: dict) -> None:
        run = await self.get_run(run_id)
        assert run is not None
        inv = json.loads(run["invocations_json"])
        inv.append(entry)
        await self.update_run(run_id, invocations_json=_dumps(inv))

    async def bump_counters(self, run_id: str, **deltas: int) -> None:
        async with self.tx("bump") as c:
            cur = await c.execute("SELECT counters_json FROM crawl_runs WHERE run_id = ?", (run_id,))
            counters = json.loads((await cur.fetchone())[0])
            for k, v in deltas.items():
                counters[k] = counters.get(k, 0) + v
            await c.execute("UPDATE crawl_runs SET counters_json = ? WHERE run_id = ?", (_dumps(counters), run_id))

    # ----------------------------------------------------------------- tasks

    @staticmethod
    def _row_to_task(row: aiosqlite.Row) -> CrawlTask:
        return CrawlTask(
            task_id=row["task_id"],
            run_id=row["run_id"],
            task_key=row["task_key"],
            task_type=TaskType(row["task_type"]),
            url=row["url"],
            root=row["root"],
            lineage=json.loads(row["lineage_json"]),
            depth=row["depth"],
            attempts=row["attempts"],
            status=TaskStatus(row["status"]),
        )

    async def _enqueue(self, c: aiosqlite.Connection, run_id: str, children: Iterable[ChildTask]) -> int:
        inserted = 0
        now = utcnow()
        for ch in children:
            key = task_key(ch.task_type, ch.url)
            cur = await c.execute(
                "INSERT OR IGNORE INTO crawl_tasks(run_id, task_key, task_type, url, root, lineage_json,"
                " depth, priority, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    key,
                    ch.task_type.value,
                    ch.url,
                    ch.root,
                    _dumps(ch.lineage),
                    ch.depth,
                    PRIORITY[ch.task_type],
                    TaskStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            if cur.rowcount:
                inserted += 1
                continue
            # Known task: merge the new category lineage (idempotent).
            cur = await c.execute(
                "SELECT task_id, lineage_json FROM crawl_tasks WHERE run_id = ? AND task_key = ?", (run_id, key)
            )
            row = await cur.fetchone()
            merged = _merge_lineage(json.loads(row["lineage_json"]), ch.lineage)
            await c.execute(
                "UPDATE crawl_tasks SET lineage_json = ?, updated_at = ? WHERE task_id = ?",
                (_dumps(merged), now, row["task_id"]),
            )
            if ch.task_type == TaskType.PRODUCT:
                cur = await c.execute(
                    "SELECT product_id, category_paths_json FROM products WHERE run_id = ? AND task_id = ?",
                    (run_id, row["task_id"]),
                )
                prow = await cur.fetchone()
                if prow:
                    paths = _merge_lineage(json.loads(prow["category_paths_json"]), ch.lineage)
                    await c.execute(
                        "UPDATE products SET category_paths_json = ? WHERE run_id = ? AND product_id = ?",
                        (_dumps(paths), run_id, prow["product_id"]),
                    )
        return inserted

    async def enqueue(self, run_id: str, children: Iterable[ChildTask]) -> int:
        async with self.tx("enqueue") as c:
            return await self._enqueue(c, run_id, children)

    async def get_task(self, task_id: int) -> CrawlTask:
        cur = await self.conn.execute("SELECT * FROM crawl_tasks WHERE task_id = ?", (task_id,))
        return self._row_to_task(await cur.fetchone())

    async def task_counts(self, run_id: str) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) FROM crawl_tasks WHERE run_id = ? GROUP BY status", (run_id,)
        )
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def tasks(self, run_id: str, statuses: Iterable[str] | None = None) -> list[dict]:
        q = "SELECT * FROM crawl_tasks WHERE run_id = ?"
        args: list[Any] = [run_id]
        if statuses:
            st = list(statuses)
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        cur = await self.conn.execute(q + " ORDER BY task_id", args)
        return [dict(r) for r in await cur.fetchall()]

    async def products_done_by_root(self, c: aiosqlite.Connection, run_id: str) -> dict[str, int]:
        cur = await c.execute(
            "SELECT root, COUNT(*) FROM crawl_tasks WHERE run_id = ? AND task_type = 'product'"
            " AND status IN ('COMPLETED','PARTIAL','RUNNING') GROUP BY root",
            (run_id,),
        )
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def claim_next(self, run_id: str, per_task_model_calls: int) -> tuple[CrawlTask | None, str | None]:
        """Atomically claim the next eligible task and reserve its page (and model) budget.

        Returns (task, None) or (None, reason) where reason is 'empty', 'waiting', 'pages' or 'products'.
        """
        now = time.time()
        async with self.tx("claim") as c:
            cur = await c.execute("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,))
            run = await cur.fetchone()
            cur = await c.execute(
                "SELECT * FROM crawl_tasks WHERE run_id = ? AND (status = 'PENDING' OR"
                " (status = 'RETRY_WAIT' AND next_retry_at <= ?)) ORDER BY priority, task_id",
                (run_id, now),
            )
            candidates = await cur.fetchall()
            done = await self.products_done_by_root(c, run_id)
            limit = run["max_products_per_category"]

            def eligible(row: aiosqlite.Row) -> bool:
                if row["task_type"] == TaskType.DISCOVERY.value or not row["root"]:
                    return True
                return done.get(row["root"], 0) < limit

            chosen = next((r for r in candidates if eligible(r)), None)
            if chosen is None:
                # Nothing claimable now. Wait only for work that can still run: tasks in flight, or
                # retries whose category quota is not full. Otherwise the category budget is spent.
                cur = await c.execute(
                    "SELECT task_type, root, status FROM crawl_tasks WHERE run_id = ?"
                    " AND status IN ('RETRY_WAIT','RUNNING')",
                    (run_id,),
                )
                live = [r for r in await cur.fetchall() if r["status"] == "RUNNING" or eligible(r)]
                if live:
                    return None, "waiting"
                return None, ("products" if candidates or await self._has_open(c, run_id) else "empty")
            if run["pages_used"] >= run["max_pages"]:
                return None, "pages"
            reserve = 0
            if chosen["task_type"] == TaskType.PRODUCT.value:
                reserve = max(0, min(per_task_model_calls, run["max_model_calls"] - run["model_calls_used"]))
            await c.execute(
                "UPDATE crawl_runs SET pages_used = pages_used + 1, model_calls_used = model_calls_used + ?,"
                " updated_at = ? WHERE run_id = ?",
                (reserve, utcnow(), run_id),
            )
            await c.execute(
                "UPDATE crawl_tasks SET status = 'RUNNING', attempts = attempts + 1, reserved_model_calls = ?,"
                " updated_at = ? WHERE task_id = ?",
                (reserve, utcnow(), chosen["task_id"]),
            )
            cur = await c.execute("SELECT * FROM crawl_tasks WHERE task_id = ?", (chosen["task_id"],))
            task = self._row_to_task(await cur.fetchone())
            return task, None

    async def _has_open(self, c: aiosqlite.Connection, run_id: str) -> bool:
        cur = await c.execute(
            "SELECT COUNT(*) FROM crawl_tasks WHERE run_id = ? AND status IN ('PENDING','RETRY_WAIT')", (run_id,)
        )
        return bool((await cur.fetchone())[0])

    async def reserve_model_calls(self, run_id: str, n: int) -> bool:
        """Reserve n run-level model calls before use; False if the run budget does not allow it."""
        async with self.tx("reserve_model") as c:
            cur = await c.execute(
                "UPDATE crawl_runs SET model_calls_used = model_calls_used + ? WHERE run_id = ?"
                " AND model_calls_used + ? <= max_model_calls",
                (n, run_id, n),
            )
            return cur.rowcount == 1

    async def release_model_calls(self, run_id: str, n: int) -> None:
        if n <= 0:
            return
        async with self.tx("release_model") as c:
            await c.execute(
                "UPDATE crawl_runs SET model_calls_used = MAX(0, model_calls_used - ?) WHERE run_id = ?", (n, run_id)
            )

    async def add_model_usage(self, run_id: str, in_tok: int, out_tok: int) -> None:
        async with self.tx("model_usage") as c:
            await c.execute(
                "UPDATE crawl_runs SET input_tokens_used = input_tokens_used + ?,"
                " output_tokens_used = output_tokens_used + ? WHERE run_id = ?",
                (in_tok, out_tok, run_id),
            )

    async def reserved_model_calls(self, task_id: int) -> int:
        cur = await self.conn.execute("SELECT reserved_model_calls FROM crawl_tasks WHERE task_id = ?", (task_id,))
        return (await cur.fetchone())[0]

    async def _settle_model_budget(
        self, c: aiosqlite.Connection, task: CrawlTask, used_calls: int, in_tok: int, out_tok: int
    ) -> None:
        cur = await c.execute("SELECT reserved_model_calls FROM crawl_tasks WHERE task_id = ?", (task.task_id,))
        reserved = (await cur.fetchone())[0]
        refund = max(0, reserved - used_calls)
        await c.execute(
            "UPDATE crawl_runs SET model_calls_used = model_calls_used - ?, input_tokens_used = input_tokens_used + ?,"
            " output_tokens_used = output_tokens_used + ? WHERE run_id = ?",
            (refund, in_tok, out_tok, task.run_id),
        )
        await c.execute("UPDATE crawl_tasks SET reserved_model_calls = 0 WHERE task_id = ?", (task.task_id,))

    async def _finish_task(
        self,
        c: aiosqlite.Connection,
        task: CrawlTask,
        status: TaskStatus,
        outcome: dict | None,
        snapshot: dict | None = None,
        error: tuple[str, str] | None = None,
        next_retry_at: float | None = None,
    ) -> None:
        snap = snapshot or {}
        await c.execute(
            "UPDATE crawl_tasks SET status = ?, outcome_json = ?, snapshot_id = COALESCE(?, snapshot_id),"
            " snapshot_path = COALESCE(?, snapshot_path), content_hash = COALESCE(?, content_hash),"
            " page_type = COALESCE(?, page_type), error_code = ?, last_error = ?, next_retry_at = ?,"
            " updated_at = ? WHERE task_id = ? AND status = 'RUNNING'",
            (
                status.value,
                _dumps(outcome) if outcome is not None else None,
                snap.get("snapshot_id"),
                snap.get("path"),
                snap.get("content_hash"),
                snap.get("page_type"),
                error[0] if error else None,
                error[1] if error else None,
                next_retry_at,
                utcnow(),
                task.task_id,
            ),
        )

    async def complete_navigation(
        self, task: CrawlTask, children: list[ChildTask], outcome: dict, snapshot: dict, status: TaskStatus
    ) -> int:
        """Children enqueue and parent completion in one transaction."""
        async with self.tx("complete_navigation") as c:
            n = await self._enqueue(c, task.run_id, children)
            await self._finish_task(c, task, status, outcome, snapshot)
            return n

    async def complete_product(
        self,
        task: CrawlTask,
        product: Product | None,
        status: TaskStatus,
        outcome: dict,
        snapshot: dict,
        model_calls: int,
        in_tok: int,
        out_tok: int,
    ) -> None:
        """Product/variant upserts and task completion in one transaction."""
        async with self.tx("complete_product") as c:
            if product is not None:
                await self._upsert_product(c, task, product)
            await self._settle_model_budget(c, task, model_calls, in_tok, out_tok)
            await self._finish_task(c, task, status, outcome, snapshot)

    async def _upsert_product(self, c: aiosqlite.Connection, task: CrawlTask, p: Product) -> None:
        cur = await c.execute(
            "SELECT category_paths_json FROM products WHERE run_id = ? AND product_id = ?", (p.run_id, p.product_id)
        )
        prev = await cur.fetchone()
        paths = _merge_lineage(json.loads(prev[0]) if prev else [], p.category_paths)
        await c.execute(
            "INSERT INTO products(run_id, product_id, source, product_key, url, name, brand, manufacturer,"
            " description, category_paths_json, image_urls_json, attributes_json, alternatives_json,"
            " variant_count, record_status, extraction_method, llm_fields_json, missing_json, observed_at,"
            " root, task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id, product_id) DO UPDATE SET url=excluded.url, name=excluded.name,"
            " brand=excluded.brand, manufacturer=excluded.manufacturer, description=excluded.description,"
            " category_paths_json=excluded.category_paths_json, image_urls_json=excluded.image_urls_json,"
            " attributes_json=excluded.attributes_json, alternatives_json=excluded.alternatives_json,"
            " record_status=excluded.record_status, extraction_method=excluded.extraction_method,"
            " llm_fields_json=excluded.llm_fields_json, missing_json=excluded.missing_json,"
            " observed_at=excluded.observed_at, task_id=excluded.task_id",
            (
                p.run_id,
                p.product_id,
                p.source,
                p.product_key,
                p.url,
                p.name,
                p.brand,
                p.manufacturer,
                p.description,
                _dumps(paths),
                _dumps(p.image_urls),
                _dumps(p.attributes),
                _dumps([a.model_dump() for a in p.alternatives]),
                0,
                p.record_status,
                p.extraction_method,
                _dumps(p.llm_fields),
                _dumps([m.model_dump(mode="json") for m in p.missing]),
                p.observed_at,
                task.root,
                task.task_id,
            ),
        )
        for v in p.variants:
            await c.execute(
                "INSERT INTO variants(run_id, variant_id, product_id, sku, variant_name, attributes_json,"
                " pack_label, pack_quantity, pack_unit, price, currency, price_visibility, availability,"
                " availability_raw, llm_fields_json, evidence_json, missing_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id, variant_id) DO UPDATE SET variant_name=excluded.variant_name,"
                " attributes_json=excluded.attributes_json, pack_label=excluded.pack_label,"
                " pack_quantity=excluded.pack_quantity, pack_unit=excluded.pack_unit, price=excluded.price,"
                " currency=excluded.currency, price_visibility=excluded.price_visibility,"
                " availability=excluded.availability, availability_raw=excluded.availability_raw,"
                " llm_fields_json=excluded.llm_fields_json, evidence_json=excluded.evidence_json,"
                " missing_json=excluded.missing_json",
                (
                    p.run_id,
                    v.variant_id,
                    p.product_id,
                    v.sku,
                    v.variant_name,
                    _dumps(v.attributes),
                    v.pack_label,
                    v.pack_quantity,
                    v.pack_unit,
                    v.price,
                    v.currency,
                    v.price_visibility.value,
                    v.availability.value,
                    v.availability_raw,
                    _dumps(v.llm_fields),
                    _dumps([e.model_dump() for e in v.evidence]),
                    _dumps([m.model_dump(mode="json") for m in v.missing]),
                ),
            )
        # Existing variants are never deleted; variant_count reflects every stored variant.
        await c.execute(
            "UPDATE products SET variant_count = (SELECT COUNT(*) FROM variants WHERE run_id = ? AND product_id = ?)"
            " WHERE run_id = ? AND product_id = ?",
            (p.run_id, p.product_id, p.run_id, p.product_id),
        )

    async def fail_task(
        self,
        task: CrawlTask,
        status: TaskStatus,
        code: str,
        message: str,
        next_retry_at: float | None = None,
        outcome: dict | None = None,
        snapshot: dict | None = None,
        model_calls: int = 0,
        in_tok: int = 0,
        out_tok: int = 0,
    ) -> None:
        async with self.tx("fail_task") as c:
            await self._settle_model_budget(c, task, model_calls, in_tok, out_tok)
            await self._finish_task(c, task, status, outcome, snapshot, (code, message[:500]), next_retry_at)

    async def reconcile_running(self, run_id: str, max_attempts: int) -> tuple[int, int]:
        """After acquiring exclusive ownership: requeue interrupted RUNNING tasks or fail them.

        Reserved page/model budget of interrupted attempts stays consumed (Architecture §6.1).
        """
        async with self.tx("reconcile") as c:
            cur = await c.execute(
                "UPDATE crawl_tasks SET status = 'PENDING', reserved_model_calls = 0, updated_at = ?"
                " WHERE run_id = ? AND status = 'RUNNING' AND attempts < ?",
                (utcnow(), run_id, max_attempts),
            )
            requeued = cur.rowcount
            cur = await c.execute(
                "UPDATE crawl_tasks SET status = 'FAILED', error_code = 'INTERNAL', reserved_model_calls = 0,"
                " last_error = 'interrupted; attempt budget exhausted', updated_at = ?"
                " WHERE run_id = ? AND status = 'RUNNING'",
                (utcnow(), run_id),
            )
            return requeued, cur.rowcount

    async def next_retry_due(self, run_id: str) -> float | None:
        cur = await self.conn.execute(
            "SELECT MIN(next_retry_at) FROM crawl_tasks WHERE run_id = ? AND status = 'RETRY_WAIT'", (run_id,)
        )
        return (await cur.fetchone())[0]

    # -------------------------------------------------------------- catalog

    async def products(self, run_id: str) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM products WHERE run_id = ? ORDER BY root, name", (run_id,))
        prods = [dict(r) for r in await cur.fetchall()]
        cur = await self.conn.execute("SELECT * FROM variants WHERE run_id = ? ORDER BY product_id, sku", (run_id,))
        by_pid: dict[str, list[dict]] = {}
        for r in await cur.fetchall():
            by_pid.setdefault(r["product_id"], []).append(dict(r))
        for p in prods:
            p["variants"] = by_pid.get(p["product_id"], [])
        return prods

    async def count_unique(self, run_id: str) -> tuple[int, int]:
        cur = await self.conn.execute("SELECT COUNT(*) FROM products WHERE run_id = ?", (run_id,))
        n_p = (await cur.fetchone())[0]
        cur = await self.conn.execute("SELECT COUNT(*) FROM variants WHERE run_id = ?", (run_id,))
        return n_p, (await cur.fetchone())[0]
