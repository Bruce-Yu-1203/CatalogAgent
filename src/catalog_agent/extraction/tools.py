"""Read-only snapshot tools for the Extractor's model loop.

The tools see only the sections of one immutable snapshot. They take no URLs, paths or queries and
have no network, filesystem, shell, database or secret access. Results are size-bounded.
"""

from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from catalog_agent.contracts import PageSnapshot

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "inspect_sections",
        "description": "List the section IDs of the current page snapshot with kind, title and row counts.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_section",
        "description": "Return one section of the snapshot (text and rows with stable row IDs).",
        "input_schema": {
            "type": "object",
            "properties": {"section_id": {"type": "string", "maxLength": 64}},
            "required": ["section_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_variant_rows",
        "description": "Return every variant row (section_id, row_id, cells) observed on the page.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

SUBMIT_SPEC: dict[str, Any] = {
    "name": "submit_patch",
    "description": (
        "Return the typed extraction patch. Every SKU or price must cite the section_id and row_id where its "
        "raw text appears exactly. Do not invent values; omit what you cannot ground."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "maxItems": 60,
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string", "maxLength": 64},
                        "section_id": {"type": "string", "maxLength": 64},
                        "row_id": {"type": "string", "maxLength": 64},
                        "sku_raw": {"type": "string", "maxLength": 64},
                        "price_raw": {"type": ["string", "null"], "maxLength": 32},
                        "variant_name": {"type": ["string", "null"], "maxLength": 200},
                        "attributes": {
                            "type": "object",
                            "additionalProperties": {"type": "string", "maxLength": 200},
                        },
                    },
                    "required": ["sku", "section_id", "row_id", "sku_raw"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string", "maxLength": 500},
        },
        "required": ["variants"],
        "additionalProperties": False,
    },
}


class SnapshotTools:
    def __init__(self, adapter: ModuleType, snapshot: PageSnapshot, max_chars: int):
        self._sections = {s.section_id: s for s in adapter.build_sections(snapshot)}
        self.max_chars = max_chars

    def _bound(self, payload: Any) -> str:
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + '..."[truncated]"'
        return text

    def call(self, name: str, args: dict[str, Any]) -> str:
        if not isinstance(args, dict):
            return self._bound({"error": "arguments must be an object"})
        if name == "inspect_sections":
            return self._bound(
                [
                    {
                        "section_id": s.section_id,
                        "kind": s.kind,
                        "title": s.title,
                        "rows": len(s.rows),
                        "chars": len(s.text),
                    }
                    for s in self._sections.values()
                ]
            )
        if name == "get_section":
            sid = args.get("section_id")
            sec = self._sections.get(sid) if isinstance(sid, str) else None
            if sec is None:
                return self._bound({"error": "unknown section_id", "known": list(self._sections)})
            return self._bound(
                {
                    "section_id": sec.section_id,
                    "kind": sec.kind,
                    "text": sec.text[:1500],
                    "rows": [r.model_dump() for r in sec.rows],
                }
            )
        if name == "get_variant_rows":
            # The rendered variant table is primary; structured data is the fallback source.
            ordered = sorted(self._sections.values(), key=lambda s: s.kind != "variant_table")
            sec = next((s for s in ordered if s.rows), None)
            if sec is None:
                return self._bound([])
            return self._bound([{"section_id": sec.section_id, **r.model_dump()} for r in sec.rows])
        return self._bound({"error": f"unknown tool {name!r}"})
