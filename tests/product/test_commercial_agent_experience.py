from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from customs_backend.adapters import (
    CallableAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
)
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.domain import (
    AgentReviewRequest,
    AgentReviewResultV3,
    AgentDocumentInput,
    Principal,
    Role,
    StageEvent,
    canonical_digest,
    utc_now,
)
from customs_backend.experience import (
    AgentUatCapabilityAttestationV1,
    AssistantAnswerV2,
    CaseConversationRequestV2,
    GroundedPresentationV1,
    ReviewExperienceEnvelopeV1,
    AgentReviewStageEventV1,
    build_answer_experience_claim_catalog,
    build_canonical_experience_claim_catalog,
    build_controlled_presentation_sections,
    validate_answer_against_context,
    validate_presentation_against_result,
)
from customs_backend.ports import (
    AgentResponseRejectedError,
    ConflictError,
    JobExecutionNotRunnableError,
)
from customs_backend.experience_contract import validate_agent_experience_contract_bundle
from customs_backend.experience_contract import provider_meaning_catalog
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService, _OrderedAgentStageStream
import customs_backend.service as service_module
from customs_backend.settings import Settings
from tests.product.agent_v3_fixture import build_agent_v3_result


_EVIDENCE_RUNTIME_SHA = "8" * 64
_REVIEW_STAGES = [
    "request_validation",
    "document_analysis",
    "normalization",
    "document_pairing",
    "cross_validation",
    "evidence_retrieval",
    "compliance_review",
    "adaptive_investigation",
    "report_composition",
    "grounding_verification",
    "result_validation",
    "completed",
]
_ANSWER_STAGES = [
    "request_validation",
    "conversation_binding",
    "answer_planning",
    "answer_composition",
    "grounding_verification",
    "result_validation",
    "completed",
]
_SECTION_TITLES = {
    "conclusion": "한눈에 보는 결론",
    "process": "Agent가 수행한 검토 과정",
    "document_comparison": "문서 대조 결과",
    "issues_and_causes": "발견된 문제와 원인",
    "regulatory_meaning": "법령 근거와 이 심사 건에서의 의미",
    "business_impact": "업무상 영향",
    "next_actions": "담당자의 다음 조치",
    "scope_and_limits": "검토 범위와 한계",
}
_SAFE_SUGGESTED_QUESTIONS = [
    "이 결과가 나온 가장 중요한 이유는 무엇입니까?",
    "담당자가 먼저 확인할 항목은 무엇입니까?",
    "연결된 법령 근거가 이 심사 건에서 어떤 의미입니까?",
]
_CASE_LIMITATION = "답변은 현재 심사 회차와 연결된 문서·법령 근거 범위에 한정됩니다."
_GENERAL_GUIDANCE_LIMITATION = (
    "위 내용은 일반 통관 안내이며 현재 심사 건의 판정이나 법적 의무를 뜻하지 않습니다."
)


def _rehash_result(result: dict[str, Any]) -> dict[str, Any]:
    result["result_sha256"] = "0" * 64
    result["result_sha256"] = canonical_digest(result)
    return result


def _agent_event(
    *,
    operation_id: str,
    sequence: int,
    stage_code: str,
    message_key: str | None = None,
) -> dict[str, Any]:
    occurred_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "schema_version": "1.0",
        "operation_id": operation_id,
        "sequence": sequence,
        "stage_code": stage_code,
        "state": "completed",
        "occurred_at": occurred_at,
        "duration_ms": 10,
        "safe_counts": {"documents": 1},
        "message_key": message_key or f"agent.stage.{stage_code}.completed",
    }
    return {**payload, "event_sha256": canonical_digest(payload)}


def _agent_event_with_state(
    *,
    operation_id: str,
    sequence: int,
    stage_code: str,
    state: str,
) -> AgentReviewStageEventV1:
    raw = _agent_event(
        operation_id=operation_id,
        sequence=sequence,
        stage_code=stage_code,
        message_key=f"agent.stage.{stage_code}.{state}",
    )
    raw["state"] = state
    raw["duration_ms"] = None if state == "started" else 10
    raw["event_sha256"] = canonical_digest(
        {key: value for key, value in raw.items() if key != "event_sha256"}
    )
    return AgentReviewStageEventV1.model_validate(raw)


def _review_events() -> list[dict[str, Any]]:
    return [
        _agent_event(
            operation_id="operation-review-1",
            sequence=index,
            stage_code=stage,
            message_key=f"agent.stage.{stage}.completed",
        )
        for index, stage in enumerate(_REVIEW_STAGES)
    ]


def _answer_events() -> list[dict[str, Any]]:
    return [
        _agent_event(
            operation_id="operation-answer-1",
            sequence=index,
            stage_code=stage,
            message_key=f"agent.stage.{stage}.completed",
        )
        for index, stage in enumerate(_ANSWER_STAGES)
    ]


def _experience(request: AgentReviewRequest) -> dict[str, Any]:
    result = build_agent_v3_result(request, auto_complete=False)
    return _experience_from_result(result)


