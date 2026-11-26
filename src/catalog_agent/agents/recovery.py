"""Recovery Agent: retry, back off, pause or fail by error class (P0). Repair suggestions are P1."""

from __future__ import annotations

import random

from catalog_agent.contracts import AgentResult, ErrorCode, Event, RecoveryDecision

TRANSIENT = {
    ErrorCode.TIMEOUT,
    ErrorCode.CONNECTION,
    ErrorCode.HTTP_5XX,
    ErrorCode.INCOMPLETE_RENDER,
    ErrorCode.DATABASE,
    ErrorCode.INTERNAL,
}
TERMINAL = {
    ErrorCode.NOT_FOUND,
    ErrorCode.ROBOTS_DISALLOWED,
    ErrorCode.OFF_SCOPE_REDIRECT,
    ErrorCode.UNSUPPORTED_PAGE,
    ErrorCode.PARSE_FAILED,
}


class RecoveryAgent:
    name = "recovery"

    def __init__(self, max_attempts: int, backoff_base: float, backoff_max: float, rng: random.Random | None = None):
        self.max_attempts = max_attempts
        self.base = backoff_base
        self.cap = backoff_max
        self.rng = rng or random.Random()

    def backoff(self, attempt: int) -> float:
        delay = min(self.cap, self.base * (2 ** max(0, attempt - 1)))
        return delay + self.rng.uniform(0, delay * 0.25)  # jitter

    async def run(self, error: tuple[ErrorCode, str, int, float | None]) -> AgentResult:
        code, message, attempts, retry_after = error
        left = attempts < self.max_attempts
        if code == ErrorCode.ACCESS_DENIED:
            d = RecoveryDecision(action="pause_scope", error_code=code, reason="access restricted; never bypassed")
        elif code == ErrorCode.THROTTLED:
            if left:
                delay = max(retry_after or 0.0, self.backoff(attempts))
                d = RecoveryDecision(action="retry", delay_seconds=delay, error_code=code, reason="honor Retry-After")
            else:
                d = RecoveryDecision(action="fail", error_code=code, reason="throttled; attempts exhausted")
        elif code in TRANSIENT:
            if left:
                d = RecoveryDecision(
                    action="retry", delay_seconds=self.backoff(attempts), error_code=code, reason="transient"
                )
            else:
                d = RecoveryDecision(action="fail", error_code=code, reason="retry budget exhausted")
        elif code in TERMINAL:
            d = RecoveryDecision(action="fail", error_code=code, reason="terminal")
        else:
            d = RecoveryDecision(action="fail", error_code=code, reason="unclassified")
        return AgentResult(
            outcome="retry" if d.action == "retry" else "failed",
            output=d,
            events=[
                Event(event="recovery_decision", outcome=d.action, data={"code": code.value, "msg": message[:200]})
            ],
        )
