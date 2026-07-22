from __future__ import annotations

import hashlib
import json
from typing import Any


_SAFE_CLAIM_PREFIXES: dict[str, tuple[str, ...]] = {
    "summary": ("", "요약하면, ", "사전심사 요약: "),
    "document_consistency": ("", "문서 정합성 확인 결과, ", "문서 검토 결과: "),
    "regulatory_review": ("", "규정 근거 검토 결과, ", "규정 검토 결과: "),
    "required_actions": ("", "필요 조치: ", "담당자 조치 사항: "),
}
_FACT_KEYS = {
    "fact_id",
    "kind",
    "reference_id",
    "label",
    "value",
    "document_id",
    "source_sha256",
}
_ACTIVE_FAILURE_REASON_CODES = frozenset(
    {
        "active_bounded_execution_failed",
        "active_tool_execution_incomplete",
        "agent_budget_exhausted",
    }
)
_PRODUCTION_RELEASE_ACTION_MESSAGE = (
    "Claude extraction과 법령 범위에 대한 독립 검수 및 Release 승인이 필요합니다."
)
_SUPPORTING_DOCUMENT_ACTION_MESSAGE = "요건서류의 유효성·대상 범위·신뢰 검증을 완료해야 합니다."
_ACTIVE_FAILURE_ACTION_MESSAGE = "제한된 Agent 조사를 완료하지 못해 사람 검토가 필요합니다."


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_object_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{_canonical_digest(value)}"


def _display_value(value: Any) -> str:
    if isinstance(value, str):
        rendered = value.strip()
    else:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return (rendered or "(empty)")[:4000]


def _fact(
    *,
    kind: str,
    reference_id: str,
    label: str,
    value: Any,
    document_id: str | None = None,
) -> dict[str, Any]:
    rendered = _display_value(value)
    canonical = {
        "kind": kind,
        "reference_id": reference_id,
        "label": label,
        "value": rendered,
        "document_id": document_id,
    }
    digest = _canonical_digest(canonical)
    return {
        "fact_id": f"fact_{digest}",
        **canonical,
        "source_sha256": digest,
    }


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _reconstruct_current_facts(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[str] | str]]:
    entries: list[dict[str, Any]] = []
    field_references: list[str] = []
    for document_value in _require_list(payload.get("document_results"), "document_results"):
        document = _require_dict(document_value, "document result")
        document_id = document.get("document_id")
        if not isinstance(document_id, str):
            raise ValueError("document result requires document_id")
        for field_value in _require_list(document.get("fields"), "document fields"):
            field = _require_dict(field_value, "document field")
            name = field.get("name")
            source = field.get("source")
            if not isinstance(name, str) or not isinstance(source, dict):
                raise ValueError("document field requires name and source")
            reference_id = _stable_object_id(
                "field",
                {
                    "document_id": document_id,
                    "field_path": f"fields.{name}",
                    "source": source,
                },
            )
            field_references.append(reference_id)
            entries.append(
                _fact(
                    kind="field_source",
                    reference_id=reference_id,
                    label=f"{document_id}.fields.{name}",
                    value=(
                        field.get("normalized_value")
                        if field.get("normalized_value") is not None
                        else field.get("value")
                    ),
                    document_id=document_id,
                )
            )
        for line_item_value in _require_list(document.get("line_items"), "line items"):
            line_item = _require_dict(line_item_value, "line item")
            item_id = line_item.get("item_id")
            if not isinstance(item_id, str):
                raise ValueError("line item requires item_id")
            for field_value in _require_list(line_item.get("fields"), "line-item fields"):
                field = _require_dict(field_value, "line-item field")
                name = field.get("name")
                source = field.get("source")
                if not isinstance(name, str) or not isinstance(source, dict):
                    raise ValueError("line-item field requires name and source")
                reference_id = _stable_object_id(
                    "field",
                    {
                        "document_id": document_id,
                        "field_path": f"line_items.{item_id}.{name}",
                        "source": source,
                    },
                )
                field_references.append(reference_id)
                entries.append(
                    _fact(
                        kind="field_source",
                        reference_id=reference_id,
                        label=f"{document_id}.line_items.{item_id}.{name}",
                        value=(
                            field.get("normalized_value")
                            if field.get("normalized_value") is not None
                            else field.get("value")
                        ),
                        document_id=document_id,
                    )
                )

    issue_references: list[str] = []
    issues = _require_list(payload.get("validation_issues"), "validation_issues")
    for index, issue_value in enumerate(issues):
        issue = _require_dict(issue_value, "validation issue")
        if not isinstance(issue.get("issue_code"), str) or not isinstance(
            issue.get("message"), str
        ):
            raise ValueError("validation issue requires issue_code and message")
        reference_id = _stable_object_id("issue", {"index": index, "issue": issue})
        issue_references.append(reference_id)
        entries.append(
            _fact(
                kind="issue",
                reference_id=reference_id,
                label=issue["issue_code"],
                value=issue["message"],
                document_id=issue.get("source_document_id"),
            )
        )

    finding_references: list[str] = []
    findings = _require_list(payload.get("regulatory_findings"), "regulatory_findings")
    for index, finding_value in enumerate(findings):
        finding = _require_dict(finding_value, "regulatory finding")
        if not isinstance(finding.get("finding_code"), str) or not isinstance(
            finding.get("message"), str
        ):
            raise ValueError("regulatory finding requires finding_code and message")
        reference_id = _stable_object_id(
            "finding",
            {"index": index, "finding": finding},
        )
        finding_references.append(reference_id)
        source = finding.get("source")
        entries.append(
            _fact(
                kind="finding",
                reference_id=reference_id,
                label=finding["finding_code"],
                value=finding["message"],
                document_id=(source.get("document_id") if isinstance(source, dict) else None),
            )
        )

    evidence_references: list[str] = []
    evidence_items = _require_list(payload.get("rag_evidence"), "rag_evidence")
    for evidence_value in evidence_items:
        evidence = _require_dict(evidence_value, "RAG evidence")
        evidence_id = evidence.get("evidence_id")
        if (
            not isinstance(evidence_id, str)
            or not isinstance(evidence.get("title"), str)
            or not isinstance(evidence.get("text"), str)
        ):
            raise ValueError("RAG evidence requires evidence_id, title and text")
        evidence_references.append(evidence_id)
        entries.append(
            _fact(
                kind="evidence",
                reference_id=evidence_id,
                label=evidence["title"],
                value=evidence["text"],
            )
        )

    outcome_value = {
        "status": payload.get("status"),
        "requires_human_review": payload.get("requires_human_review"),
        "retrieval_status": payload.get("retrieval_status"),
        "retrieval_coverage": payload.get("retrieval_coverage"),
    }
    outcome_reference = _stable_object_id("outcome", outcome_value)
    entries.append(
        _fact(
            kind="outcome",
            reference_id=outcome_reference,
            label="deterministic_outcome",
            value=outcome_value,
        )
    )
    entries.sort(key=lambda item: (str(item["kind"]), str(item["reference_id"])))
    return entries, {
        "fields": field_references,
        "issues": issue_references,
        "findings": finding_references,
        "evidence": evidence_references,
        "outcome": outcome_reference,
    }