def _experience_from_result(result: dict[str, Any]) -> dict[str, Any]:
    lock = validate_agent_experience_contract_bundle().lock
    result["runtime_release"]["evidence_runtime_v3_sha256"] = _EVIDENCE_RUNTIME_SHA
    result["runtime_release"]["agent_git_sha"] = lock.provider_commit
    result["runtime_release"]["result_schema_sha256"] = lock.provider_review_result_v3.file_sha256
    result["runtime_release"]["tool_registry_id"] = lock.provider_runtime_tool_registry_id
    _rehash_result(result)
    typed_result = AgentReviewResultV3.model_validate(result)
    claims = build_canonical_experience_claim_catalog(typed_result)
    sections = [
        section.model_dump(mode="json")
        for section in build_controlled_presentation_sections(claims)
    ]
    for section in sections:
        for statement in section["statements"]:
            statement["text"] = (
                "연결된 근거를 토대로 이번 심사의 의미를 쉽게 설명하면, "
                f"{statement['text']} "
                "따라서 담당자는 이 내용을 문서와 근거가 어떻게 연결되는지 이해하는 "
                "기준으로 확인할 수 있습니다."
            )
            statement_payload = {
                key: value for key, value in statement.items() if key != "statement_id"
            }
            statement["statement_id"] = f"statement_{canonical_digest(statement_payload)}"
    headline = {
        "no_issue_detected": "문서와 법령 근거 범위에서 별도의 문제를 확인하지 못했습니다",
        "issues_detected": "담당자가 확인하고 보완해야 할 사항이 발견되었습니다",
        "indeterminate": "현재 근거만으로 확정하기 어려워 담당자 확인이 필요합니다",
    }[result["assessment"]]
    verified_content_sha256 = canonical_digest(
        {
            "headline": headline,
            "sections": sections,
        }
    )
    verifier_context_sha256 = canonical_digest(
        {
            "canonical_claims": [item.model_dump(mode="json") for item in claims],
            "candidate_statements": [
                statement for section in sections for statement in section["statements"]
            ],
        }
    )
    presentation_payload = {
        "schema_version": "1.0",
        "headline": headline,
        "sections": sections,
        "generation_audit": {
            "mode": "claude",
            "task_id": "customs_experience_report",
            "model_id": result["runtime_release"]["model_id"],
            "prompt_sha256": lock.prompt_identities["presentation"],
            "output_schema_sha256": lock.output_schema_identities["presentation"],
            "input_tokens": 100,
            "output_tokens": 200,
            "latency_ms": 100,
            "attempt_count": 1,
            "scope_classification": None,
        },
        "verification_audit": {
            "status": "passed",
            "deterministic_checks_passed": True,
            "entailment_verifier": "claude",
            "verifier_model_id": result["runtime_release"]["model_id"],
            "verifier_prompt_sha256": lock.prompt_identities["entailment"],
            "verifier_schema_sha256": lock.output_schema_identities["entailment"],
            "verifier_context_sha256": verifier_context_sha256,
            "checked_statement_count": sum(len(section["statements"]) for section in sections),
            "verified_content_sha256": verified_content_sha256,
            "reason_codes": [],
        },
    }
    presentation = GroundedPresentationV1(
        **presentation_payload,
        presentation_sha256=canonical_digest(presentation_payload),
    )
    issued_at = datetime.now(UTC)
    issued_at_text = issued_at.isoformat().replace("+00:00", "Z")
    expires_at_text = (issued_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    capability_payload = {
        "schema_version": "1.0",
        "profile_id": "local-uat-capability",
        "environment": "local_uat",
        "evidence_profile": "preserved-3law-v3-scenario-eval",
        "loopback_only": True,
        "issued_at": issued_at_text,
        "expires_at": expires_at_text,
        "agent_commit": result["runtime_release"]["agent_git_sha"],
        "agent_code_snapshot_sha256": result["runtime_release"]["agent_code_snapshot_sha256"],
        "model_id": result["runtime_release"]["model_id"],
        "prompt_bundle_sha256": lock.experience_prompt_bundle_sha256,
        "schema_bundle_sha256": lock.experience_schema_bundle_sha256,
        "tool_registry_id": lock.uat_tool_registry_id,
        "tool_registry_sha256": lock.uat_tool_registry_sha256,
        "evidence_runtime_v3_sha256": _EVIDENCE_RUNTIME_SHA,
        "max_cycles": 2,
        "max_tool_calls": 6,
        "max_rereads_per_field": 1,
        "adaptive_active": True,
        "auto_completion_enabled": False,
    }
    capability = AgentUatCapabilityAttestationV1(
        **capability_payload,
        capability_sha256=canonical_digest(capability_payload),
    )
    meaning_catalog = provider_meaning_catalog()
    user_action_events = []
    for action in result["required_actions"]:
        action_code = action["action_code"]
        action_description = meaning_catalog.actions.get(action_code)
        if action_description is None and action_code.startswith("resolve_"):
            source_code = action_code.removeprefix("resolve_").upper()
            source_meaning = meaning_catalog.issues.get(
                source_code
            ) or meaning_catalog.findings.get(source_code)
            if source_meaning is not None:
                action_description = source_meaning.next_action
        if action_description is None:
            action_description = (
                "해석되지 않은 확인 항목이 있어 담당자가 연결된 근거를 직접 확인해야 합니다."
            )
        action_payload = {
            "schema_version": "1.0",
            "action_code": action_code,
            "title": "담당자 확인이 필요합니다",
            "description": action_description,
            "status": "needs_attention",
            "source_reference_ids": action["source_reference_ids"],
        }
        user_action_events.append(
            {
                "event_id": f"user_action_{canonical_digest(action_payload)}",
                **action_payload,
            }
        )
    explanation_action_payload = {
        "schema_version": "1.0",
        "action_code": "explanation.compose/v2",
        "title": "사용자용 심사 설명을 작성했습니다",
        "description": (
            "검증된 심사 사실과 근거만 사용해 사용자용 설명을 작성하고 "
            "근거 결박 검사를 완료했습니다."
        ),
        "status": "completed",
        "source_reference_ids": sorted(
            {
                statement["statement_id"]
                for section in presentation_payload["sections"]
                for statement in section["statements"]
            }
        )[:100],
    }
    user_action_events.append(
        {
            "event_id": f"user_action_{canonical_digest(explanation_action_payload)}",
            **explanation_action_payload,
        }
    )
    envelope_payload = {
        "schema_version": "1.0",
        "canonical_result": result,
        "user_presentation": presentation.model_dump(mode="json"),
        "capability_attestation": capability.model_dump(mode="json"),
        "user_action_events": user_action_events,
    }
    return ReviewExperienceEnvelopeV1(
        **envelope_payload,
        envelope_sha256=canonical_digest(envelope_payload),
    ).model_dump(mode="json")


def _answer(payload: dict[str, Any]) -> dict[str, Any]:
    lock = validate_agent_experience_contract_bundle().lock
    normalized_request = CaseConversationRequestV2.model_validate(payload).model_dump(mode="json")
    request_sha256 = canonical_digest(normalized_request)
    claims = build_answer_experience_claim_catalog(
        AgentReviewResultV3.model_validate(payload["review_result"])
    )
    direct_claim = claims[0]
    explanation_claims = [
        item
        for item in claims[1:]
        if item.role in {"conclusion", "observation", "reason", "limitation"}
    ][:5]
    evidence_claims = [item for item in claims if item.role == "evidence"][:5]
    impact_claims = [item for item in claims if item.role == "impact"][:5]
    action_claims = [item for item in claims if item.role == "action"][:5]
    selected_claims = [
        direct_claim,
        *explanation_claims,
        *evidence_claims,
        *impact_claims,
        *action_claims,
    ]
    citations = list(
        {
            (citation.kind, citation.reference_id): citation
            for claim in selected_claims
            for citation in claim.citation_references
        }.values()
    )
    answer_content = {
        "request_sha256": request_sha256,
        "answer_scope": "case_grounded",
        "status": "answered",
        "direct_answer": direct_claim.canonical_text,
        "explanation": [item.canonical_text for item in explanation_claims],
        "evidence_meaning": [item.canonical_text for item in evidence_claims],
        "impact": [item.canonical_text for item in impact_claims],
        "next_steps": [item.canonical_text for item in action_claims],
        "limitations": [_CASE_LIMITATION],
        "claim_selections": {
            "direct_answer": [direct_claim.claim_id],
            "explanation": [item.claim_id for item in explanation_claims],
            "evidence_meaning": [item.claim_id for item in evidence_claims],
            "impact": [item.claim_id for item in impact_claims],
            "next_steps": [item.claim_id for item in action_claims],
        },
        "citations": [item.model_dump(mode="json") for item in citations],
        "suggested_questions": _SAFE_SUGGESTED_QUESTIONS,
        "used_context_message_ids": [
            item["message_id"] for item in normalized_request["recent_turns"]
        ],
    }
    answer_payload = {
        "schema_version": "2.0",
        **answer_content,
        "generation_audit": {
            "mode": "claude",
            "task_id": "customs_case_answer_v2",
            "model_id": payload["review_result"]["runtime_release"]["model_id"],
            "prompt_sha256": lock.prompt_identities["chat"],
            "output_schema_sha256": lock.output_schema_identities["chat"],
            "input_tokens": 50,
            "output_tokens": 100,
            "latency_ms": 50,
            "attempt_count": 1,
            "scope_classification": {
                "mode": "claude",
                "task_id": "customs_case_scope_classifier",
                "classified_scope": "case_grounded",
                "model_id": payload["review_result"]["runtime_release"]["model_id"],
                "prompt_sha256": lock.prompt_identities["scope_classifier"],
                "output_schema_sha256": lock.output_schema_identities["scope_classifier"],
                "semantic_output_schema_sha256": (
                    lock.output_schema_identities["scope_classifier_semantic"]
                ),
                "input_tokens": 10,
                "output_tokens": 5,
                "latency_ms": 10,
                "reason_code": "case_result_question",
                "guidance_topics": [],
            },
        },
        "verification_audit": {
            "status": "passed",
            "deterministic_checks_passed": True,
            "entailment_verifier": "claude",
            "verifier_model_id": payload["review_result"]["runtime_release"]["model_id"],
            "verifier_prompt_sha256": lock.prompt_identities["chat_verifier"],
            "verifier_schema_sha256": lock.output_schema_identities["chat_verifier"],
            "verifier_context_sha256": canonical_digest(
                {
                    "question": normalized_request["question"],
                    "recent_turns": normalized_request["recent_turns"],
                    "answer_scope": "case_grounded",
                    "allowed_claims": [item.model_dump(mode="json") for item in claims[:100]],
                    "general_guidance": {},
                    "fixed_limitation_source": _CASE_LIMITATION,
                    "candidate_answer": {
                        "direct_answer": answer_content["direct_answer"],
                        "explanation": answer_content["explanation"],
                        "evidence_meaning": answer_content["evidence_meaning"],
                        "impact": answer_content["impact"],
                        "next_steps": answer_content["next_steps"],
                        "limitations": answer_content["limitations"],
                        "claim_selections": answer_content["claim_selections"],
                        "citation_references": answer_content["citations"],
                        "suggested_questions": answer_content["suggested_questions"],
                        "used_context_message_ids": answer_content["used_context_message_ids"],
                    },
                }
            ),
            "checked_statement_count": (
                1
                + len(answer_content["explanation"])
                + len(answer_content["evidence_meaning"])
                + len(answer_content["impact"])
                + len(answer_content["next_steps"])
                + len(answer_content["limitations"])
            ),
            "verified_content_sha256": canonical_digest(answer_content),
            "reason_codes": [],
        },
    }
    return AssistantAnswerV2(
        **answer_payload,
        answer_sha256=canonical_digest(answer_payload),
    ).model_dump(mode="json")


def _rehash_answer(
    raw: dict[str, Any],
    request_payload: dict[str, Any],
    *,
    refresh_verifier_context: bool = True,
) -> None:
    normalized_request = CaseConversationRequestV2.model_validate(request_payload).model_dump(
        mode="json"
    )
    answer_content_keys = (
        "request_sha256",
        "answer_scope",
        "status",
        "direct_answer",
        "explanation",
        "evidence_meaning",
        "impact",
        "next_steps",
        "limitations",
        "claim_selections",
        "citations",
        "suggested_questions",
        "used_context_message_ids",
    )
    answer_content = {key: raw[key] for key in answer_content_keys}
    raw["verification_audit"]["verified_content_sha256"] = canonical_digest(answer_content)
    if refresh_verifier_context and raw["verification_audit"]["entailment_verifier"] == "claude":
        result = AgentReviewResultV3.model_validate(request_payload["review_result"])
        claims = build_answer_experience_claim_catalog(result)
        scope = raw["answer_scope"]
        if scope == "evidence_grounded":
            claims = [item for item in claims if item.section_code == "regulatory_meaning"]
        elif scope != "case_grounded":
            claims = []
        guidance: dict[str, str] = {}
        if scope == "general_customs_guidance":
            catalog = provider_meaning_catalog()
            guidance = {
                topic: catalog.general_guidance[topic]
                for topic in raw["generation_audit"]["scope_classification"]["guidance_topics"]
            }
        raw["verification_audit"]["verifier_context_sha256"] = canonical_digest(
            {
                "question": normalized_request["question"],
                "recent_turns": normalized_request["recent_turns"],
                "answer_scope": scope,
                "allowed_claims": [item.model_dump(mode="json") for item in claims[:100]],
                "general_guidance": guidance,
                "fixed_limitation_source": (
                    _GENERAL_GUIDANCE_LIMITATION
                    if scope == "general_customs_guidance"
                    else _CASE_LIMITATION
                ),
                "candidate_answer": {
                    "direct_answer": raw["direct_answer"],
                    "explanation": raw["explanation"],
                    "evidence_meaning": raw["evidence_meaning"],
                    "impact": raw["impact"],
                    "next_steps": raw["next_steps"],
                    "limitations": raw["limitations"],
                    "claim_selections": raw["claim_selections"],
                    "citation_references": raw["citations"],
                    "suggested_questions": raw["suggested_questions"],
                    "used_context_message_ids": raw["used_context_message_ids"],
                },
            }
        )
    answer_payload = {key: value for key, value in raw.items() if key != "answer_sha256"}
    raw["answer_sha256"] = canonical_digest(answer_payload)


@pytest.fixture
def commercial_workbench():
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    observed_answer_requests: list[dict[str, Any]] = []

    def answer(payload: dict[str, Any]) -> dict[str, Any]:
        observed_answer_requests.append(payload)
        return _answer(payload)

    agent = CallableAgentClient(
        lambda request: _experience(request)["canonical_result"],
        review_experience=_experience,
        answer_v2=answer,
        review_events=_review_events(),
        answer_events=_answer_events(),
    )
    principal = Principal(
        subject="reviewer-1",
        org_id="org-a",
        roles=frozenset({Role.reviewer}),
    )
    other_principal = Principal(
        subject="reviewer-2",
        org_id="org-b",
        roles=frozenset({Role.reviewer}),
    )
    verifier = StaticTokenVerifier(
        {
            "reviewer-token": principal,
            "other-reviewer-token": other_principal,
        }
    )
    settings = Settings(
        environment="test",
        oidc_issuer="https://id.example.invalid/realms/customs",
        oidc_audience="customs-workbench-api",
        oidc_jwks_url="https://id.example.invalid/jwks",
        oidc_client_id="customs-workbench-spa",
        oidc_authorization_endpoint="https://id.example.invalid/auth",
        oidc_token_endpoint="https://id.example.invalid/token",
    )
    service = WorkbenchService(
        repository=repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
        auto_completion_enabled=False,
        enhanced_agent_experience=True,
        uat_adaptive_active=True,
        evidence_profile="preserved-3law-v3-scenario-eval",
        expected_evidence_runtime_v3_sha256=_EVIDENCE_RUNTIME_SHA,
    )
    container = Container(
        settings=settings,
        repository=repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
        token_verifier=verifier,
        service=service,
    )
    return (
        TestClient(create_app(container=container, settings=settings)),
        container,
        (observed_answer_requests),
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer reviewer-token"}


def _review_request() -> AgentReviewRequest:
    return AgentReviewRequest(
        review_run_id="run-experience-security",
        case_id="case-experience-security",
        documents=[
            AgentDocumentInput(
                document_id="document-experience-security",
                filename="invoice.txt",
                content_text="COMMERCIAL INVOICE",
            )
        ],
        review_as_of=date(2026, 7, 27),
        assertion_cutoff_at=datetime(2026, 7, 27, tzinfo=UTC),
        trade_type="import",
    )


def _create_case(client: TestClient) -> dict[str, Any]:
    case = client.post(
        "/api/v1/cases",
        json={"title": "상용형 사용자 UAT"},
        headers=_auth(),
    ).json()
    response = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "CI",
            "filename": "invoice.txt",
            "content_text": "INVOICE NO: INV-001\nHS CODE: 0303540000",
        },
        headers=_auth(),
    )
    assert response.status_code == 201, response.text
    return case


