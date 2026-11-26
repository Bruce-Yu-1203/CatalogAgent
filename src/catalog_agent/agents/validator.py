"""Validator & Deduplicator Agent: schema, identity, evidence and completeness. Never uses a model.

Evidence is resolved against sections rebuilt from the original snapshot HTML, never against text
supplied by the model.
"""

from __future__ import annotations

import time
from types import ModuleType

from catalog_agent.contracts import (
    AgentResult,
    Availability,
    Event,
    EvidenceRef,
    Gap,
    Issue,
    MissingField,
    MissingReason,
    PageSnapshot,
    PriceVisibility,
    Product,
    ProductCandidate,
    ValidatorOutput,
    Variant,
    VariantObservation,
    product_id_for,
    variant_id_for,
)
from catalog_agent.extraction.normalize import normalize_availability, normalize_price, parse_pack


class EvidenceChecker:
    """Resolves (section_id, row_id, raw_value) references against the snapshot."""

    def __init__(self, adapter: ModuleType, snapshot: PageSnapshot):
        self.adapter = adapter
        self.sections = {s.section_id: s for s in adapter.build_sections(snapshot)}

    def cell(self, section_id: str, row_id: str | None, field: str) -> str | None:
        sec = self.sections.get(section_id)
        if sec is None or row_id is None:
            return None
        row = sec.row(row_id)
        if row is None:
            return None
        cell_map = self.adapter.SKU_CELL if field == "sku" else self.adapter.PRICE_CELL
        name = cell_map.get(section_id)
        return row.cells.get(name) if name else None

    def verify(self, field: str, section_id: str, row_id: str | None, raw: str) -> bool:
        value = self.cell(section_id, row_id, field)
        # Exact cell equality: a substring match does not establish row association.
        return value is not None and value.strip() == raw.strip() and raw.strip() != ""

    def row_text(self, section_id: str, row_id: str | None) -> str:
        sec = self.sections.get(section_id)
        row = sec.row(row_id) if sec and row_id else None
        return " ".join(row.cells.values()) if row else ""

    def currency(self, section_id: str, row_id: str | None, raw: str) -> str | None:
        sec = self.sections.get(section_id)
        row = sec.row(row_id) if sec and row_id else None
        cur = row.cells.get("currency") if row else None
        if cur:
            return cur.upper()
        return "USD" if "$" in raw else None


def find_gaps(
    candidate: ProductCandidate, snapshot: PageSnapshot, adapter: ModuleType, attribute_gaps: bool
) -> list[Gap]:
    """Recoverable gaps only: something is displayed but was not extracted reliably."""
    gaps: list[Gap] = []
    have = {v.sku for v in candidate.variants}
    for sku in candidate.expected_skus:
        if sku not in have:
            gaps.append(Gap(kind="missing_variant", sku=sku, detail="SKU observed on page but not extracted"))
    for v in candidate.variants:
        if v.price_raw and normalize_price(v.price_raw) is None:
            gaps.append(Gap(kind="unparsed_price", sku=v.sku, detail=f"price text not parseable: {v.price_raw!r}"))
    if attribute_gaps:
        todo = [v.sku for v in candidate.variants if v.variant_name and set(v.attributes) <= {"Group"}]
        if todo:
            gaps.append(
                Gap(
                    kind="unparsed_attributes",
                    detail="variant label text has no structured attributes: " + ",".join(todo[:20]),
                )
            )
    return gaps