def _expected_narrative(
    payload: dict[str, Any],
    references: dict[str, list[str] | str],
) -> dict[str, list[dict[str, Any]]]:
    issues = _require_list(payload.get("validation_issues"), "validation_issues")
    findings = _require_list(payload.get("regulatory_findings"), "regulatory_findings")
    evidence = _require_list(payload.get("rag_evidence"), "rag_evidence")
    citable_evidence = [
        _require_dict(item, "RAG evidence")
        for item in evidence
        if _require_dict(item, "RAG evidence").get("eligibility") != "blocked"
    ]
    outcome_reference = references["outcome"]
    if not isinstance(outcome_reference, str):
        raise ValueError("outcome reference is malformed")
    outcome_citation = [{"kind": "outcome", "reference_id": outcome_reference}]

    if issues or findings:
        summary_text = "문서 또는 규정 검토에서 담당자 확인이 필요한 항목이 발견되었습니다."
    elif (
        payload.get("retrieval_status") == "ok"
        and payload.get("retrieval_coverage") == "complete"
        and citable_evidence
    ):
        summary_text = "현재 검토 범위에서 문서 불일치와 미충족 규정 요건이 발견되지 않았습니다."
    else:
        summary_text = "규정 근거가 완전하지 않아 자동 사전심사를 확정할 수 없습니다."

    issue_references = references["issues"]
    finding_references = references["findings"]
    evidence_references = references["evidence"]
    if not all(
        isinstance(value, list)
        for value in (issue_references, finding_references, evidence_references)
    ):
        raise ValueError("semantic references are malformed")

    document_consistency = [
        {
            "text": f"{issue['issue_code']}: {issue['message']}",
            "citations": [{"kind": "issue", "reference_id": reference_id}],
        }
        for issue, reference_id in zip(issues, issue_references, strict=True)
    ]
    if not document_consistency:
        document_consistency.append(
            {
                "text": "결정론적 문서 교차검증에서 불일치가 발견되지 않았습니다.",
                "citations": outcome_citation,
            }
        )

    regulatory_review: list[dict[str, Any]] = []
    for finding, reference_id in zip(findings, finding_references, strict=True):
        evidence_ids = finding.get("evidence_ids")
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) for item in evidence_ids
        ):
            raise ValueError("regulatory finding evidence_ids are malformed")
        regulatory_review.append(
            {
                "text": f"{finding['finding_code']}: {finding['message']}",
                "citations": [
                    {"kind": "finding", "reference_id": reference_id},
                    *[
                        {"kind": "evidence", "reference_id": evidence_id}
                        for evidence_id in evidence_ids
                    ],
                ],
            }
        )
    if not regulatory_review:
        if citable_evidence:
            regulatory_review.append(
                {
                    "text": "조회된 규정 근거 범위에서 별도의 미충족 요건 Finding이 생성되지 않았습니다.",
                    "citations": [
                        {
                            "kind": "evidence",
                            "reference_id": citable_evidence[0]["evidence_id"],
                        }
                    ],
                }
            )
        else:
            regulatory_review.append(
                {
                    "text": "인용 가능한 규정 근거가 없어 규정 판단을 보류합니다.",
                    "citations": outcome_citation,
                }
            )

    required_actions: list[dict[str, Any]] = []
    if payload.get("requires_human_review") is True:
        required_actions.append(
            {
                "text": "담당자는 근거와 추출값을 확인한 뒤 수정 또는 승인을 기록해야 합니다.",
                "citations": outcome_citation,
            }
        )
    if payload.get("status") == "blocked":
        required_actions.append(
            {
                "text": "차단 원인을 해소한 뒤 새로운 Review Run으로 다시 실행해야 합니다.",
                "citations": outcome_citation,
            }
        )
    if not required_actions:
        eligibility = _require_dict(
            payload.get("completion_eligibility"),
            "completion_eligibility",
        )
        unmet = _require_list(eligibility.get("unmet_reason_codes"), "unmet reason codes")
        release_approved = eligibility.get("eligible") is True or set(unmet) == {
            "narrative_release_identity_incomplete"
        }
        required_actions.append(
            {
                "text": (
                    "승인된 자동완료 범위에서 추가 조치가 필요한 항목이 없습니다."
                    if release_approved
                    else "현재 결과는 Release Gate 승인 전까지 자동완료에 사용할 수 없습니다."
                ),
                "citations": outcome_citation,
            }
        )

    return {
        "summary": [{"text": summary_text, "citations": outcome_citation}],
        "document_consistency": document_consistency,
        "regulatory_review": regulatory_review,
        "required_actions": required_actions,
    }


