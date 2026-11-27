"""CatalogAgent CLI: crawl, resume, export, report."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from catalog_agent.config import AppConfig, load_config
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
    replay: Annotated[Path | None, typer.Option(help="replay saved fixtures (offline) instead of live")] = None,
    db: Annotated[str | None, typer.Option(help="override storage.database")] = None,
    sitemap_sample: Annotated[
        int | None, typer.Option(help="seed N random sitemap products instead of discovery")
    ] = None,
    sample_seed: Annotated[int, typer.Option(help="random seed for --sitemap-sample")] = 0,
) -> None:
    """Start a new bounded crawl."""
    if mode not in ("deterministic", "hybrid"):
        raise typer.BadParameter("mode must be deterministic or hybrid")
    cfg = load_config(config)
    if replay is not None:
        cfg.replay_dir = str(replay)
    if db is not None:
        cfg.storage.database = db
    if sitemap_sample is not None:
        if not cfg.site.sitemap_url:
            raise typer.BadParameter("site.sitemap_url is not configured")
        cfg.site.sitemap_sample = sitemap_sample
        cfg.site.sitemap_seed = sample_seed
        cfg.crawler.max_products_per_category = max(cfg.crawler.max_products_per_category, sitemap_sample)
    # CLI overrides are assigned after loading; validate again so a value that `resume` would reject later
    # (it re-reads the run's stored configuration) fails here instead.
    try:
        cfg = AppConfig.model_validate(cfg.model_dump())
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
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
    # Always written, so a file from an earlier run can never be mistaken for this run's result.
    (out_dir / "repair_suggestions.json").write_text(json.dumps(rep.get("repair_suggestions", []), indent=2) + "\n")
    _print({k: rep[k] for k in ("run_id", "status", "termination_reason", "catalog", "tasks")})


@app.command()
def evaluate(
    fixtures: Annotated[Path, typer.Option(help="frozen labelled fixtures")] = Path("tests/fixtures/eval"),
    mode: Annotated[str, typer.Option(help="deterministic | hybrid | compare")] = "compare",
    config: Path = Path("configs/safco.yaml"),
    out: Path = Path("outputs/evaluation_report.json"),
) -> None:
    """Offline deterministic-vs-hybrid evaluation (FR-13). Hybrid needs model credentials."""
    from catalog_agent.adapters import safco
    from catalog_agent.evaluation import evaluate_mode, summarize
    from catalog_agent.extraction.llm import ModelUnavailable, model_from_env

    cfg = load_config(config)
    modes = ["deterministic", "hybrid"] if mode == "compare" else [mode]
    model = None
    if "hybrid" in modes:
        try:
            model = model_from_env(cfg.llm.max_output_tokens_per_call)
        except ModelUnavailable as e:
            raise typer.BadParameter(f"hybrid evaluation needs a model: {e}") from e

    async def run() -> list[dict]:
        return [await evaluate_mode(cfg, safco, fixtures, m, model if m == "hybrid" else None) for m in modes]

    results = asyncio.run(run())
    report = {
        "fixtures": str(fixtures),
        "model_id": getattr(model, "model_id", None),
        "note": "Replay of frozen snapshots; not full-site accuracy. Labels: see fixtures README.",
        "summaries": [summarize(r, cfg.llm.input_price_per_mtok, cfg.llm.output_price_per_mtok) for r in results],
        "pages": results,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    _print([{k: s[k] for k in ("mode", "all", "held_out_live", "synthetic")} for s in report["summaries"]])


@app.command()
def experiment(
    run_id: Annotated[str, typer.Option(help="run whose saved product snapshots are replayed")],
    groups: Annotated[str, typer.Option(help="subset of A,B,C,D")] = "A,B,C,D",
    redesigns: Annotated[str, typer.Option(help="redesign kinds for C and D: data,classes,tags,columns")] = "classes",
    config: Path = Path("configs/safco.yaml"),
    db: Annotated[str | None, typer.Option(help="override storage.database")] = None,
    out: Path = Path("outputs/redesign_experiment.json"),
) -> None:
    """Offline 2x2: original vs SYNTHETIC redesigned pages (JSON-LD removed, classes renamed), rules vs rules + LLM."""
    from catalog_agent.adapters import safco
    from catalog_agent.evaluation import redesign_experiment, summarize_experiment
    from catalog_agent.extraction.llm import ModelUnavailable, model_from_env
    from catalog_agent.runtime.fetcher import SnapshotStore
    from catalog_agent.storage.repository import Repository

    cfg = load_config(config)
    gs = [g.strip().upper() for g in groups.split(",") if g.strip()]
    model = None
    if {"B", "D"} & set(gs):
        try:
            model = model_from_env(cfg.llm.max_output_tokens_per_call)
        except ModelUnavailable as e:
            raise typer.BadParameter(f"groups B and D need a model: {e}") from e

    async def run() -> dict:
        repo = await Repository.open(db or cfg.storage.database)
        try:
            tasks = await repo.tasks(run_id)
        finally:
            await repo.close()
        store = SnapshotStore(cfg.storage.snapshot_dir)
        snaps = [store.load(t["snapshot_path"]) for t in tasks if t["page_type"] == "product" and t["snapshot_path"]]
        kinds = tuple(k.strip() for k in redesigns.split(",") if k.strip())
        return await redesign_experiment(cfg, safco, snaps, gs, model, kinds)

    result = asyncio.run(run())
    summary = summarize_experiment(result, cfg.llm.input_price_per_mtok, cfg.llm.output_price_per_mtok)
    report = {
        "run_id": run_id,
        "model_id": getattr(model, "model_id", None),
        "note": (
            "Offline replay of the run's saved product pages. C and D use SYNTHETIC redesigned copies (JSON-LD "
            "removed and every class renamed; 'data' keeps inline scripts with product data, the other kinds remove "
            "them; 'tags' also changes element types, 'columns' also reverses the children of repeated elements); "
            "reference SKUs and prices come from the original page's JSON-LD."
        ),
        "summary": summary,
        "groups": result,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    _print(summary)


@app.command()
def summary(
    run_id: Annotated[str | None, typer.Option(help="run to summarize (default: latest)")] = None,
    db: str = DB,
    out: Annotated[Path | None, typer.Option(help="also write the Markdown to this file")] = None,
) -> None:
    """Print the run summary and data-quality tables (Markdown): pages, outcomes, model use, field completeness,
    missing/rejection reasons and duplicates."""
    from catalog_agent.quality import markdown_summary

    _, _, rep = asyncio.run(_load(db, run_id))
    text = markdown_summary(rep)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    typer.echo(text)


if __name__ == "__main__":
    app()