def test_stage_eta_uses_one_sample_per_successful_run_and_sums_stage_retries(
    commercial_workbench,
):
    client, container, _observed_answer_requests = commercial_workbench
    case = _create_case(client)
    principal = Principal(
        subject="system:test-history",
        org_id="org-a",
        roles=frozenset(),
    )
    retry_durations_ms = (990, 1_990, 2_990, 3_990, 4_990)
    last_job_id = ""

    for index, retry_duration_ms in enumerate(retry_durations_ms, start=1):
        queued = client.post(
            f"/api/v2/cases/{case['case_id']}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": f"stage-eta-history-{index}"},
        )
        assert queued.status_code == 202, queued.text
        last_job_id = queued.json()["job"]["job_id"]
        pending = container.repository.get_job("org-a", last_job_id)
        assert pending is not None
        sequence = len(container.repository.list_stage_events("org-a", last_job_id)) + 1
        container.repository.update_job(
            pending.model_copy(update={"attempt_count": 1}),
            StageEvent(
                event_id=f"event-stage-retry-{index}",
                job_id=last_job_id,
                sequence=sequence,
                stage="document_analysis",
                state="failed",
                message_code="review.document_analysis_retryable",
                occurred_at=utc_now(),
                detail={"attempt": 1, "duration_ms": retry_duration_ms},
            ),
            container.service._audit(
                principal,
                "review.stage_retry_recorded",
                case_id=case["case_id"],
                detail={"job_id": last_job_id, "attempt": 1},
            ),
        )
        container.service.execute_job(org_id="org-a", job_id=last_job_id)

    samples = container.repository.recent_review_stage_durations_ms(
        org_id="org-a",
        evidence_profile="preserved-3law-v3-scenario-eval",
        document_count=1,
        limit=100,
    )
    assert len(samples) == 5
    assert sorted(sample["document_analysis"] for sample in samples) == [
        1_000,
        2_000,
        3_000,
        4_000,
        5_000,
    ]
    assert sorted(
        container.repository.recent_review_durations_ms(
            org_id="org-a",
            evidence_profile="preserved-3law-v3-scenario-eval",
            document_count=1,
            limit=100,
        )
    ) == [1_100, 2_100, 3_100, 4_100, 5_100]
    assert (
        container.repository.recent_review_stage_durations_ms(
            org_id="org-a",
            evidence_profile="another-profile",
            document_count=1,
        )
        == []
    )
    assert (
        container.repository.recent_review_durations_ms(
            org_id="org-a",
            evidence_profile="another-profile",
            document_count=1,
        )
        == []
    )

    response = client.get(f"/api/v2/jobs/{last_job_id}", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    document_analysis = next(
        item for item in body["stage_eta"]["stages"] if item["stage_code"] == "document_analysis"
    )
    assert document_analysis == {
        "stage_code": "document_analysis",
        "source": "observed",
        "sample_count": 5,
        "p50_seconds": 3,
        "p90_seconds": 5,
    }
    request_validation = next(
        item for item in body["stage_eta"]["stages"] if item["stage_code"] == "request_validation"
    )
    assert request_validation == {
        "stage_code": "request_validation",
        "source": "observed",
        "sample_count": 5,
        "p50_seconds": 0,
        "p90_seconds": 0,
    }


def test_v2_review_experience_and_multiturn_assistant_are_end_to_end_bound(
    commercial_workbench,
):
    client, container, observed_answer_requests = commercial_workbench
    case = _create_case(client)
    queued = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "commercial-review-1"},
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()["job"]
    assert job["eta"]["source"] == "validated_default"
    assert (job["eta"]["range_min_seconds"], job["eta"]["range_max_seconds"]) == (
        240,
        420,
    )
    assert job["stage_eta"]["minimum_sample_count"] == 5
    assert job["stage_eta"]["insufficient_sample_behavior"] == "no_stage_estimate_use_overall_eta"
    assert len(job["stage_eta"]["stages"]) == 11
    assert {
        (stage["source"], stage["sample_count"], stage["p50_seconds"], stage["p90_seconds"])
        for stage in job["stage_eta"]["stages"]
    } == {("insufficient_samples", 0, None, None)}
    run = container.service.execute_job(org_id="org-a", job_id=job["job_id"])
    experience = client.get(
        f"/api/v2/review-runs/{run.review_run_id}/experience",
        headers=_auth(),
    )
    assert experience.status_code == 200, experience.text
    body = experience.json()
    assert len(body["user_presentation"]["sections"]) == 8
    assert body["bound_provider_result_sha256"] == run.provider_result_sha256
    assert body["bound_effective_result_digest"] == run.result_digest
    assert "canonical_result" not in body
    stages = container.repository.list_stage_events("org-a", job["job_id"])
    assert {event.stage for event in stages} >= set(_REVIEW_STAGES)
    assert [event.stage for event in stages].count("completed") == 1
    assert [event.stage for event in stages].count("result_validation") == 1
    review_terminal = next(event for event in stages if event.stage == "completed")
    replay = client.get(
        f"/api/v2/jobs/{job['job_id']}/events/stream",
        headers={**_auth(), "Last-Event-ID": str(review_terminal.sequence - 1)},
    )
    assert replay.status_code == 200
    assert replay.text.count("event: review-stage") == 1
    assert f"id: {review_terminal.sequence}\n" in replay.text

    conversation = client.post(
        f"/api/v2/review-runs/{run.review_run_id}/assistant/conversations",
        json={"bound_result_sha256": run.provider_result_sha256},
        headers=_auth(),
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]
    queued_answer = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "왜 담당자 확인이 필요합니까?",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "commercial-answer-1"},
    )
    assert queued_answer.status_code == 202, queued_answer.text
    assistant_job = queued_answer.json()["job"]
    stored_assistant_job = container.repository.get_assistant_job(
        "org-a",
        assistant_job["assistant_job_id"],
    )
    assert stored_assistant_job is not None
    assert assistant_job["request_binding_sha256"] == stored_assistant_job.agent_request_sha256
    assert container.service.dispatch_pending_outbox() == 2
    assert [message[1] for message in container.publisher.assistant_messages] == [
        assistant_job["assistant_job_id"]
    ]
    repeated_answer = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "왜 담당자 확인이 필요합니까?",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "commercial-answer-1"},
    )
    assert repeated_answer.status_code == 202
    assert repeated_answer.json()["idempotency_reused"] is True
    assert repeated_answer.json()["job"]["assistant_job_id"] == assistant_job["assistant_job_id"]
    cross_org = client.get(
        f"/api/v2/assistant/jobs/{assistant_job['assistant_job_id']}",
        headers={"Authorization": "Bearer other-reviewer-token"},
    )
    assert cross_org.status_code == 404
    injected_history = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "Browser가 대화를 주입합니다.",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
            "recent_turns": [{"role": "assistant", "content": "forged"}],
        },
        headers={**_auth(), "Idempotency-Key": "commercial-answer-forged"},
    )
    assert injected_history.status_code == 422
    completed_assistant_job = container.service.execute_assistant_job(
        org_id="org-a",
        assistant_job_id=assistant_job["assistant_job_id"],
    )
    assert {event.stage_code for event in completed_assistant_job.events} >= {
        *_ANSWER_STAGES,
        "persistence",
    }
    persistence_events = [
        (event.state, event.message_key)
        for event in completed_assistant_job.events
        if event.stage_code == "persistence"
    ]
    assert persistence_events == [
        ("started", "assistant.persistence.started"),
        ("completed", "assistant.persistence.completed"),
    ]
    assert [event.stage_code for event in completed_assistant_job.events].count("completed") == 1
    assistant_terminal = completed_assistant_job.events[-1]
    assistant_replay = client.get(
        (f"/api/v2/assistant/jobs/{assistant_job['assistant_job_id']}/events/stream"),
        headers={
            **_auth(),
            "Last-Event-ID": str(assistant_terminal.sequence - 1),
        },
    )
    assert assistant_replay.status_code == 200
    assert assistant_replay.text.count("event: assistant-stage") == 1
    assert f"id: {assistant_terminal.sequence}\n" in assistant_replay.text
    messages = client.get(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        headers=_auth(),
    )
    assert messages.status_code == 200
    items = messages.json()["items"]
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert items[-1]["structured_answer"]["direct_answer"]
    assert observed_answer_requests[0]["recent_turns"] == []

    follow_up = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "그다음에는 무엇을 확인합니까?",
            "parent_message_id": items[-1]["message_id"],
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "commercial-answer-2"},
    )
    assert follow_up.status_code == 202, follow_up.text
    container.service.execute_assistant_job(
        org_id="org-a",
        assistant_job_id=follow_up.json()["job"]["assistant_job_id"],
    )
    assert [item["role"] for item in observed_answer_requests[1]["recent_turns"]] == [
        "user",
        "assistant",
    ]
    restored_jobs = client.get(
        f"/api/v2/assistant/conversations/{conversation_id}/jobs",
        headers=_auth(),
    )
    assert restored_jobs.status_code == 200
    assert [item["state"] for item in restored_jobs.json()["items"]] == [
        "succeeded",
        "succeeded",
    ]


