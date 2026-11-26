"""Typed contracts shared across components: agent protocol, snapshots, export schema (PRD §5)."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums (PRD §5)
# ---------------------------------------------------------------------------


class MissingReason(StrEnum):
    NOT_VISIBLE = "not_visible"
    LOGIN_REQUIRED = "login_required"
    NOT_APPLICABLE = "not_applicable"
    PARSE_FAILED = "parse_failed"
    NOT_OBSERVED = "not_observed"


class PriceVisibility(StrEnum):
    PUBLIC = "public"
    LOGIN_REQUIRED = "login_required"
    NOT_SHOWN = "not_shown"


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    BACKORDER = "backorder"
    LIMITED = "limited"
    UNKNOWN = "unknown"


class PageType(StrEnum):
    CATEGORY = "category"
    LISTING = "listing"
    PRODUCT = "product"
    OTHER = "other"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class TaskType(StrEnum):
    DISCOVERY = "discovery"
    LISTING = "listing"
    PRODUCT = "product"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    RETRY_WAIT = "RETRY_WAIT"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


class ErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION = "CONNECTION"
    HTTP_5XX = "HTTP_5XX"
    THROTTLED = "THROTTLED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    OFF_SCOPE_REDIRECT = "OFF_SCOPE_REDIRECT"
    INCOMPLETE_RENDER = "INCOMPLETE_RENDER"
    UNSUPPORTED_PAGE = "UNSUPPORTED_PAGE"
    PARSE_FAILED = "PARSE_FAILED"
    MODEL_ERROR = "MODEL_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    BUDGET = "BUDGET"
    DATABASE = "DATABASE"
    INTERNAL = "INTERNAL"


# ---------------------------------------------------------------------------
# Agent contract (Tech Stack §4)
# ---------------------------------------------------------------------------


class Issue(BaseModel):
    code: str
    message: str
    field: str | None = None
    severity: Literal["info", "warning", "critical"] = "warning"


class Event(BaseModel):
    event: str
    outcome: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Budget(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def add(self, other: Budget) -> Budget:
        return Budget(
            model_calls=self.model_calls + other.model_calls,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            seconds=self.seconds + other.seconds,
        )


Outcome = Literal["ok", "partial", "retry", "quarantine", "failed"]


class AgentResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    outcome: Outcome
    output: BaseModel | None = None
    issues: list[Issue] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    budget_used: Budget = Field(default_factory=Budget)


class AgentInput(BaseModel):
    """Base class for agent-specific inputs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Agent(Protocol):
    name: str

    async def run(self, task: Any) -> AgentResult: ...


# ---------------------------------------------------------------------------
# Snapshots and sections
# ---------------------------------------------------------------------------


class Row(BaseModel):
    """One variant row inside a section; cells hold text exactly as found."""

    row_id: str
    cells: dict[str, str]


class Section(BaseModel):
    section_id: str
    kind: str
    title: str | None = None
    text: str = ""
    rows: list[Row] = Field(default_factory=list)
    data: dict[str, Any] | None = None

    def row(self, row_id: str) -> Row | None:
        return next((r for r in self.rows if r.row_id == row_id), None)