class ValidatorAgent:
    name = "validator"

    def __init__(self, adapter: ModuleType, source: str):
        self.adapter = adapter
        self.source = source

    async def run(self, args: tuple[ProductCandidate, PageSnapshot, str, list[list[str]]]) -> AgentResult:
        t0 = time.perf_counter()
        candidate, snapshot, run_id, lineage = args
        out, issues = self.validate(candidate, snapshot, run_id, lineage)
        outcome = {"accepted": "ok", "partial": "partial", "quarantined": "quarantine"}[out.decision]
        return AgentResult(
            outcome=outcome,
            output=out,
            issues=issues,
            events=[
                Event(
                    event="validated",
                    outcome=out.decision,
                    data={
                        "expected_skus": out.expected_skus,
                        "variants": out.observed_skus,
                        "rejected_values": out.rejected_values,
                    },
                )
            ],
            budget_used={"seconds": time.perf_counter() - t0},
        )

    def _variant(
        self,
        obs: VariantObservation,
        pid: str,
        ev: EvidenceChecker,
        cand: ProductCandidate,
        snapshot: PageSnapshot,
        issues: list[Issue],
    ) -> tuple[Variant | None, int]:
        rejected = 0
        if not ev.verify("sku", obs.section_id, obs.row_id, obs.sku_raw) or obs.sku != obs.sku_raw.strip():
            issues.append(
                Issue(
                    code="sku_evidence_failed",
                    message=f"SKU {obs.sku!r} not verified",
                    field="sku",
                    severity="critical",
                )
            )
            return None, 1
        method = obs.method
        evidence = [
            EvidenceRef(
                field="sku",
                snapshot_id=snapshot.snapshot_id,
                section_id=obs.section_id,
                row_id=obs.row_id,
                raw_value=obs.sku_raw,
                normalized_value=obs.sku,
                method=method,
            )
        ]
        missing: list[MissingField] = []
        llm_fields = list(obs.llm_fields)
        price = currency = None
        visibility = PriceVisibility.NOT_SHOWN
        if obs.price_raw:
            norm = normalize_price(obs.price_raw)
            aligned = obs.price_section_id == obs.section_id and obs.price_row_id == obs.row_id
            verified = aligned and ev.verify("price", obs.price_section_id or "", obs.price_row_id, obs.price_raw)
            if norm is not None and verified:
                price = norm
                currency = ev.currency(obs.section_id, obs.row_id, obs.price_raw)
                visibility = PriceVisibility.PUBLIC
                pmethod = "llm" if "price" in llm_fields else "deterministic"
                evidence.append(
                    EvidenceRef(
                        field="price",
                        snapshot_id=snapshot.snapshot_id,
                        section_id=obs.price_section_id or "",
                        row_id=obs.price_row_id,
                        raw_value=obs.price_raw,
                        normalized_value=norm,
                        method=pmethod,
                    )
                )
                if currency is None:
                    price = None
                    visibility = PriceVisibility.NOT_SHOWN
                    missing.append(MissingField(field="price", reason=MissingReason.PARSE_FAILED))
                    evidence.pop()
            else:
                rejected += 1
                issues.append(
                    Issue(
                        code="price_evidence_failed" if norm else "price_parse_failed",
                        message=f"price {obs.price_raw!r} for {obs.sku} rejected",
                        field="price",
                    )
                )
                missing.append(MissingField(field="price", reason=MissingReason.PARSE_FAILED))
                llm_fields = [f for f in llm_fields if f != "price"]
        elif cand.login_required_price:
            visibility = PriceVisibility.LOGIN_REQUIRED
            missing.append(MissingField(field="price", reason=MissingReason.LOGIN_REQUIRED))
        else:
            missing.append(MissingField(field="price", reason=MissingReason.NOT_VISIBLE))
        if price is None:
            missing.append(
                MissingField(field="currency", reason=missing[-1].reason if missing else MissingReason.NOT_VISIBLE)
            )

        pack_label, pack_qty, pack_unit = parse_pack(obs.pack_label) if obs.pack_label else (None, None, None)
        if pack_label is None:
            missing.append(MissingField(field="pack_label", reason=MissingReason.NOT_VISIBLE))
        elif pack_qty is None:
            missing.append(MissingField(field="pack_quantity", reason=MissingReason.PARSE_FAILED))
        avail = normalize_availability(obs.availability_raw)
        avail_raw = obs.availability_raw
        if avail_raw and "schema.org/" in avail_raw:
            avail_raw = avail_raw.rsplit("/", 1)[-1]
        if not avail_raw:
            missing.append(MissingField(field="availability_raw", reason=MissingReason.NOT_VISIBLE))
        if obs.variant_name is None:
            missing.append(MissingField(field="variant_name", reason=MissingReason.NOT_VISIBLE))

        attrs: dict[str, str] = {}
        row_text = ev.row_text(obs.section_id, obs.row_id).lower()
        for k, v in obs.attributes.items():
            key = f"attributes.{k}"
            if not (isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 40 and 0 < len(v) <= 200):
                rejected += 1
                llm_fields = [f for f in llm_fields if f != key]
                continue
            if key in llm_fields and v.lower() not in row_text:
                # Model-supplied attribute must at least appear in its own row's text.
                rejected += 1
                llm_fields = [f for f in llm_fields if f != key]
                issues.append(Issue(code="attribute_ungrounded", message=f"{k}={v!r} not in row text", field=key))
                continue
            attrs[k] = v
        variant = Variant(
            variant_id=variant_id_for(pid, obs.sku),
            product_id=pid,
            sku=obs.sku,
            variant_name=obs.variant_name,
            attributes=attrs,
            pack_label=pack_label,
            pack_quantity=pack_qty,
            pack_unit=pack_unit,
            price=price,
            currency=currency,
            price_visibility=visibility,
            availability=avail if isinstance(avail, Availability) else Availability.UNKNOWN,
            availability_raw=avail_raw,
            llm_fields=sorted(set(llm_fields)),
            evidence=evidence,
            missing=missing,
        )
        return variant, rejected

    def validate(
        self, cand: ProductCandidate, snapshot: PageSnapshot, run_id: str, lineage: list[list[str]]
    ) -> tuple[ValidatorOutput, list[Issue]]:
        issues: list[Issue] = []
        ev = EvidenceChecker(self.adapter, snapshot)
        expected = len(set(cand.expected_skus))
        if not cand.name or not cand.url or not cand.product_key:
            issues.append(Issue(code="identity_missing", message="product name/url/key missing", severity="critical"))
            return ValidatorOutput(
                product=None, decision="quarantined", expected_skus=expected, observed_skus=0
            ), issues

        # Row ambiguity: the same SKU observed with conflicting prices is never silently accepted.
        prices: dict[str, set[str]] = {}
        for v in cand.variants:
            if v.price_raw:
                prices.setdefault(v.sku, set()).add(normalize_price(v.price_raw) or v.price_raw)
        ambiguous = [s for s, ps in prices.items() if len(ps) > 1]
        if ambiguous:
            issues.append(Issue(code="sku_ambiguous", message=f"conflicting rows for {ambiguous}", severity="critical"))
            return ValidatorOutput(
                product=None, decision="quarantined", expected_skus=expected, observed_skus=0
            ), issues

        pid = product_id_for(self.source, cand.product_key)
        variants: list[Variant] = []
        rejected = 0
        seen: set[str] = set()
        for obs in cand.variants:
            if obs.sku in seen:
                continue
            seen.add(obs.sku)
            v, r = self._variant(obs, pid, ev, cand, snapshot, issues)
            rejected += r
            if v is not None:
                variants.append(v)

        if expected and not variants:
            issues.append(
                Issue(code="no_verified_variants", message="variants observed but none verified", severity="critical")
            )
            return ValidatorOutput(
                product=None, decision="quarantined", expected_skus=expected, observed_skus=0, rejected_values=rejected
            ), issues

        got = {v.sku for v in variants}
        missing_skus = [s for s in dict.fromkeys(cand.expected_skus) if s not in got]
        partial = False
        if missing_skus:
            partial = True
            issues.append(Issue(code="variant_coverage_incomplete", message=f"missing SKUs: {missing_skus[:10]}"))
        if not variants:
            partial = True
            issues.append(Issue(code="no_variants_observed", message="no sellable variant rows observed"))
        if any(m.reason == MissingReason.PARSE_FAILED for v in variants for m in v.missing if m.field == "price"):
            partial = True

        missing: list[MissingField] = []
        if not cand.brand:
            missing.append(MissingField(field="brand", reason=MissingReason.NOT_VISIBLE))
        if not cand.manufacturer:
            missing.append(
                MissingField(
                    field="manufacturer",
                    reason=MissingReason.NOT_APPLICABLE if cand.brand else MissingReason.NOT_VISIBLE,
                )
            )
        if not cand.description:
            missing.append(MissingField(field="description", reason=MissingReason.NOT_VISIBLE))

        crumb = list(cand.breadcrumb)
        if crumb and crumb[-1] == cand.name:
            crumb = crumb[:-1]
        paths: list[list[str]] = []
        for p in [*lineage, crumb]:
            if p and p not in paths:
                paths.append(p)

        llm_fields = sorted(set(cand.llm_fields))
        method = "hybrid" if llm_fields or any(v.llm_fields for v in variants) else "deterministic"
        product = Product(
            product_id=pid,
            run_id=run_id,
            source=self.source,
            product_key=cand.product_key,
            url=cand.url,
            name=cand.name,
            brand=cand.brand,
            manufacturer=cand.manufacturer,
            description=cand.description,
            category_paths=paths,
            image_urls=cand.image_urls,
            attributes=cand.attributes,
            alternatives=cand.alternatives,
            variant_count=len(variants),
            record_status="partial" if partial else "accepted",
            extraction_method=method,
            llm_fields=llm_fields,
            missing=missing,
            observed_at=snapshot.fetched_at,
            variants=variants,
        )
        decision = "partial" if partial else "accepted"
        return ValidatorOutput(
            product=product,
            decision=decision,
            expected_skus=expected,
            observed_skus=len(variants),
            rejected_values=rejected,
        ), issues