def test_assistant_failed_agent_terminal_is_persisted_as_response_rejected(
    commercial_workbench,
    monkeypatch,
) -> None:
    client, container, _observed = commercial_workbench
    case = _create_case(client)
    queued_review = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "failed-terminal-review"},
    ).json()["job"]
    run = container.service.execute_job(
        org_id="org-a",
        job_id=queued_review["job_id"],
    )
    conversation_id = client.post(
        f"/api/v2/review-runs/{run.review_run_id}/assistant/conversations",
        json={"bound_result_sha256": run.provider_result_sha256},
        headers=_auth(),
    ).json()["conversation_id"]
    assistant_job = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "현재 심사 결과를 설명해 주십시오.",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "failed-terminal-answer"},
    ).json()["job"]

    def failed_stream(
        _payload: dict[str, Any],
        *,
        on_event: Any,
    ) -> dict[str, Any]:
        on_event(
            _agent_event_with_state(
                operation_id="operation-assistant-provider-failed",
                sequence=0,
                stage_code="failed",
                state="failed",
            ).model_dump(mode="json")
        )
        raise AgentResponseRejectedError("Agent stream failed with internal_error")

    monkeypatch.setattr(container.agent, "answer_v2_stream", failed_stream)

    with pytest.raises(AgentResponseRejectedError, match="internal_error"):
        container.service.execute_assistant_job(
            org_id="org-a",
            assistant_job_id=assistant_job["assistant_job_id"],
        )

    rejected = container.repository.get_assistant_job(
        "org-a",
        assistant_job["assistant_job_id"],
    )
    assert rejected is not None
    assert rejected.state.value == "response_received_rejected"
    assert rejected.events[-1].stage_code == "failed"
    assert rejected.events[-1].message_key == "assistant.response_received_rejected"


def test_presentation_text_cannot_reuse_a_stale_verifier_digest() -> None:
    raw = deepcopy(_experience(_review_request()))
    statement = raw["user_presentation"]["sections"][0]["statements"][0]
    statement["text"] = "기존 결론과 반대되는 설명으로 바꿨습니다."
    statement_payload = {key: value for key, value in statement.items() if key != "statement_id"}
    statement["statement_id"] = f"statement_{canonical_digest(statement_payload)}"
    presentation_payload = {
        key: value
        for key, value in raw["user_presentation"].items()
        if key != "presentation_sha256"
    }
    raw["user_presentation"]["presentation_sha256"] = canonical_digest(presentation_payload)
    envelope_payload = {key: value for key, value in raw.items() if key != "envelope_sha256"}
    raw["envelope_sha256"] = canonical_digest(envelope_payload)
    typed = ReviewExperienceEnvelopeV1.model_validate(raw)
    with pytest.raises(ValueError, match="verification identity"):
        validate_presentation_against_result(typed)


