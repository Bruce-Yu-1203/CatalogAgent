"""robots.txt matching with '*' and '$' wildcards (urllib.robotparser does not support them)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


class RobotsPolicy:
    def __init__(self, text: str, user_agent: str = "*"):
        self._rules: list[tuple[bool, re.Pattern[str], int]] = []
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        last_was_agent = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if value:
                    rules.append((key == "allow", value))
            else:
                last_was_agent = False
        if agents or rules:
            groups.append((agents, rules))
        ua = user_agent.lower()
        chosen = [r for a, r in groups if any(x != "*" and x in ua for x in a)]
        if not chosen:
            chosen = [r for a, r in groups if "*" in a]
        for rule_list in chosen:
            for allow, pattern in rule_list:
                self._rules.append((allow, self._compile(pattern), len(pattern)))

    @staticmethod
    def _compile(pattern: str) -> re.Pattern[str]:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.compile("^" + regex + ("$" if anchored else ""))

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, rx, length in self._rules:
            if rx.match(target):
                # Longest match wins; on a tie, allow wins (RFC 9309).
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]
