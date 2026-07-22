from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from customs_backend.agent_v3_semantics import reconstruct_current_agent_facts
from customs_backend.domain import AgentAnswerResponse, canonical_digest


_QUESTION_TOKEN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_QUESTION_STOP_WORDS = frozenset(
    {"어떤", "무엇", "있나요", "인가요", "대해", "대한", "현재", "이번", "해당", "알려줘"}
)
_ANSWER_PREFIXES = ("", "확인된 Case 근거에 따르면, ", "근거 확인 결과: ")


@dataclass(frozen=True, slots=True)
class _Claim:
    text: str
    citations: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExpectedAnswerProjection:
    status: str
    claims: tuple[_Claim, ...] = ()
    refusal_text: str | None = None
    backend_owned_outcome: bool = False

    def deterministic_wire(self, question_id: str) -> dict[str, Any]:
        if self.status != "answered":
            if self.refusal_text is None:
                raise ValueError("canonical Case answer refusal is missing")
            answer = self.refusal_text
            citations: list[dict[str, str]] = []
        else:
            answer = "\n".join(claim.text for claim in self.claims)
            citations = []
            seen: set[tuple[str, str]] = set()
            for claim in self.claims:
                for citation in claim.citations:
                    identity = (citation["kind"], citation["reference_id"])
                    if identity not in seen:
                        seen.add(identity)
                        citations.append(dict(citation))
        return {
            "question_id": question_id,
            "answer": answer,
            "status": self.status,
            "citations": citations,
            "generation_mode": "deterministic_fallback",
            "citation_verification": "passed",
            "model_call": None,
        }


def _citation(kind: str, reference_id: str) -> dict[str, str]:
    return {"kind": kind, "reference_id": reference_id}


def _normalize_claim_text(text: str) -> str:
    """Match the Agent's newline-safe canonical Chat claim representation."""

    return " ".join(text.split())


def _bounded_claims(claims: list[_Claim]) -> tuple[_Claim, ...]:
    selected: list[_Claim] = []
    rendered_size = 0
    for claim in claims:
        additional = len(claim.text) + (1 if selected else 0)
        if len(selected) >= 20 or rendered_size + additional > 7600:
            break
        selected.append(claim)
        rendered_size += additional
    return tuple(selected)