def _validate_policy_projection(
    payload: dict[str, Any],
    references: dict[str, list[str] | str],
) -> None:
    """Rebuild all user-visible state and actions owned by Agent policy code."""

    outcome_reference = references["outcome"]
    issue_references = references["issues"]
    finding_references = references["findings"]
    if (
        not isinstance(outcome_reference, str)
        or not isinstance(issue_references, list)
        or not isinstance(finding_references, list)
    ):
        raise ValueError("semantic policy references are malformed")

    eligibility = _require_dict(
        payload.get("completion_eligibility"),
        "completion_eligibility",
    )
    eligible = eligibility.get("eligible") is True
    unmet_reason_codes = _require_list(
        eligibility.get("unmet_reason_codes"),
        "completion eligibility reasons",
    )
    if any(not isinstance(value, str) for value in unmet_reason_codes):
        raise ValueError("completion eligibility reasons must be strings")

    gaps = [
        _require_dict(value, "review gap")
        for value in _require_list(payload.get("review_gaps"), "review_gaps")
    ]
    active_failure_gaps = [
        gap
        for gap in gaps
        if gap.get("gap_type") == "release_gate"
        and gap.get("reason_code") in _ACTIVE_FAILURE_REASON_CODES
    ]
    if any(
        gap.get("source_reference_ids") != [outcome_reference] or gap.get("status") != "open"
        for gap in active_failure_gaps
    ):
        raise ValueError("active failure gaps must bind the current outcome and remain open")
    active_failure_codes = sorted({str(gap["reason_code"]) for gap in active_failure_gaps})

    expected_actions: list[dict[str, Any]] = []
    issues = _require_list(payload.get("validation_issues"), "validation_issues")
    for issue_value, reference_id in zip(issues, issue_references, strict=True):
        issue = _require_dict(issue_value, "validation issue")
        expected_actions.append(
            {
                "action_code": f"resolve_{str(issue['issue_code']).casefold()}",
                "message": issue["message"],
                "source_reference_ids": [reference_id],
            }
        )
    findings = _require_list(payload.get("regulatory_findings"), "regulatory_findings")
    for finding_value, reference_id in zip(findings, finding_references, strict=True):
        finding = _require_dict(finding_value, "regulatory finding")
        evidence_ids = _require_list(
            finding.get("evidence_ids"),
            "regulatory finding Evidence IDs",
        )
        if any(not isinstance(value, str) for value in evidence_ids):
            raise ValueError("regulatory finding Evidence IDs must be strings")
        expected_actions.append(
            {
                "action_code": f"resolve_{str(finding['finding_code']).casefold()}",
                "message": finding["message"],
                "source_reference_ids": [reference_id, *evidence_ids],
            }
        )
    if not eligible:
        expected_actions.append(
            {
                "action_code": "obtain_production_release_approval",
                "message": _PRODUCTION_RELEASE_ACTION_MESSAGE,
                "source_reference_ids": [outcome_reference],
            }
        )
    if "supporting_document_verification_incomplete" in unmet_reason_codes:
        expected_actions.append(
            {
                "action_code": "verify_supporting_document",
                "message": _SUPPORTING_DOCUMENT_ACTION_MESSAGE,
                "source_reference_ids": [outcome_reference],
            }
        )
    expected_actions.extend(
        {
            "action_code": reason_code,
            "message": _ACTIVE_FAILURE_ACTION_MESSAGE,
            "source_reference_ids": [outcome_reference],
        }
        for reason_code in active_failure_codes
    )
    actual_actions = _require_list(payload.get("required_actions"), "required_actions")
    if actual_actions != expected_actions:
        raise ValueError("required actions are not the exact code-owned policy projection")

    observations = [
        _require_dict(value, "Tool observation")
        for value in _require_list(payload.get("observations"), "observations")
    ]
    action_failed = any(item.get("status") != "succeeded" for item in observations)
    model_errors = _require_list(payload.get("model_errors"), "model_errors")
    deterministic_status = payload.get("status")
    expected_execution = (
        "failed"
        if deterministic_status == "blocked"
        else ("partial" if model_errors or active_failure_codes or action_failed else "succeeded")
    )
    if payload.get("execution_status") != expected_execution:
        raise ValueError("execution status is not derived from model and Tool outcomes")

    open_gaps = any(gap.get("status") != "resolved" for gap in gaps)
    expected_disposition = (
        "blocked"
        if deterministic_status == "blocked"
        else (
            "auto_review_complete"
            if eligible and not open_gaps and not action_failed
            else "human_review_required"
        )
    )
    if payload.get("review_disposition") != expected_disposition:
        raise ValueError("review disposition is not the exact policy projection")
    if (payload.get("assurance_level") == "production") is not eligible:
        raise ValueError("production assurance must exactly match completion eligibility")


