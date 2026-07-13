from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


FinalStatus = Literal["completed", "needs_review", "blocked"]
RetrievalStatus = Literal["ok", "partial", "failed"]
RetrievalCoverage = Literal["complete", "partial", "unknown"]


class ReviewRunSnapshot(BaseModel):
    schema_version: str = "1.0"
    review_run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    parent_run_id: str | None = None
    final_status: FinalStatus
    requires_human_review: bool
    knowledge_snapshot_id: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    retrieval_request: dict[str, Any] = Field(default_factory=dict)
    retrieval_status: RetrievalStatus | None = None
    retrieval_coverage: RetrievalCoverage | None = None
    retrieval_results: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_errors: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    classified_documents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    extracted_documents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    normalized_documents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pairing_result: dict[str, Any] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    regulatory_findings: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any]
    audit_events: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_and_references_are_consistent(self) -> "ReviewRunSnapshot":
        report_status = self.report.get("overall_status")
        if report_status and report_status != self.final_status:
            raise ValueError("report overall_status must equal final_status")
        if self.final_status == "completed" and self.requires_human_review:
            raise ValueError("completed run cannot require human review")
        if self.final_status != "completed" and not self.requires_human_review:
            raise ValueError("non-completed run must require human review")
        if self.final_status == "completed" and self.retrieval_status is not None:
            if self.retrieval_status != "ok" or self.retrieval_coverage != "complete":
                raise ValueError("completed run requires ok/complete retrieval")
            if self.retrieval_errors:
                raise ValueError("completed run cannot contain retrieval errors")

        document_ids = {
            str(document.get("document_id"))
            for document in self.documents
            if document.get("document_id")
        }
        for mapping_name, mapping in (
            ("classified_documents", self.classified_documents),
            ("extracted_documents", self.extracted_documents),
            ("normalized_documents", self.normalized_documents),
        ):
            unknown = set(mapping) - document_ids
            if document_ids and unknown:
                raise ValueError(f"{mapping_name} contains unknown document IDs: {sorted(unknown)}")

        item_ids = {str(item.get("item_id")) for item in self.items if item.get("item_id")}
        evidence_keys: set[tuple[str, str]] = set()
        for evidence in self.evidence:
            required = ("evidence_id", "item_id", "source_record_id", "version_hash", "source_uri")
            missing = [key for key in required if not evidence.get(key)]
            if missing:
                raise ValueError(f"evidence missing required fields: {missing}")
            item_id = str(evidence["item_id"])
            if item_ids and item_id not in item_ids:
                raise ValueError(f"evidence references unknown item_id: {item_id}")
            evidence_keys.add((item_id, str(evidence["evidence_id"])))

        for finding in self.regulatory_findings:
            finding_item_id = finding.get("item_id")
            if not finding_item_id:
                raise ValueError("regulatory finding must include item_id")
            evidence_ids = finding.get("evidence_ids") or []
            if not evidence_ids:
                raise ValueError("regulatory finding must include evidence_ids")
            for evidence_id in evidence_ids:
                if (str(finding_item_id), str(evidence_id)) not in evidence_keys:
                    raise ValueError(
                        "finding references evidence outside the same item/run: "
                        f"{finding_item_id}/{evidence_id}"
                    )
        return self

    def canonical_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=False)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