def test_presentation_action_event_is_exactly_bound_to_current_statement_ids() -> None:
    raw = deepcopy(_experience(_review_request()))
    expected_statement_ids = sorted(
        {
            statement["statement_id"]
            for section in raw["user_presentation"]["sections"]
            for statement in section["statements"]
        }
    )[:100]
    explanation_event = raw["user_action_events"][-1]
    assert explanation_event["action_code"] == "explanation.compose/v2"
    assert explanation_event["source_reference_ids"] == expected_statement_ids
    validate_presentation_against_result(ReviewExperienceEnvelopeV1.model_validate(raw))

    explanation_event["source_reference_ids"] = expected_statement_ids[:-1]
    event_payload = {key: value for key, value in explanation_event.items() if key != "event_id"}
    explanation_event["event_id"] = f"user_action_{canonical_digest(event_payload)}"
    envelope_payload = {key: value for key, value in raw.items() if key != "envelope_sha256"}
    raw["envelope_sha256"] = canonical_digest(envelope_payload)
    with pytest.raises(ValueError, match="user action copy"):
        validate_presentation_against_result(ReviewExperienceEnvelopeV1.model_validate(raw))


def test_experience_pins_distinct_production_and_uat_tool_registries() -> None:
    envelope = ReviewExperienceEnvelopeV1.model_validate(_experience(_review_request()))
    lock = validate_agent_experience_contract_bundle().lock

    assert (
        envelope.canonical_result.runtime_release.tool_registry_id
        == lock.provider_runtime_tool_registry_id
    )
    assert envelope.capability_attestation.tool_registry_id == lock.uat_tool_registry_id
    assert (
        envelope.canonical_result.runtime_release.tool_registry_id
        != envelope.capability_attestation.tool_registry_id
    )
    validate_presentation_against_result(envelope)


@pytest.mark.parametrize(
    "unsupported_text",
    [
        "현재 확인 결과 이 건의 관세율은 50%이며 3일 이내 제출해야 합니다.",
        "자세한 판단 기준은 https://example.invalid/customs 에서 확인할 수 있습니다.",
        "이 심사에는 관세법 999조가 직접 적용되는 것으로 확인했습니다.",
        "<strong>제출 문서는 모두 안전합니다.</strong>",
        "내부 근거는 experience_claim_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 입니다.",
    ],
)
def test_presentation_rejects_unsupported_factual_content_even_with_fresh_audit(
    unsupported_text: str,
) -> None:
    raw = deepcopy(_experience(_review_request()))
    statement = raw["user_presentation"]["sections"][0]["statements"][0]
    statement["text"] = unsupported_text
    statement_payload = {key: value for key, value in statement.items() if key != "statement_id"}
    statement["statement_id"] = f"statement_{canonical_digest(statement_payload)}"
    canonical_claims = build_canonical_experience_claim_catalog(
        AgentReviewResultV3.model_validate(raw["canonical_result"])
    )
    raw["user_presentation"]["verification_audit"]["verifier_context_sha256"] = canonical_digest(
        {
            "canonical_claims": [item.model_dump(mode="json") for item in canonical_claims],
            "candidate_statements": [
                item
                for section in raw["user_presentation"]["sections"]
                for item in section["statements"]
            ],
        }
    )
    raw["user_presentation"]["verification_audit"]["verified_content_sha256"] = canonical_digest(
        {
            "headline": raw["user_presentation"]["headline"],
            "sections": raw["user_presentation"]["sections"],
        }
    )
    presentation_payload = {
        key: value
        for key, value in raw["user_presentation"].items()
        if key != "presentation_sha256"
    }
    raw["user_presentation"]["presentation_sha256"] = canonical_digest(presentation_payload)
    envelope_payload = {key: value for key, value in raw.items() if key != "envelope_sha256"}
    raw["envelope_sha256"] = canonical_digest(envelope_payload)

    typed = ReviewExperienceEnvelopeV1.model_validate(raw)
    with pytest.raises(
        ValueError,
        match="deterministic grounding",
    ):
        validate_presentation_against_result(typed)


@pytest.mark.parametrize(
    "unsupported_text",
    [
        "현재 심사 결과 이 건의 관세율은 50%이며 3일 이내 제출해야 합니다.",
        "자세한 판단 기준은 https://example.invalid/customs 에서 확인할 수 있습니다.",
        "이 심사에는 관세법 999조가 직접 적용되는 것으로 확인했습니다.",
        "<strong>제출 문서는 모두 안전합니다.</strong>",
        "내부 근거는 experience_claim_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 입니다.",
    ],
)
def test_assistant_rejects_unsupported_factual_content_even_with_fresh_audit(
    unsupported_text: str,
) -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-grounding-attack",
        "parent_message_id": None,
        "question": "현재 심사 결과를 설명해 주십시오.",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    raw["direct_answer"] = unsupported_text
    _rehash_answer(raw, request)

    typed = AssistantAnswerV2.model_validate(raw)
    with pytest.raises(
        ValueError,
        match="deterministic grounding",
    ):
        validate_answer_against_context(
            answer=typed,
            request=CaseConversationRequestV2.model_validate(request),
        )


def test_assistant_requires_each_canonical_claims_own_citation() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-citation-binding",
        "parent_message_id": None,
        "question": "품목번호를 설명해 주십시오.",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    claims = build_answer_experience_claim_catalog(AgentReviewResultV3.model_validate(result))
    field_claim = next(
        claim
        for claim in claims
        if any(citation.kind == "field_source" for citation in claim.citation_references)
    )
    outcome = next(item for item in result["fact_ledger"]["entries"] if item["kind"] == "outcome")
    raw["direct_answer"] = field_claim.canonical_text
    raw["explanation"] = []
    raw["evidence_meaning"] = []
    raw["impact"] = []
    raw["next_steps"] = []
    raw["claim_selections"] = {
        "direct_answer": [field_claim.claim_id],
        "explanation": [],
        "evidence_meaning": [],
        "impact": [],
        "next_steps": [],
    }
    raw["citations"] = [{"kind": "outcome", "reference_id": outcome["reference_id"]}]
    raw["verification_audit"]["checked_statement_count"] = 2
    _rehash_answer(raw, request)

    typed = AssistantAnswerV2.model_validate(raw)
    with pytest.raises(ValueError, match="citation binding"):
        validate_answer_against_context(
            answer=typed,
            request=CaseConversationRequestV2.model_validate(request),
        )


def test_assistant_accepts_code_owned_korean_frame_for_exact_field_fact() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-field-frame",
        "parent_message_id": None,
        "question": "송장번호는 무엇입니까?",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    field_claim = next(
        claim
        for claim in build_answer_experience_claim_catalog(
            AgentReviewResultV3.model_validate(result)
        )
        if "송장번호" in claim.canonical_text
    )
    raw["direct_answer"] = f"질문에 바로 답변드리면, {field_claim.canonical_text}"
    raw["explanation"] = []
    raw["evidence_meaning"] = []
    raw["impact"] = []
    raw["next_steps"] = []
    raw["claim_selections"] = {
        "direct_answer": [field_claim.claim_id],
        "explanation": [],
        "evidence_meaning": [],
        "impact": [],
        "next_steps": [],
    }
    raw["citations"] = [item.model_dump(mode="json") for item in field_claim.citation_references]
    raw["verification_audit"]["checked_statement_count"] = 2
    _rehash_answer(raw, request)

    validate_answer_against_context(
        answer=AssistantAnswerV2.model_validate(raw),
        request=CaseConversationRequestV2.model_validate(request),
    )


def test_assistant_accepts_natural_prose_when_claims_citations_and_verifier_are_bound() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-fluent-answer",
        "parent_message_id": None,
        "question": "현재 심사 결과를 이해하기 쉽게 설명해 주십시오.",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    raw["direct_answer"] = "질문하신 결과를 이해하기 쉽게 설명하면, " + raw["direct_answer"]
    raw["limitations"] = ["안내드린 답변의 적용 범위를 함께 설명하면, " + _CASE_LIMITATION]
    _rehash_answer(raw, request)

    validate_answer_against_context(
        answer=AssistantAnswerV2.model_validate(raw),
        request=CaseConversationRequestV2.model_validate(request),
    )