def _validate_retrieval_item(item: dict[str, Any]) -> None:
    item_id = item.get("item_id")
    evidence = _require_list(item.get("evidence"), "retrieval item evidence")
    errors = _require_list(item.get("errors"), "retrieval item errors")
    if any(not isinstance(value, dict) or value.get("item_id") != item_id for value in evidence):
        raise ValueError("retrieval Evidence must bind its result item")
    evidence_ids = [value.get("evidence_id") for value in evidence]
    if any(not isinstance(value, str) for value in evidence_ids) or len(evidence_ids) != len(
        set(evidence_ids)
    ):
        raise ValueError("retrieval Evidence IDs must be unique")
    candidates = _require_list(item.get("candidate_hsk10s"), "candidate HSK codes")
    if any(
        not isinstance(value, str) or len(value) != 10 or not value.isdigit()
        for value in candidates
    ) or len(candidates) != len(set(candidates)):
        raise ValueError("candidate HSK codes must be unique 10-digit values")
    resolution = item.get("hs_resolution")
    resolved = item.get("resolved_hsk10")
    if resolution in {"exact", "unique_descendant"}:
        if not isinstance(resolved, str) or candidates not in ([], [resolved]):
            raise ValueError("resolved Evidence item has inconsistent HSK candidates")
    elif resolution == "ambiguous":
        if resolved is not None or len(candidates) < 2:
            raise ValueError("ambiguous Evidence item requires multiple candidates")
    elif resolved is not None or candidates:
        raise ValueError("unresolved Evidence item cannot claim a resolved HSK code")
    finding_evidence = [
        value
        for value in evidence
        if value.get("source_type") == "customs_requirement"
        and value.get("eligibility") == "finding"
    ]
    if item.get("requirements_found") is not bool(finding_evidence):
        raise ValueError("requirements_found must match finding-eligible Evidence")
    if item.get("coverage") == "complete" and errors:
        raise ValueError("complete Evidence item cannot contain errors")


