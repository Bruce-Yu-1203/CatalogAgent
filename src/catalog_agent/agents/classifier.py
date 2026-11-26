"""Page Classifier Agent: rule-based page type from URL, metadata and DOM signals (no model in P0)."""

from __future__ import annotations

import time
from types import ModuleType

from catalog_agent.contracts import AgentResult, ClassifierOutput, Event, PageSnapshot, PageType


class ClassifierAgent:
    name = "classifier"

    def __init__(self, adapter: ModuleType):
        self.adapter = adapter

    async def run(self, snapshot: PageSnapshot) -> AgentResult:
        t0 = time.perf_counter()
        page_type, reason = self.adapter.classify_signals(snapshot)
        # P1 (FR-11) would call the model for UNKNOWN; disabled, so UNKNOWN is recorded for review.
        outcome = "ok" if page_type not in (PageType.UNKNOWN, PageType.BLOCKED) else "failed"
        return AgentResult(
            outcome=outcome,
            output=ClassifierOutput(page_type=page_type, reason=reason),
            events=[Event(event="classified", outcome=page_type.value, data={"reason": reason})],
            budget_used={"seconds": time.perf_counter() - t0},
        )
