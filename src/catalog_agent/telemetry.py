"""Structured JSON logs. Every event carries run_id, task_id, agent, event, outcome and duration."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("catalog_agent")
_SECRET_KEYS = ("api_key", "apikey", "authorization", "password", "token", "prompt")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
        }
        payload.update(getattr(record, "fields", {}) or {"message": record.getMessage()})
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(log_dir: str | Path | None = None, run_id: str | None = None, quiet: bool = False) -> None:
    _LOGGER.handlers.clear()
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    fmt = JsonFormatter()
    if not quiet:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        _LOGGER.addHandler(h)
    if log_dir and run_id:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(Path(log_dir) / f"{run_id}.jsonl")
        fh.setFormatter(fmt)
        _LOGGER.addHandler(fh)


def _scrub(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if any(s in k.lower() for s in _SECRET_KEYS) else v) for k, v in fields.items()}


def log_event(
    event: str,
    *,
    run_id: str | None = None,
    task_id: int | None = None,
    agent: str = "orchestrator",
    outcome: str | None = None,
    duration_ms: float | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    rec = {
        "run_id": run_id,
        "task_id": task_id,
        "agent": agent,
        "event": event,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        **_scrub(fields),
    }
    _LOGGER.log(level, event, extra={"fields": rec})