def _validate_v2_semantics(payload: dict[str, Any]) -> None:
    """Mirror the provider's security-relevant V2 cross-layer invariants."""

    report = _require_dict(payload.get("report"), "report")
    summary = _require_dict(report.get("summary"), "report summary")
    if payload.get("case_id") != report.get("case_id"):
        raise ValueError("report case_id is not bound to the review")
    if payload.get("status") != report.get("overall_status"):
        raise ValueError("report status is not bound to the review")
    if payload.get("requires_human_review") is not (payload.get("status") != "completed"):
        raise ValueError("requires_human_review is not derived from status")
    for key in ("retrieval_status", "retrieval_coverage", "knowledge_snapshot_id"):
        if summary.get(key) != payload.get(key):
            raise ValueError(f"report {key} is not bound to the review")

    request = payload.get("retrieval_request")
    results = _require_list(payload.get("retrieval_results"), "retrieval_results")
    errors = _require_list(payload.get("retrieval_errors"), "retrieval_errors")
    public_evidence = _require_list(payload.get("rag_evidence"), "rag_evidence")
    result_ids = [_require_dict(value, "retrieval result").get("item_id") for value in results]
    if any(not isinstance(value, str) for value in result_ids) or len(result_ids) != len(
        set(result_ids)
    ):
        raise ValueError("retrieval result item IDs must be unique")
    for value in results:
        _validate_retrieval_item(_require_dict(value, "retrieval result"))

    if request is None:
        if (
            payload.get("retrieval_status") != "failed"
            or results
            or public_evidence
            or payload.get("knowledge_snapshot_id") is not None
        ):
            raise ValueError("a result without a retrieval request cannot claim Evidence")
    else:
        request_object = _require_dict(request, "retrieval_request")
        for key in (
            "case_id",
            "review_run_id",
            "review_as_of",
            "trade_type",
            "jurisdiction",
        ):
            if request_object.get(key) != payload.get(key):
                raise ValueError("retrieval request identity is not bound to the review")
        request_items = _require_list(request_object.get("items"), "retrieval request items")
        request_ids = [
            _require_dict(value, "retrieval request item").get("item_id") for value in request_items
        ]
        if any(not isinstance(value, str) for value in request_ids) or len(request_ids) != len(
            set(request_ids)
        ):
            raise ValueError("retrieval request item IDs must be unique")
        if not set(result_ids).issubset(request_ids):
            raise ValueError("retrieval results are outside the request")
        if payload.get("retrieval_status") == "ok" and set(result_ids) != set(request_ids):
            raise ValueError("ok retrieval must cover every request item")

    has_result_problem = any(
        _require_dict(value, "retrieval result").get("coverage") != "complete"
        or bool(_require_dict(value, "retrieval result").get("errors"))
        for value in results
    )
    retrieval_status = payload.get("retrieval_status")
    if retrieval_status == "ok" and (
        not payload.get("knowledge_snapshot_id") or errors or has_result_problem
    ):
        raise ValueError("ok retrieval requires a snapshot and error-free complete results")
    if retrieval_status == "partial" and not (errors or has_result_problem):
        raise ValueError("partial retrieval requires an incomplete or errored result")
    if (
        request is not None
        and retrieval_status == "failed"
        and not (
            errors
            or any(
                bool(_require_dict(value, "retrieval result").get("errors")) for value in results
            )
        )
    ):
        raise ValueError("failed retrieval requires an error")

    if not results:
        derived_coverage = "unknown"
    elif all(
        _require_dict(value, "retrieval result").get("coverage") == "complete" for value in results
    ):
        derived_coverage = "complete"
    elif any(
        _require_dict(value, "retrieval result").get("coverage") == "unknown" for value in results
    ):
        derived_coverage = "unknown"
    else:
        derived_coverage = "partial"
    if payload.get("retrieval_coverage") != derived_coverage:
        raise ValueError("retrieval coverage is not derived from item results")
    if payload.get("status") == "completed" and (
        request is None
        or retrieval_status != "ok"
        or derived_coverage != "complete"
        or not payload.get("knowledge_snapshot_id")
        or errors
        or not results
        or any(
            _require_dict(value, "retrieval result").get("coverage") != "complete"
            or bool(_require_dict(value, "retrieval result").get("errors"))
            or _require_dict(value, "retrieval result").get("hs_resolution")
            not in {"exact", "unique_descendant"}
            or not _require_dict(value, "retrieval result").get("resolved_hsk10")
            for value in results
        )
    ):
        raise ValueError("completed result requires real complete Evidence retrieval")

    flattened_evidence = [
        evidence
        for value in results
        for evidence in _require_list(
            _require_dict(value, "retrieval result").get("evidence"),
            "retrieval item evidence",
        )
    ]
    if flattened_evidence != public_evidence:
        raise ValueError("public Evidence must exactly equal item-level Evidence")
    public_evidence_ids = [
        _require_dict(value, "RAG evidence").get("evidence_id") for value in public_evidence
    ]
    if any(not isinstance(value, str) for value in public_evidence_ids) or len(
        public_evidence_ids
    ) != len(set(public_evidence_ids)):
        raise ValueError("public Evidence IDs must be globally unique")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for value in public_evidence:
        evidence = _require_dict(value, "RAG evidence")
        source_record_id = evidence.get("source_record_id")
        version_hash = evidence.get("version_hash")
        if not isinstance(source_record_id, str) or not isinstance(version_hash, str):
            raise ValueError("Evidence requires source record and version identity")
        expected_id = (
            "ev_"
            + hashlib.sha256(f"{source_record_id}\x1f{version_hash}".encode("utf-8")).hexdigest()
        )
        if evidence.get("evidence_id") != expected_id:
            raise ValueError("Evidence ID is not bound to source record and version")
        if evidence.get("trade_type") != payload.get("trade_type"):
            raise ValueError("Evidence trade type is not bound to the review")
        for list_key in ("agencies", "retrieval_methods"):
            values = _require_list(evidence.get(list_key), f"Evidence {list_key}")
            if len(values) != len(set(values)):
                raise ValueError(f"Evidence {list_key} must be unique")
        evidence_by_id[expected_id] = evidence

    document_results = _require_list(payload.get("document_results"), "document_results")
    documents_by_id: dict[str, dict[str, Any]] = {}
    line_item_hs_sources: dict[str, dict[str, Any]] = {}
    for value in document_results:
        document = _require_dict(value, "document result")
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or document_id in documents_by_id:
            raise ValueError("document result IDs must be present and unique")
        documents_by_id[document_id] = document
        field_names: list[str] = []
        all_fields: list[dict[str, Any]] = []
        for field_value in _require_list(document.get("fields"), "document fields"):
            field = _require_dict(field_value, "document field")
            field_names.append(str(field.get("name")))
            all_fields.append(field)
        if len(field_names) != len(set(field_names)):
            raise ValueError("document field names must be unique")
        item_ids: list[str] = []
        for item_value in _require_list(document.get("line_items"), "line items"):
            item = _require_dict(item_value, "line item")
            item_id = item.get("item_id")
            if not isinstance(item_id, str):
                raise ValueError("line item requires item_id")
            item_ids.append(item_id)
            fields = [
                _require_dict(field_value, "line-item field")
                for field_value in _require_list(item.get("fields"), "line-item fields")
            ]
            all_fields.extend(fields)
            hs_fields = [field for field in fields if field.get("name") == "hs_code"]
            if len(hs_fields) == 1:
                if item_id in line_item_hs_sources:
                    raise ValueError("line item IDs must be globally unique")
                line_item_hs_sources[item_id] = _require_dict(
                    hs_fields[0].get("source"),
                    "HS field source",
                )
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("line item IDs must be unique within a document")
        for field in all_fields:
            source = _require_dict(field.get("source"), "field source")
            if source.get("document_id") != document_id or source.get(
                "source_modality"
            ) != document.get("input_modality"):
                raise ValueError("field source is not bound to its parent document")

    report_documents = _require_list(report.get("document_summary"), "report documents")
    report_documents_by_id = {
        _require_dict(value, "report document").get("document_id"): _require_dict(
            value, "report document"
        )
        for value in report_documents
    }
    if (
        len(report_documents_by_id) != len(report_documents)
        or set(report_documents_by_id) != set(documents_by_id)
        or summary.get("total_documents") != len(report_documents)
    ):
        raise ValueError("report documents do not exactly match document results")
    for document_id, document in documents_by_id.items():
        report_document = report_documents_by_id[document_id]
        if any(
            report_document.get(key) != document.get(key)
            for key in ("document_type", "extraction_status", "extraction_confidence")
        ):
            raise ValueError("report document summary is not bound to extraction result")

    issues = _require_list(payload.get("validation_issues"), "validation_issues")
    findings = _require_list(payload.get("regulatory_findings"), "regulatory_findings")
    if summary.get("total_issues") != len(issues) + len(findings):
        raise ValueError("report issue count is not derived from findings and issues")
    for severity in ("critical", "high", "medium", "low"):
        expected_count = sum(
            _require_dict(value, "issue").get("severity") == severity for value in issues
        ) + sum(_require_dict(value, "finding").get("severity") == severity for value in findings)
        if summary.get(severity) != expected_count:
            raise ValueError("report severity counts are not derived from findings and issues")
    report_issues = [
        {
            key: value
            for key, value in _require_dict(item, "report issue").items()
            if key != "recommendation"
        }
        for item in _require_list(report.get("issues"), "report issues")
    ]
    if report_issues != issues:
        raise ValueError("report issues do not exactly match validation issues")
    if _require_list(report.get("regulatory_findings"), "report findings") != findings:
        raise ValueError("report findings do not exactly match regulatory findings")
    normalized_report_evidence: list[dict[str, Any]] = []
    for item_value in _require_list(report.get("rag_evidence"), "report Evidence"):
        item = dict(_require_dict(item_value, "report Evidence item"))
        scores = _require_list(item.get("raw_scores"), "report Evidence scores")
        score_map: dict[str, Any] = {}
        for score_value in scores:
            score = _require_dict(score_value, "report Evidence score")
            method = score.get("method")
            if not isinstance(method, str) or method in score_map:
                raise ValueError("report Evidence score methods must be unique")
            score_map[method] = score.get("score")
        item["raw_scores"] = score_map
        normalized_report_evidence.append(item)
    if normalized_report_evidence != public_evidence:
        raise ValueError("report Evidence does not exactly match public Evidence")

    for finding_value in findings:
        finding = _require_dict(finding_value, "regulatory finding")
        finding_source = line_item_hs_sources.get(str(finding.get("item_id")))
        if finding.get("severity") in {"critical", "high"} and (
            finding_source is None or finding.get("source") != finding_source
        ):
            raise ValueError("material finding is not bound to its public HS source")
        if finding.get("source") is not None and finding.get("source") != finding_source:
            raise ValueError("finding source is not bound to its public HS source")
        evidence_ids = _require_list(finding.get("evidence_ids"), "finding evidence IDs")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("finding Evidence references must be unique")
        for evidence_id in evidence_ids:
            finding_evidence = evidence_by_id.get(str(evidence_id))
            if (
                finding_evidence is None
                or finding_evidence.get("item_id") != finding.get("item_id")
                or finding_evidence.get("source_type") != "customs_requirement"
                or finding_evidence.get("eligibility") != "finding"
                or finding_evidence.get("required_document_code")
                != finding.get("required_document_code")
                or finding_evidence.get("required_document_name")
                != finding.get("required_document_name")
            ):
                raise ValueError("finding is not bound to matching finding-eligible Evidence")

    for call_value in _require_list(payload.get("model_calls"), "model_calls"):
        call = _require_dict(call_value, "model call")
        call_document = documents_by_id.get(str(call.get("document_id")))
        modality = call.get("input_modality")
        if call_document is None or (
            (modality == "text" and call_document.get("input_modality") != "text")
            or (modality != "text" and call_document.get("input_modality") != "image")
        ):
            raise ValueError("model call is not bound to its public document result")
    for error_value in _require_list(payload.get("model_errors"), "model_errors"):
        error = _require_dict(error_value, "model error")
        error_document = documents_by_id.get(str(error.get("document_id")))
        if error_document is None or (
            error.get("input_modality") == "text" and error_document.get("input_modality") != "text"
        ):
            raise ValueError("model error is not bound to its public document result")