class PageSnapshot(BaseModel):
    """Immutable fetched page. `html` is the rendered DOM; sections are derived deterministically."""

    snapshot_id: str
    url: str
    final_url: str
    status: int
    fetched_at: str
    content_hash: str
    html: str
    path: str | None = None  # relative path in snapshot_dir

    @staticmethod
    def make_id(final_url: str, content_hash: str) -> str:
        return "s_" + hashlib.sha256(f"{final_url}|{content_hash}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Export schema (PRD §5)
# ---------------------------------------------------------------------------


class MissingField(BaseModel):
    field: str
    reason: MissingReason


class EvidenceRef(BaseModel):
    field: Literal["sku", "price"]
    snapshot_id: str
    section_id: str
    row_id: str | None = None
    raw_value: str
    normalized_value: str
    method: Literal["deterministic", "llm"]


class Alternative(BaseModel):
    name: str
    url: str
    sku: str | None = None


class Variant(BaseModel):
    variant_id: str
    product_id: str
    sku: str
    variant_name: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    pack_label: str | None = None
    pack_quantity: int | None = None
    pack_unit: str | None = None
    price: str | None = None
    currency: str | None = None
    price_visibility: PriceVisibility = PriceVisibility.NOT_SHOWN
    availability: Availability = Availability.UNKNOWN
    availability_raw: str | None = None
    llm_fields: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    missing: list[MissingField] = Field(default_factory=list)


class Product(BaseModel):
    product_id: str
    run_id: str
    source: str
    product_key: str
    url: str
    name: str
    brand: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    category_paths: list[list[str]] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    alternatives: list[Alternative] = Field(default_factory=list)
    variant_count: int = 0
    record_status: Literal["accepted", "partial"] = "accepted"
    extraction_method: Literal["deterministic", "hybrid"] = "deterministic"
    llm_fields: list[str] = Field(default_factory=list)
    missing: list[MissingField] = Field(default_factory=list)
    observed_at: str
    variants: list[Variant] = Field(default_factory=list)


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic ID (PRD §5.7): identical across runs for the same inputs."""
    return prefix + hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def product_id_for(source: str, product_key: str) -> str:
    return stable_id("p_", source, product_key)


def variant_id_for(product_id: str, sku: str) -> str:
    return stable_id("v_", product_id, sku)


# ---------------------------------------------------------------------------
# Crawl tasks and agent payloads
# ---------------------------------------------------------------------------


class CrawlTask(BaseModel):
    task_id: int
    run_id: str
    task_key: str
    task_type: TaskType
    url: str
    root: str | None = None
    lineage: list[list[str]] = Field(default_factory=list)  # category paths (names)
    depth: int = 0
    attempts: int = 0
    status: TaskStatus = TaskStatus.PENDING


class LinkCandidate(BaseModel):
    url: str
    kind: Literal["root", "subcategory", "product", "next_page"]
    label: str | None = None


class ChildTask(BaseModel):
    task_type: TaskType
    url: str
    root: str | None
    lineage: list[list[str]]
    depth: int


class NavigatorOutput(BaseModel):
    children: list[ChildTask] = Field(default_factory=list)
    rejected: list[tuple[str, str]] = Field(default_factory=list)  # (url, reason)
    pagination_stop: str | None = None
    degraded_discovery: bool = False


class ClassifierOutput(BaseModel):
    page_type: PageType
    reason: str


class VariantObservation(BaseModel):
    """A variant as observed by deterministic parsing, before validation."""

    sku: str
    row_id: str | None
    section_id: str
    sku_raw: str
    variant_name: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    pack_label: str | None = None
    price_raw: str | None = None
    price_section_id: str | None = None
    price_row_id: str | None = None
    availability_raw: str | None = None
    method: Literal["deterministic", "llm"] = "deterministic"
    llm_fields: list[str] = Field(default_factory=list)


class ProductCandidate(BaseModel):
    snapshot_id: str
    url: str
    product_key: str
    name: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    breadcrumb: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    alternatives: list[Alternative] = Field(default_factory=list)
    variants: list[VariantObservation] = Field(default_factory=list)
    expected_skus: list[str] = Field(default_factory=list)  # independently observed SKU set
    login_required_price: bool = False
    llm_fields: list[str] = Field(default_factory=list)


class Gap(BaseModel):
    """A recoverable gap the model may try to resolve (never a field that is simply not shown)."""

    kind: Literal["missing_variant", "unparsed_price", "unparsed_attributes"]
    sku: str | None = None
    detail: str = ""


class ExtractorOutput(BaseModel):
    candidate: ProductCandidate
    gaps_remaining: list[Gap] = Field(default_factory=list)
    patches_applied: int = 0
    patches_rejected: int = 0
    model_used: bool = False
    degraded: str | None = None


class ValidatorOutput(BaseModel):
    product: Product | None
    decision: Literal["accepted", "partial", "quarantined"]
    expected_skus: int
    observed_skus: int
    rejected_values: int = 0


class RecoveryDecision(BaseModel):
    action: Literal["retry", "fail", "pause_scope", "quarantine"]
    delay_seconds: float = 0.0
    error_code: ErrorCode
    reason: str


class FetchError(Exception):
    """Typed fetch failure."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