def test_general_guidance_rejects_case_outcome_prose_with_fresh_hashes() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-guidance-grounding-attack",
        "parent_message_id": None,
        "question": "HSK가 무엇입니까?",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    raw["answer_scope"] = "general_customs_guidance"
    raw["status"] = "answered"
    raw["direct_answer"] = (
        "현재 심사 건의 판정은 승인되었으므로 담당자 확인 없이 처리할 수 있습니다."
    )
    raw["explanation"] = []
    raw["evidence_meaning"] = []
    raw["impact"] = []
    raw["next_steps"] = []
    raw["limitations"] = [_GENERAL_GUIDANCE_LIMITATION]
    raw["claim_selections"] = {
        "direct_answer": [],
        "explanation": [],
        "evidence_meaning": [],
        "impact": [],
        "next_steps": [],
    }
    raw["citations"] = []
    raw["generation_audit"]["scope_classification"]["classified_scope"] = "general_customs_guidance"
    raw["generation_audit"]["scope_classification"]["guidance_topics"] = ["hsk"]
    raw["verification_audit"]["checked_statement_count"] = 2
    _rehash_answer(raw, request)

    with pytest.raises(ValueError, match="deterministic grounding"):
        validate_answer_against_context(
            answer=AssistantAnswerV2.model_validate(raw),
            request=CaseConversationRequestV2.model_validate(request),
        )


def test_general_guidance_accepts_the_selected_catalog_content() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-guidance-controlled-language",
        "parent_message_id": None,
        "question": "HSK가 무엇입니까?",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    raw["answer_scope"] = "general_customs_guidance"
    raw["status"] = "answered"
    raw["direct_answer"] = (
        "질문하신 용어를 일반 통관 절차의 관점에서 설명하면, "
        + provider_meaning_catalog().general_guidance["hsk"]
    )
    raw["explanation"] = []
    raw["evidence_meaning"] = []
    raw["impact"] = []
    raw["next_steps"] = []
    raw["limitations"] = [
        "이 안내가 현재 심사 건의 결론과 구분된다는 점을 덧붙이면, " + _GENERAL_GUIDANCE_LIMITATION
    ]
    raw["claim_selections"] = {
        "direct_answer": [],
        "explanation": [],
        "evidence_meaning": [],
        "impact": [],
        "next_steps": [],
    }
    raw["citations"] = []
    raw["generation_audit"]["scope_classification"]["classified_scope"] = "general_customs_guidance"
    raw["generation_audit"]["scope_classification"]["guidance_topics"] = ["hsk"]
    raw["verification_audit"]["checked_statement_count"] = 2
    _rehash_answer(raw, request)

    validate_answer_against_context(
        answer=AssistantAnswerV2.model_validate(raw),
        request=CaseConversationRequestV2.model_validate(request),
    )


def test_general_guidance_accepts_reordered_grounded_topics_with_fresh_hashes() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-guidance-reordered",
        "parent_message_id": None,
        "question": "HSK와 상업송장을 함께 설명해 주십시오.",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    catalog = provider_meaning_catalog().general_guidance
    raw = _answer(request)
    raw["answer_scope"] = "general_customs_guidance"
    raw["status"] = "answered"
    raw["direct_answer"] = catalog["invoice"]
    raw["explanation"] = [catalog["hsk"]]
    raw["evidence_meaning"] = []
    raw["impact"] = []
    raw["next_steps"] = []
    raw["limitations"] = [_GENERAL_GUIDANCE_LIMITATION]
    raw["claim_selections"] = {
        "direct_answer": [],
        "explanation": [],
        "evidence_meaning": [],
        "impact": [],
        "next_steps": [],
    }
    raw["citations"] = []
    raw["generation_audit"]["scope_classification"]["classified_scope"] = "general_customs_guidance"
    raw["generation_audit"]["scope_classification"]["guidance_topics"] = [
        "hsk",
        "invoice",
    ]
    raw["verification_audit"]["checked_statement_count"] = 3
    _rehash_answer(raw, request)

    validate_answer_against_context(
        answer=AssistantAnswerV2.model_validate(raw),
        request=CaseConversationRequestV2.model_validate(request),
    )


def test_assistant_rejects_a_verifier_receipt_from_another_question_context() -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-verifier-replay",
        "parent_message_id": None,
        "question": "현재 심사 결과를 설명해 주십시오.",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    raw["verification_audit"]["verifier_context_sha256"] = "f" * 64
    _rehash_answer(raw, request, refresh_verifier_context=False)

    with pytest.raises(ValueError, match="Claude assistant answer audit"):
        validate_answer_against_context(
            answer=AssistantAnswerV2.model_validate(raw),
            request=CaseConversationRequestV2.model_validate(request),
        )


@pytest.mark.parametrize(
    "secret_uri",
    [
        "https://example.invalid/law/article?token=private-value#credential",
        "https://example.invalid/law/article?serviceKey=private-value",
        "https://example.invalid/law/article?foo=Bearer%20private-value",
        "https://example.invalid/law/article#%74oken=private-value",
        "https://example.invalid/law/token=private-value/article",
    ],
)
def test_review_rejects_provider_uri_credentials_before_persistence(
    commercial_workbench,
    secret_uri: str,
) -> None:
    client, container, _observed = commercial_workbench
    case = _create_case(client)
    original = container.agent._review_experience

    def experience_with_private_uri(request: AgentReviewRequest) -> dict[str, Any]:
        assert original is not None
        envelope = original(request)
        result = envelope["canonical_result"]
        result["retrieval_results"][0]["evidence"][0]["source_uri"] = secret_uri
        result["rag_evidence"][0]["source_uri"] = secret_uri
        result["report"]["rag_evidence"][0]["source_uri"] = secret_uri
        _rehash_result(result)
        envelope["envelope_sha256"] = "0" * 64
        envelope["envelope_sha256"] = canonical_digest(
            {key: value for key, value in envelope.items() if key != "envelope_sha256"}
        )
        return envelope

    container.agent._review_experience = experience_with_private_uri
    queued = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "private-provider-uri"},
    ).json()["job"]
    with pytest.raises(ConflictError, match="credential material"):
        container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    rejected = container.repository.get_job("org-a", queued["job_id"])
    assert rejected is not None
    assert rejected.status.value == "response_received_rejected"
    assert container.repository.get_run("org-a", queued["review_run_id"]) is None
    assert container.repository.get_provider_result("org-a", queued["review_run_id"]) is None
    assert container.repository.get_review_presentation("org-a", queued["review_run_id"]) is None