def validate_agent_v3_semantics(payload: dict[str, Any]) -> None:
    """Reject self-consistent but semantically forged Agent V3 content.

    The Agent owns its V2 deterministic result.  The V3 FactLedger and every
    material public narrative claim must be an exact, independently derived
    projection of that result.  Historical investigation facts remain trace-only
    and can never become user-facing citations.
    """

    _validate_v2_semantics(payload)
    expected_entries, references = _reconstruct_current_facts(payload)
    ledger = _require_dict(payload.get("fact_ledger"), "fact_ledger")
    actual_entries = _require_list(ledger.get("entries"), "FactLedger entries")
    if any(not isinstance(item, dict) or set(item) != _FACT_KEYS for item in actual_entries):
        raise ValueError("FactLedger entries must use the exact provider schema")
    expected_reference_ids = {str(item["reference_id"]) for item in expected_entries}
    current_entries = [
        item for item in actual_entries if item.get("reference_id") in expected_reference_ids
    ]
    investigation_entries = [
        item for item in actual_entries if item.get("reference_id") not in expected_reference_ids
    ]
    if current_entries != expected_entries:
        raise ValueError("current FactLedger is not derived from the deterministic V2 result")

    plan = _require_dict(payload.get("review_plan"), "review_plan")
    planned_actions = _require_list(plan.get("actions"), "review plan actions")
    runtime = _require_dict(payload.get("runtime_release"), "runtime_release")
    if plan.get("provider") == "anthropic":
        if (
            plan.get("model_id") != runtime.get("model_id")
            or plan.get("prompt_sha256") != runtime.get("coordinator_prompt_sha256")
            or plan.get("output_schema_sha256") != runtime.get("coordinator_schema_sha256")
            or not isinstance(plan.get("input_tokens"), int)
            or isinstance(plan.get("input_tokens"), bool)
            or plan.get("input_tokens", 0) <= 0
            or not isinstance(plan.get("output_tokens"), int)
            or isinstance(plan.get("output_tokens"), bool)
            or plan.get("output_tokens", 0) <= 0
        ):
            raise ValueError("Claude review plan audit is not bound to the runtime release")
    elif (
        plan.get("provider") != "deterministic_policy"
        or plan.get("model_id") is not None
        or plan.get("prompt_sha256") is not None
        or plan.get("output_schema_sha256") is not None
        or plan.get("input_tokens") != 0
        or plan.get("output_tokens") != 0
        or plan.get("latency_ms") != 0
    ):
        raise ValueError("deterministic review plan cannot claim model execution")
    planned_input_references: set[str] = set()
    for action_value in planned_actions:
        action = _require_dict(action_value, "review plan action")
        input_references = _require_list(
            action.get("input_reference_ids"),
            "review plan input references",
        )
        if any(not isinstance(item, str) for item in input_references):
            raise ValueError("review plan input references must be strings")
        planned_input_references.update(input_references)
    current_reference_ids = expected_reference_ids
    if {
        str(item.get("reference_id")) for item in investigation_entries
    } != planned_input_references - current_reference_ids or any(
        item.get("label") == "investigation_input:" for item in investigation_entries
    ):
        raise ValueError("historical investigation facts must exactly bind missing plan inputs")

    _validate_policy_projection(payload, references)
    expected_sections = _expected_narrative(payload, references)
    narrative = _require_dict(payload.get("narrative_report"), "narrative_report")
    generation_mode = narrative.get("generation_mode")
    narrative_call = narrative.get("model_call")
    if generation_mode == "claude":
        call = _require_dict(narrative_call, "narrative model_call")
        if (
            call.get("task_id") != "customs_narrative_report"
            or call.get("provider") != "anthropic"
            or call.get("model_id") != runtime.get("model_id")
            or call.get("prompt_sha256") != runtime.get("narrative_prompt_sha256")
            or call.get("output_schema_sha256") != runtime.get("narrative_schema_sha256")
            or not isinstance(call.get("input_tokens"), int)
            or isinstance(call.get("input_tokens"), bool)
            or call.get("input_tokens", 0) <= 0
            or not isinstance(call.get("output_tokens"), int)
            or isinstance(call.get("output_tokens"), bool)
            or call.get("output_tokens", 0) <= 0
        ):
            raise ValueError("Claude narrative audit is not bound to the runtime release")
    elif generation_mode != "deterministic_fallback" or narrative_call is not None:
        raise ValueError("deterministic narrative cannot claim model execution")
    for section, expected_claims in expected_sections.items():
        actual_claims = _require_list(narrative.get(section), f"narrative {section}")
        if len(actual_claims) != len(expected_claims):
            raise ValueError("narrative must exactly cover code-owned claims")
        for actual_value, expected in zip(actual_claims, expected_claims, strict=True):
            actual = _require_dict(actual_value, "narrative claim")
            text = actual.get("text")
            if (
                actual.get("material") is not True
                or actual.get("citations") != expected["citations"]
                or not isinstance(text, str)
                or text
                not in (
                    {str(expected["text"])}
                    if generation_mode == "deterministic_fallback"
                    else {f"{prefix}{expected['text']}" for prefix in _SAFE_CLAIM_PREFIXES[section]}
                    if generation_mode == "claude"
                    else set()
                )
            ):
                raise ValueError(
                    "narrative text and citations are not the safe projection of V2 facts"
                )


__all__ = ["validate_agent_v3_semantics"]
