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
_HANGUL_TOKEN = re.compile(r"^[가-힣]+$")
_ISSUE_KEYWORDS = frozenset({"문제", "오류", "불일치", "위험", "finding", "issue"})
_REGULATION_KEYWORDS = frozenset({"법령", "규정", "요건", "서류", "근거", "evidence"})
_OUTCOME_KEYWORDS = frozenset({"결과", "상태", "완료", "심사", "outcome", "status"})
_FIELD_KEYWORDS = frozenset({"문서", "금액", "수량", "원산지", "송장", "선하", "품목", "hsk", "hs"})
_EXPLANATION_KEYWORDS = frozenset({"왜", "이유", "원인", "의미", "설명", "해석", "영향"})
_ACTION_KEYWORDS = frozenset({"조치", "해야", "해결", "수정", "확인", "다음", "대응"})
_FOLLOW_UP_KEYWORDS = frozenset({"그럼", "그러면", "그것", "그게", "이것", "이게", "앞서", "방금"})
_MAX_EVIDENCE_CLAIMS = 5
_MAX_EVIDENCE_EXCERPT_CHARS = 360
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


def _evidence_excerpt(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= _MAX_EVIDENCE_EXCERPT_CHARS:
        return compact
    return f"{compact[: _MAX_EVIDENCE_EXCERPT_CHARS - 1].rstrip()}…"


def _review_disposition_text(disposition: str) -> str:
    values = {
        "auto_review_complete": "자동 사전심사가 완료된 상태",
        "human_review_required": "담당자 검토가 필요한 상태",
        "blocked": "실행 또는 근거 조건 때문에 심사가 차단된 상태",
    }
    try:
        return values[disposition]
    except KeyError as exc:
        raise ValueError("effective review disposition is invalid") from exc


def _citable_regulatory_evidence_ids(provider_result: dict[str, Any]) -> tuple[str, ...]:
    identifiers: list[str] = []
    for value in provider_result.get("rag_evidence", []):
        if not isinstance(value, dict):
            raise ValueError("RAG Evidence is malformed")
        evidence_id = value.get("evidence_id")
        if not isinstance(evidence_id, str):
            raise ValueError("RAG Evidence identity is malformed")
        if value.get("eligibility") in {"supporting", "finding"}:
            identifiers.append(evidence_id)
    return tuple(identifiers)


def _matches_intent(
    tokens: set[str],
    *,
    keywords: frozenset[str],
) -> bool:
    """Recognize bounded domain intent without attempting Korean morphology.

    Korean particles and sentence endings are attached to the preceding noun,
    so a bounded domain keyword may occur inside one Hangul token
    (for example ``결과인가요``).  English keywords remain exact-token matches
    to avoid accepting unrelated words that merely contain ``status`` or
    ``issue``.
    """

    for keyword in keywords:
        if keyword.isascii():
            if keyword in tokens:
                return True
        elif any(_HANGUL_TOKEN.fullmatch(token) and keyword in token for token in tokens):
            return True
    return False


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
    issue_intent = _matches_intent(
        tokens,
        keywords=_ISSUE_KEYWORDS,
    )
    regulation_intent = _matches_intent(
        tokens,
        keywords=_REGULATION_KEYWORDS,
    )
    outcome_intent = _matches_intent(
        tokens,
        keywords=_OUTCOME_KEYWORDS,
    )
    field_intent = _matches_intent(
        tokens,
        keywords=_FIELD_KEYWORDS,
    )
    explanation_intent = (
        _matches_intent(
            tokens,
            keywords=_EXPLANATION_KEYWORDS,
        )
        or "왜" in question
    )
    action_intent = _matches_intent(
        tokens,
        keywords=_ACTION_KEYWORDS,
    )
    follow_up_intent = _matches_intent(
        tokens,
        keywords=_FOLLOW_UP_KEYWORDS,
    )
    core_case_intent = issue_intent or regulation_intent or outcome_intent or field_intent
    bare_why_intent = "왜" in question and tokens.issubset(
        _EXPLANATION_KEYWORDS | _FOLLOW_UP_KEYWORDS
    )
    case_intent = (
        core_case_intent
        or action_intent
        or bare_why_intent
        or (explanation_intent and follow_up_intent)
    )
    if not case_intent:
        return ExpectedAnswerProjection(
            status="out_of_scope",
            refusal_text="이 질문은 현재 통관 Case의 문서·규정·심사 결과 범위를 벗어납니다.",
        )

    # Build current facts from the provider's V2 sources.  A label-prefix
    # filter is not safe here because ``investigation_input:`` is also a valid
    # document-id prefix; retained historical facts are excluded by identity,
    # not by text.
    ledger_entries, current_references = reconstruct_current_agent_facts(provider_result)
    ledger_by_kind: dict[str, dict[str, dict[str, Any]]] = {}
    ledger_by_reference: dict[str, dict[str, Any]] = {}
    for entry in ledger_entries:
        kind = entry.get("kind")
        reference_id = entry.get("reference_id")
        if isinstance(kind, str) and isinstance(reference_id, str):
            ledger_by_kind.setdefault(kind, {})[reference_id] = entry
            ledger_by_reference[reference_id] = entry

    outcome_reference = current_references.get("outcome")
    if not isinstance(outcome_reference, str):
        raise ValueError("current outcome Fact is missing or ambiguous")

    claims: list[_Claim] = []

    def append_claim(text: str, citations: tuple[dict[str, str], ...]) -> None:
        claim = _Claim(text=_normalize_claim_text(text), citations=citations)
        if claim not in claims:
            claims.append(claim)

    def citations_for(reference_ids: list[Any]) -> tuple[dict[str, str], ...]:
        citations: list[dict[str, str]] = []
        for reference_id in reference_ids:
            if not isinstance(reference_id, str):
                raise ValueError("required action source reference is malformed")
            fact = ledger_by_reference.get(reference_id)
            if fact is not None:
                kind = fact.get("kind")
                if not isinstance(kind, str):
                    raise ValueError("current Fact kind is malformed")
                citations.append(_citation(kind, reference_id))
        return tuple(citations)

    detailed_explanation = explanation_intent and (
        core_case_intent or follow_up_intent or "왜" in question
    )
    if outcome_intent or detailed_explanation or action_intent or follow_up_intent:
        append_claim(
            f"결론 — 현재 사전심사 결과는 {_review_disposition_text(effective_disposition)}입니다.",
            (_citation("outcome", outcome_reference),),
        )

    if issue_intent or detailed_explanation:
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
            append_claim(
                (
                    f"원인 — 규정 검토에서 {finding_value['message']} "
                    f"({finding_value['finding_code']})"
                ),
                (
                    _citation("finding", reference_id),
                    *(_citation("evidence", value) for value in evidence_ids),
                ),
            )
        for index, issue_value in enumerate(provider_result.get("validation_issues", [])):
            if not isinstance(issue_value, dict):
                raise ValueError("validation issue is malformed")
            reference_id = f"issue_{canonical_digest({'index': index, 'issue': issue_value})}"
            if reference_id in ledger_by_kind.get("issue", {}):
                append_claim(
                    (
                        f"원인 — 문서 교차검증에서 {issue_value['message']} "
                        f"({issue_value['issue_code']})"
                    ),
                    (_citation("issue", reference_id),),
                )

    validation_issues = provider_result.get("validation_issues", [])
    regulatory_findings = provider_result.get("regulatory_findings", [])
    if not isinstance(validation_issues, list) or not isinstance(regulatory_findings, list):
        raise ValueError("review issue or finding collection is malformed")
    citable_evidence_ids = _citable_regulatory_evidence_ids(provider_result)
    if (issue_intent or detailed_explanation) and not validation_issues and not regulatory_findings:
        no_issue_citations = [_citation("outcome", outcome_reference)]
        if citable_evidence_ids:
            no_issue_citations.append(_citation("evidence", citable_evidence_ids[0]))
        append_claim(
            (
                "원인 — 결정론적 문서 교차검증과 현재 인용 가능한 규정 Evidence 범위에서 "
                "별도의 불일치 또는 미충족 요건이 확인되지 않았습니다."
            ),
            tuple(no_issue_citations),
        )

    if regulation_intent or detailed_explanation:
        citable_evidence_id_set = set(citable_evidence_ids)
        evidence_claim_count = 0
        for evidence_value in provider_result["rag_evidence"]:
            if not isinstance(evidence_value, dict):
                raise ValueError("RAG Evidence is malformed")
            evidence_id = str(evidence_value["evidence_id"])
            if evidence_id not in citable_evidence_id_set:
                continue
            append_claim(
                (
                    f"근거 — {evidence_value['title']}: "
                    f"{_evidence_excerpt(str(evidence_value['text']))}"
                ),
                (_citation("evidence", evidence_id),),
            )
            evidence_claim_count += 1
            if evidence_claim_count >= _MAX_EVIDENCE_CLAIMS:
                break

    if detailed_explanation and (validation_issues or regulatory_findings):
        append_claim(
            (
                "영향 — 확인된 문서 불일치 또는 규정 요건이 해결되기 전에는 "
                "현재 사전심사를 자동으로 완료할 수 없습니다."
            ),
            (_citation("outcome", outcome_reference),),
        )

    if action_intent or detailed_explanation:
        required_actions = provider_result.get("required_actions", [])
        if not isinstance(required_actions, list):
            raise ValueError("required action collection is malformed")
        for action_value in required_actions:
            if not isinstance(action_value, dict):
                raise ValueError("required action is malformed")
            source_reference_ids = action_value.get("source_reference_ids")
            if not isinstance(source_reference_ids, list):
                raise ValueError("required action source references are malformed")
            citations = citations_for(source_reference_ids)
            if citations:
                append_claim(f"조치 — {action_value['message']}", citations)
        if not required_actions:
            append_claim(
                "조치 — 현재 ReviewResult에 필수 후속 조치가 기록되어 있지 않습니다.",
                (_citation("outcome", outcome_reference),),
            )

    if field_intent:
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
        if not matching and not claims:
            return ExpectedAnswerProjection(
                status="insufficient_evidence",
                refusal_text="현재 Review Result에 질문과 직접 연결되는 문서 필드 근거가 없습니다.",
            )
        claims.extend(
            _Claim(
                text=_normalize_claim_text(f"문서 근거 — {entry['label']}: {entry['value']}"),
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
        backend_owned_outcome=(
            (outcome_intent and not issue_intent and not regulation_intent and not field_intent)
            or (
                effective_disposition != provider_result.get("review_disposition")
                and (outcome_intent or detailed_explanation or action_intent or follow_up_intent)
            )
        ),
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
