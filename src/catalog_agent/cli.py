"""CatalogAgent CLI: crawl, resume, export, report."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from catalog_agent.config import load_config
from catalog_agent.quality import build_report
from catalog_agent.runtime.lock import LockHeldError
from catalog_agent.runtime.runner import Extensions, execute
from catalog_agent.storage.export import rows_to_products, write_csv, write_json
from catalog_agent.storage.repository import Repository

app = typer.Typer(add_completion=False, help="Agent-based catalog extraction POC (Safco Dental).")
DB = "data/catalog.db"


def _print(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def crawl(
    config: Annotated[Path, typer.Option(help="YAML configuration")] = Path("configs/safco.yaml"),
    mode: Annotated[str, typer.Option(help="deterministic | hybrid")] = "hybrid",
    run_id: Annotated[str | None, typer.Option(help="explicit run id")] = None,
) -> None:
    """Start a new bounded crawl."""
    if mode not in ("deterministic", "hybrid"):
        raise typer.BadParameter("mode must be deterministic or hybrid")
    cfg = load_config(config)
    try:
        res = asyncio.run(execute(database=cfg.storage.database, cfg=cfg, mode=mode, run_id=run_id))
    except LockHeldError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    _print(res)


@app.command()
def resume(
    run_id: Annotated[str, typer.Option(help="run to resume")],
    db: Annotated[str, typer.Option(help="SQLite database")] = DB,
    add_pages: int = 0,
    add_model_calls: int = 0,
    add_products: int = 0,
) -> None:
    """Resume an interrupted, paused or (with extensions) budget-exhausted run."""
    ext = Extensions(add_pages, add_model_calls, add_products)
    try:
        res = asyncio.run(execute(database=db, run_id=run_id, resume=True, extensions=ext))
    except LockHeldError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    _print(res)


async def _load(db: str, run_id: str | None):
    repo = await Repository.open(db)
    try:
        rid = run_id or await repo.latest_run_id()
        if rid is None:
            raise typer.BadParameter("no runs in database")
        return rid, rows_to_products(await repo.products(rid)), await build_report(repo, rid)
    finally:
        await repo.close()


@app.command()
def export(
    run_id: Annotated[str | None, typer.Option(help="run to export (default: latest)")] = None,
    format: Annotated[str, typer.Option("--format", help="json | csv | all")] = "all",
    db: str = DB,
    out_dir: Path = Path("outputs"),
) -> None:
    """Export one run's catalog as nested JSON and/or variant-level CSV (PRD §5)."""
    rid, products, _ = asyncio.run(_load(db, run_id))
    written = []
    if format in ("json", "all"):
        written.append(str(write_json(products, out_dir / "products.json")))
    if format in ("csv", "all"):
        written.append(str(write_csv(products, out_dir / "variants.csv")))
    _print(
        {
            "run_id": rid,
            "products": len(products),
            "variants": sum(len(p.variants) for p in products),
            "written": written,
        }
    )


@app.command()
def report(
    run_id: Annotated[str | None, typer.Option(help="run to report (default: latest)")] = None,
    db: str = DB,
    out_dir: Path = Path("outputs"),
) -> None:
    """Write run_report.json (with §10.1 metrics) and failed_tasks.json."""
    rid, _, rep = asyncio.run(_load(db, run_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run_report.json").write_text(json.dumps(rep, indent=2, default=str) + "\n")
    (out_dir / "failed_tasks.json").write_text(json.dumps(rep["failed_tasks"], indent=2, default=str) + "\n")
    _print({k: rep[k] for k in ("run_id", "status", "termination_reason", "catalog", "tasks")})


if __name__ == "__main__":
    app()