def test_assistant_answer_cannot_be_replayed_for_another_question() -> None:
    result = _experience(_review_request())["canonical_result"]
    payload = {
        "conversation_id": "conversation-replay",
        "parent_message_id": None,
        "question": "이 심사 결과의 이유는 무엇입니까?",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    answer = AssistantAnswerV2.model_validate(_answer(payload))
    with pytest.raises(ValueError, match="request binding"):
        validate_answer_against_context(
            answer=answer,
            request=CaseConversationRequestV2.model_validate(
                {**payload, "question": "다른 질문입니다."}
            ),
        )


@pytest.mark.parametrize(
    ("answer_scope", "contradictory_status"),
    [
        ("case_grounded", "insufficient_evidence"),
        ("evidence_grounded", "clarification_needed"),
        ("general_customs_guidance", "out_of_scope"),
        ("clarification_needed", "out_of_scope"),
        ("insufficient_evidence", "answered"),
        ("out_of_scope", "clarification_needed"),
    ],
)
def test_assistant_scope_requires_one_exact_status(
    answer_scope: str,
    contradictory_status: str,
) -> None:
    result = _experience(_review_request())["canonical_result"]
    request = {
        "conversation_id": "conversation-scope-status",
        "parent_message_id": None,
        "question": "현재 심사 결과를 설명해 주십시오.",
        "bound_result_sha256": result["result_sha256"],
        "conversation_context_sha256": canonical_digest([]),
        "recent_turns": [],
        "review_result": result,
        "locale": "ko-KR",
    }
    raw = _answer(request)
    raw["answer_scope"] = answer_scope
    raw["status"] = contradictory_status
    raw["generation_audit"]["scope_classification"]["classified_scope"] = answer_scope
    raw["generation_audit"]["scope_classification"]["guidance_topics"] = (
        ["hsk"] if answer_scope == "general_customs_guidance" else []
    )
    raw["answer_sha256"] = canonical_digest(
        {key: value for key, value in raw.items() if key != "answer_sha256"}
    )
    with pytest.raises(ValueError, match="status is inconsistent"):
        AssistantAnswerV2.model_validate(raw)


def test_public_evidence_uri_keeps_benign_provenance_private() -> None:
    assert (
        service_module._safe_public_evidence_uri(
            "https://apis.data.go.kr/example?hsSgn=0303540000&imexTpcd=2"
        )
        == "https://apis.data.go.kr/example"
    )


@pytest.mark.parametrize(
    "mutator",
    [
        "duplicate",
        "out_of_order",
        "failed",
    ],
)
def test_agent_stream_rejects_duplicate_out_of_order_and_failed_stages(
    mutator: str,
) -> None:
    stream = _OrderedAgentStageStream(stages=("request_validation", "document_analysis"))
    first = AgentReviewStageEventV1.model_validate(
        _agent_event(
            operation_id="operation-adversarial",
            sequence=0,
            stage_code="request_validation",
            message_key="agent.stage.request_validation.completed",
        )
    )
    stream.observe(first)
    if mutator == "duplicate":
        event = first
    elif mutator == "out_of_order":
        event = AgentReviewStageEventV1.model_validate(
            _agent_event(
                operation_id="operation-adversarial",
                sequence=1,
                stage_code="completed",
                message_key="agent.stage.completed.completed",
            )
        )
    else:
        raw = _agent_event(
            operation_id="operation-adversarial",
            sequence=1,
            stage_code="document_analysis",
            message_key="agent.stage.document_analysis.completed",
        )
        raw["state"] = "failed"
        raw["message_key"] = "agent.stage.document_analysis.failed"
        raw["event_sha256"] = canonical_digest(
            {key: value for key, value in raw.items() if key != "event_sha256"}
        )
        event = AgentReviewStageEventV1.model_validate(raw)
    with pytest.raises(ConflictError):
        stream.observe(event)


@pytest.mark.parametrize(
    ("retry_group", "events"),
    [
        (
            ("report_composition", "grounding_verification"),
            [
                ("request_validation", "completed"),
                ("report_composition", "started"),
                ("report_composition", "completed"),
                ("report_composition", "started"),
                ("report_composition", "completed"),
                ("grounding_verification", "started"),
                ("grounding_verification", "failed"),
                ("report_composition", "started"),
                ("report_composition", "completed"),
                ("grounding_verification", "started"),
                ("grounding_verification", "completed"),
                ("result_validation", "completed"),
                ("completed", "completed"),
            ],
        ),
        (
            ("answer_composition", "grounding_verification"),
            [
                ("request_validation", "completed"),
                ("answer_composition", "started"),
                ("answer_composition", "completed"),
                ("answer_composition", "started"),
                ("answer_composition", "completed"),
                ("grounding_verification", "started"),
                ("grounding_verification", "completed"),
                ("result_validation", "completed"),
                ("completed", "completed"),
            ],
        ),
    ],
)
def test_agent_stream_accepts_bounded_verified_generation_retries(
    retry_group: tuple[str, str],
    events: list[tuple[str, str]],
) -> None:
    stages = ("request_validation", *retry_group, "result_validation")
    stream = _OrderedAgentStageStream(
        stages=stages,
        retry_groups=(retry_group,),
    )
    operation_id = "operation-bounded-retry"
    for sequence, (stage_code, state) in enumerate(events):
        stream.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=sequence,
                stage_code=stage_code,
                state=state,
            )
        )

    stream.require_success()


def test_agent_stream_rejects_a_fourth_generation_attempt_and_duplicate_terminal() -> None:
    stream = _OrderedAgentStageStream(
        stages=(
            "request_validation",
            "report_composition",
            "grounding_verification",
            "result_validation",
        ),
        retry_groups=(("report_composition", "grounding_verification"),),
    )
    operation_id = "operation-retry-limit"
    sequence = 0
    stream.observe(
        _agent_event_with_state(
            operation_id=operation_id,
            sequence=sequence,
            stage_code="request_validation",
            state="completed",
        )
    )
    sequence += 1
    for _attempt in range(3):
        for state in ("started", "completed"):
            stream.observe(
                _agent_event_with_state(
                    operation_id=operation_id,
                    sequence=sequence,
                    stage_code="report_composition",
                    state=state,
                )
            )
            sequence += 1
    with pytest.raises(ConflictError, match="retry count"):
        stream.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=sequence,
                stage_code="report_composition",
                state="started",
            )
        )

    successful = _OrderedAgentStageStream(stages=("request_validation",))
    successful.observe(
        _agent_event_with_state(
            operation_id=operation_id,
            sequence=0,
            stage_code="request_validation",
            state="completed",
        )
    )
    terminal = _agent_event_with_state(
        operation_id=operation_id,
        sequence=1,
        stage_code="completed",
        state="completed",
    )
    successful.observe(terminal)
    with pytest.raises(ConflictError, match="followed its terminal"):
        successful.observe(terminal)


@pytest.mark.parametrize("grounding_state", ["completed", "failed"])
def test_agent_stream_requires_regeneration_to_restart_at_composition(
    grounding_state: str,
) -> None:
    stream = _OrderedAgentStageStream(
        stages=(
            "request_validation",
            "answer_composition",
            "grounding_verification",
            "result_validation",
        ),
        retry_groups=(("answer_composition", "grounding_verification"),),
    )
    operation_id = "operation-no-grounding-only-retry"
    sequence = 0
    for stage_code, state in [
        ("request_validation", "completed"),
        ("answer_composition", "started"),
        ("answer_composition", "completed"),
        ("grounding_verification", "started"),
        ("grounding_verification", grounding_state),
    ]:
        stream.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=sequence,
                stage_code=stage_code,
                state=state,
            )
        )
        sequence += 1

    with pytest.raises(ConflictError, match="stage order"):
        stream.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=sequence,
                stage_code="grounding_verification",
                state="started",
            )
        )


def test_agent_stream_accepts_one_failed_terminal_but_never_reports_success() -> None:
    stream = _OrderedAgentStageStream(stages=("request_validation", "document_analysis"))
    failed = _agent_event_with_state(
        operation_id="operation-terminal-failure",
        sequence=0,
        stage_code="failed",
        state="failed",
    )

    stream.observe(failed)

    with pytest.raises(ConflictError, match="omitted an ordered stage"):
        stream.require_success()
    with pytest.raises(ConflictError, match="followed its terminal"):
        stream.observe(failed)


def test_agent_stream_accepts_failed_stage_only_when_followed_by_failed_terminal() -> None:
    stream = _OrderedAgentStageStream(stages=("request_validation", "document_analysis"))
    operation_id = "operation-stage-then-terminal-failure"
    events = [
        ("request_validation", "completed"),
        ("document_analysis", "started"),
        ("document_analysis", "failed"),
        ("failed", "failed"),
    ]
    for sequence, (stage_code, state) in enumerate(events):
        stream.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=sequence,
                stage_code=stage_code,
                state=state,
            )
        )

    with pytest.raises(ConflictError, match="omitted an ordered stage"):
        stream.require_success()

    interrupted = _OrderedAgentStageStream(stages=("request_validation", "document_analysis"))
    for sequence, (stage_code, state) in enumerate(events[:-1]):
        interrupted.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=sequence,
                stage_code=stage_code,
                state=state,
            )
        )
    with pytest.raises(ConflictError, match="unrecoverable stage failure"):
        interrupted.observe(
            _agent_event_with_state(
                operation_id=operation_id,
                sequence=len(events) - 1,
                stage_code="document_analysis",
                state="started",
            )
        )


@pytest.mark.parametrize("injection_target", ["message_key", "safe_counts"])
def test_agent_stage_public_metadata_rejects_untrusted_text(
    injection_target: str,
) -> None:
    raw = _agent_event(
        operation_id="operation-public-metadata",
        sequence=0,
        stage_code="request_validation",
        message_key="agent.stage.request_validation.completed",
    )
    if injection_target == "message_key":
        raw["message_key"] = "agent.stage.prompt-secret.completed"
    else:
        raw["safe_counts"] = {"document_text_SECRET": 1}
    raw["event_sha256"] = canonical_digest(
        {key: value for key, value in raw.items() if key != "event_sha256"}
    )
    with pytest.raises(ValueError):
        AgentReviewStageEventV1.model_validate(raw)


def test_agent_stage_public_metadata_accepts_bounded_action_count() -> None:
    raw = _agent_event(
        operation_id="operation-public-action-count",
        sequence=0,
        stage_code="adaptive_investigation",
    )
    raw["safe_counts"] = {
        "actions": 6,
        "evidence": 1,
        "findings": 0,
        "issues": 2,
    }
    raw["event_sha256"] = canonical_digest(
        {key: value for key, value in raw.items() if key != "event_sha256"}
    )

    event = AgentReviewStageEventV1.model_validate(raw)

    assert event.safe_counts["actions"] == 6