def expected_answer_projection(
    *,
    provider_result: dict[str, Any],
    question: str,
    effective_disposition: str,
) -> ExpectedAnswerProjection:
    """Independently rebuild the Agent's bounded answer facts.

    Outcome is intentionally Backend-owned: the provider result can be safely
    downgraded by release, input, or kill-switch Gates after Agent execution.
    Historical ``investigation_input:`` facts are never eligible for Case Chat.
    """

    lowered = question.casefold()
    if any(
        token in lowered for token in ("weather", "stock", "news", "날씨", "주식", "뉴스", "요리")
    ):
        return ExpectedAnswerProjection(
            status="out_of_scope",
            refusal_text="이 질문은 현재 통관 Case와 인용된 근거의 범위를 벗어납니다.",
        )
    tokens = {
        token.casefold()
        for token in _QUESTION_TOKEN.findall(question)
        if token.casefold() not in _QUESTION_STOP_WORDS
    }
    issue_intent = bool(tokens.intersection({"문제", "오류", "불일치", "위험", "finding", "issue"}))
    regulation_intent = bool(
        tokens.intersection({"법령", "규정", "요건", "서류", "근거", "evidence"})
    )
    outcome_intent = bool(
        tokens.intersection({"결과", "상태", "완료", "심사", "outcome", "status"})
    )
    field_intent = bool(
        tokens.intersection({"문서", "금액", "수량", "원산지", "송장", "선하", "품목", "hsk", "hs"})
    )
    if not (issue_intent or regulation_intent or outcome_intent or field_intent):
        return ExpectedAnswerProjection(
            status="out_of_scope",
            refusal_text="이 질문은 현재 통관 Case의 문서·규정·심사 결과 범위를 벗어납니다.",
        )

    # Build current facts from the provider's V2 sources.  A label-prefix
    # filter is not safe here because ``investigation_input:`` is also a valid
    # document-id prefix; retained historical facts are excluded by identity,
    # not by text.
    ledger_entries, _current_references = reconstruct_current_agent_facts(provider_result)
    ledger_by_kind: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in ledger_entries:
        kind = entry.get("kind")
        reference_id = entry.get("reference_id")
        if isinstance(kind, str) and isinstance(reference_id, str):
            ledger_by_kind.setdefault(kind, {})[reference_id] = entry

    claims: list[_Claim] = []
    if issue_intent:
        for index, finding_value in enumerate(provider_result.get("regulatory_findings", [])):
            if not isinstance(finding_value, dict):
                raise ValueError("regulatory finding is malformed")
            reference_id = f"finding_{canonical_digest({'index': index, 'finding': finding_value})}"
            if reference_id not in ledger_by_kind.get("finding", {}):
                raise ValueError("regulatory finding is missing from the current FactLedger")
            evidence_ids = finding_value.get("evidence_ids")
            if not isinstance(evidence_ids, list) or any(
                not isinstance(value, str) for value in evidence_ids
            ):
                raise ValueError("regulatory finding Evidence IDs are malformed")
            claims.append(
                _Claim(
                    text=_normalize_claim_text(
                        f"{finding_value['finding_code']}: {finding_value['message']}"
                    ),
                    citations=(
                        _citation("finding", reference_id),
                        *(_citation("evidence", value) for value in evidence_ids),
                    ),
                )
            )
        for index, issue_value in enumerate(provider_result.get("validation_issues", [])):
            if not isinstance(issue_value, dict):
                raise ValueError("validation issue is malformed")
            reference_id = f"issue_{canonical_digest({'index': index, 'issue': issue_value})}"
            if reference_id in ledger_by_kind.get("issue", {}):
                claims.append(
                    _Claim(
                        text=_normalize_claim_text(
                            f"{issue_value['issue_code']}: {issue_value['message']}"
                        ),
                        citations=(_citation("issue", reference_id),),
                    )
                )
    elif regulation_intent and provider_result.get("rag_evidence"):
        for evidence_value in provider_result["rag_evidence"]:
            if not isinstance(evidence_value, dict):
                raise ValueError("RAG Evidence is malformed")
            claims.append(
                _Claim(
                    text=_normalize_claim_text(
                        f"{evidence_value['title']}: {str(evidence_value['text'])[:1000]}"
                    ),
                    citations=(_citation("evidence", str(evidence_value["evidence_id"])),),
                )
            )
    elif outcome_intent:
        current_outcomes = [
            entry
            for entry in ledger_entries
            if entry.get("kind") == "outcome" and entry.get("label") == "deterministic_outcome"
        ]
        if len(current_outcomes) != 1:
            raise ValueError("current outcome Fact is missing or ambiguous")
        claims.append(
            _Claim(
                text=_normalize_claim_text(f"현재 사전심사 상태는 {effective_disposition}입니다."),
                citations=(_citation("outcome", str(current_outcomes[0]["reference_id"])),),
            )
        )
    elif field_intent:
        specific_tokens = tokens.difference({"문서", "품목", "송장", "선하", "hsk", "hs"})
        matching = [
            entry
            for entry in ledger_entries
            if entry.get("kind") == "field_source"
            and (
                not specific_tokens
                or any(
                    token in f"{entry.get('label')} {entry.get('value')}".casefold()
                    for token in specific_tokens
                )
            )
        ]
        if not matching:
            return ExpectedAnswerProjection(
                status="insufficient_evidence",
                refusal_text="현재 Review Result에 질문과 직접 연결되는 문서 필드 근거가 없습니다.",
            )
        claims.extend(
            _Claim(
                text=_normalize_claim_text(f"{entry['label']}: {entry['value']}"),
                citations=(_citation("field_source", str(entry["reference_id"])),),
            )
            for entry in matching
        )

    selected = _bounded_claims(claims)
    if not selected:
        return ExpectedAnswerProjection(
            status="insufficient_evidence",
            refusal_text="현재 Review Result에 질문에 답할 수 있는 문서·규정 근거가 없습니다.",
        )
    return ExpectedAnswerProjection(
        status="answered",
        claims=selected,
        backend_owned_outcome=(outcome_intent and not issue_intent and not regulation_intent),
    )


def validate_agent_answer_projection(
    *,
    expected: ExpectedAnswerProjection,
    question_id: str,
    answer: AgentAnswerResponse,
    runtime_release: dict[str, Any],
) -> None:
    canonical = expected.deterministic_wire(question_id)
    actual = answer.model_dump(mode="json")
    if actual["question_id"] != question_id or actual["status"] != canonical["status"]:
        raise ValueError("Case answer identity and status differ from the canonical plan")
    if actual["citations"] != canonical["citations"]:
        raise ValueError("Case answer citations differ from the canonical plan")
    if expected.status != "answered":
        if actual != canonical:
            raise ValueError("refused Case answer must use the exact deterministic response")
        return
    rendered_claims = answer.answer.split("\n")
    canonical_claims = [claim.text for claim in expected.claims]
    if len(rendered_claims) != len(canonical_claims):
        raise ValueError("Case answer must cover every canonical claim exactly once")
    if answer.generation_mode == "deterministic_fallback":
        if rendered_claims != canonical_claims:
            raise ValueError("deterministic Case answer must use exact canonical text")
        return
    call = answer.model_call
    if (
        call is None
        or call.task_id != "customs_case_answer"
        or call.provider != "anthropic"
        or call.model_id != runtime_release.get("model_id")
        or call.prompt_sha256 != runtime_release.get("chat_prompt_sha256")
        or call.output_schema_sha256 != runtime_release.get("chat_schema_sha256")
        or call.input_tokens <= 0
        or call.output_tokens <= 0
    ):
        raise ValueError("Claude Case answer is not bound to the runtime release")
    if any(
        rendered not in {f"{prefix}{canonical_text}" for prefix in _ANSWER_PREFIXES}
        for rendered, canonical_text in zip(rendered_claims, canonical_claims, strict=True)
    ):
        raise ValueError("Claude Case answer must use a code-owned safe frame")


__all__ = [
    "ExpectedAnswerProjection",
    "expected_answer_projection",
    "validate_agent_answer_projection",
]