def test_expired_assistant_lease_is_reconciled_without_a_second_agent_call(
    commercial_workbench,
    monkeypatch,
) -> None:
    client, container, _observed = commercial_workbench
    case = _create_case(client)
    review_job = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "lease-review"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=review_job["job_id"])
    conversation_id = client.post(
        f"/api/v2/review-runs/{run.review_run_id}/assistant/conversations",
        json={"bound_result_sha256": run.provider_result_sha256},
        headers=_auth(),
    ).json()["conversation_id"]
    assistant_job = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "왜 사람 검토가 필요합니까?",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "lease-answer"},
    ).json()["job"]
    calls = 0

    def crash_after_call_started(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt

    container.agent._answer_v2 = crash_after_call_started
    with pytest.raises(KeyboardInterrupt):
        container.service.execute_assistant_job(
            org_id="org-a",
            assistant_job_id=assistant_job["assistant_job_id"],
        )
    running = container.repository.get_assistant_job("org-a", assistant_job["assistant_job_id"])
    assert running is not None and running.lease_expires_at is not None
    monkeypatch.setattr(
        service_module,
        "utc_now",
        lambda: running.lease_expires_at + timedelta(seconds=1),
    )
    restored = client.get(
        f"/api/v2/assistant/conversations/{conversation_id}/jobs",
        headers=_auth(),
    )
    assert restored.status_code == 200
    assert restored.json()["items"][-1]["state"] == "delivery_uncertain"
    assert restored.json()["items"][-1]["request_binding_sha256"] == running.agent_request_sha256
    with pytest.raises(JobExecutionNotRunnableError):
        container.service.execute_assistant_job(
            org_id="org-a",
            assistant_job_id=assistant_job["assistant_job_id"],
        )
    assert calls == 1


def test_expired_uat_capability_blocks_assistant_before_agent_call(
    commercial_workbench,
    monkeypatch,
) -> None:
    client, container, observed = commercial_workbench
    case = _create_case(client)
    review_job = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "capability-expiry-review"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=review_job["job_id"])
    presentation = container.repository.get_review_presentation(
        "org-a",
        run.review_run_id,
    )
    assert presentation is not None
    conversation_id = client.post(
        f"/api/v2/review-runs/{run.review_run_id}/assistant/conversations",
        json={"bound_result_sha256": run.provider_result_sha256},
        headers=_auth(),
    ).json()["conversation_id"]
    assistant_job = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "현재 결과를 설명해 주십시오.",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "capability-expiry-answer"},
    ).json()["job"]
    monkeypatch.setattr(
        service_module,
        "utc_now",
        lambda: presentation.envelope.capability_attestation.expires_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="capability is expired"):
        container.service.execute_assistant_job(
            org_id="org-a",
            assistant_job_id=assistant_job["assistant_job_id"],
        )
    assert observed == []
    rejected = container.repository.get_assistant_job(
        "org-a",
        assistant_job["assistant_job_id"],
    )
    assert rejected is not None
    assert rejected.state.value == "failed"


def test_case_drift_keeps_the_review_but_omits_a_contradictory_presentation(
    commercial_workbench,
) -> None:
    client, container, _observed = commercial_workbench
    case = _create_case(client)
    original = container.agent._review_experience

    def drift_after_agent_response(request: AgentReviewRequest) -> dict[str, Any]:
        assert original is not None
        response = original(request)
        current = container.repository._cases[request.case_id]
        container.repository._cases[request.case_id] = current.model_copy(
            update={"revision": current.revision + 1, "updated_at": datetime.now(UTC)}
        )
        return response

    container.agent._review_experience = drift_after_agent_response
    review_job = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "case-drift-review"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=review_job["job_id"])
    assert "case_revision_drift" in run.completion_eligibility.unmet_reason_codes
    assert container.repository.get_job("org-a", review_job["job_id"]).status.value == "succeeded"
    experience = client.get(
        f"/api/v2/review-runs/{run.review_run_id}/experience",
        headers=_auth(),
    )
    assert experience.status_code == 404
    conversation = client.post(
        f"/api/v2/review-runs/{run.review_run_id}/assistant/conversations",
        json={"bound_result_sha256": run.provider_result_sha256},
        headers=_auth(),
    )
    assert conversation.status_code == 409


def test_task0027_downgrade_has_a_race_free_drain_guard() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "20260727_0008_commercial_agent_experience.py"
    ).read_text(encoding="utf-8")
    assert "LOCK TABLE operations.product_jobs IN ACCESS EXCLUSIVE MODE" in migration
    assert "LOCK TABLE operations.review_presentations IN ACCESS EXCLUSIVE MODE" in migration
    assert "LOCK TABLE operations.assistant_conversations IN ACCESS EXCLUSIVE MODE" in migration
    assert "LOCK TABLE operations.assistant_jobs IN ACCESS EXCLUSIVE MODE" in migration
    assert "LOCK TABLE operations.assistant_messages IN ACCESS EXCLUSIVE MODE" in migration
    assert "record_json->'request'->>'experience_api_version' = '3.0'" in migration
    assert "FROM operations.assistant_jobs" in migration
    for state in (
        "queued",
        "running",
        "delivery_uncertain",
        "response_persistence_uncertain",
    ):
        assert f"'{state}'" in migration
    assert "agent_call_endpoint = 'review'" in migration


def test_v2_assistant_rejects_stale_parent_and_cross_result_binding(
    commercial_workbench,
):
    client, container, _observed = commercial_workbench
    case = _create_case(client)
    queued = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "commercial-review-stale"},
    ).json()["job"]
    run = container.service.execute_job(org_id="org-a", job_id=queued["job_id"])
    conversation_id = client.post(
        f"/api/v2/review-runs/{run.review_run_id}/assistant/conversations",
        json={"bound_result_sha256": run.provider_result_sha256},
        headers=_auth(),
    ).json()["conversation_id"]
    for payload in (
        {
            "question": "잘못된 결과 결박",
            "parent_message_id": None,
            "bound_result_sha256": "0" * 64,
        },
        {
            "question": "잘못된 부모",
            "parent_message_id": "assistant_message_" + "f" * 64,
            "bound_result_sha256": run.provider_result_sha256,
        },
    ):
        response = client.post(
            f"/api/v2/assistant/conversations/{conversation_id}/messages",
            json=payload,
            headers={**_auth(), "Idempotency-Key": canonical_digest(payload)[:16]},
        )
        assert response.status_code == 409

    changed = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "PL",
            "filename": "packing-list.txt",
            "content_text": "PACKING LIST\nHS CODE: 0303540000",
        },
        headers=_auth(),
    )
    assert changed.status_code == 201
    stale_conversation = client.post(
        f"/api/v2/assistant/conversations/{conversation_id}/messages",
        json={
            "question": "변경 전 심사 결과를 계속 사용해도 됩니까?",
            "parent_message_id": None,
            "bound_result_sha256": run.provider_result_sha256,
        },
        headers={**_auth(), "Idempotency-Key": "commercial-answer-stale-case"},
    )
    assert stale_conversation.status_code == 409


def test_v2_endpoints_fail_closed_when_feature_is_disabled():
    repository = InMemoryWorkbenchRepository()
    objects = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    agent = CallableAgentClient(lambda request: build_agent_v3_result(request))
    principal = Principal(
        subject="reviewer-1",
        org_id="org-a",
        roles=frozenset({Role.reviewer}),
    )
    settings = Settings(environment="test")
    service = WorkbenchService(
        repository=repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
    )
    app = create_app(
        container=Container(
            settings=settings,
            repository=repository,
            object_store=objects,
            publisher=publisher,
            agent=agent,
            token_verifier=StaticTokenVerifier({"reviewer-token": principal}),
            service=service,
        ),
        settings=settings,
    )
    client = TestClient(app)
    case = client.post(
        "/api/v1/cases",
        json={"title": "disabled"},
        headers=_auth(),
    ).json()
    response = client.post(
        f"/api/v2/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "disabled-review"},
    )
    assert response.status_code == 409


def test_enhanced_runtime_requires_loopback_and_exact_capability_identities():
    common = {
        "environment": "local_uat",
        "process_role": "runtime",
        "enhanced_agent_experience": True,
        "uat_adaptive_active": True,
        "uat_evidence_profile": "preserved-3law-v3-scenario-eval",
        "uat_planner_mode": "diagnostic_active",
        "public_base_url": "http://127.0.0.1:18080",
        "agent_base_url": "http://127.0.0.1:18081",
    }
    with pytest.raises(ValueError, match="capability identities"):
        Settings(**common)
    with pytest.raises(ValueError, match="literal loopback"):
        Settings(
            **{**common, "public_base_url": "http://localhost:18080"},
            uat_expected_capability_sha256="1" * 64,
            uat_expected_capability_file_sha256="2" * 64,
        )
    with pytest.raises(ValueError, match="require enhanced"):
        Settings(
            uat_expected_capability_sha256="1" * 64,
            uat_expected_capability_file_sha256="2" * 64,
        )
