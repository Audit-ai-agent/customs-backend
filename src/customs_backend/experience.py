from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from customs_backend.domain import (
    AgentReviewResultV3,
    JobStatus,
    canonical_digest,
)
from customs_backend.experience_contract import (
    MeaningCatalogV1,
    provider_meaning_catalog,
    validate_agent_experience_contract_bundle,
)


_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_SECTION_ORDER = (
    "conclusion",
    "process",
    "document_comparison",
    "issues_and_causes",
    "regulatory_meaning",
    "business_impact",
    "next_actions",
    "scope_and_limits",
)
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
_ANSWER_AREA_ROLES: dict[str, frozenset[str]] = {
    "direct_answer": frozenset(
        {"conclusion", "observation", "reason", "evidence", "impact", "action", "limitation"}
    ),
    "explanation": frozenset({"conclusion", "observation", "reason", "limitation"}),
    "evidence_meaning": frozenset({"evidence", "limitation"}),
    "impact": frozenset({"impact"}),
    "next_steps": frozenset({"action"}),
}
_CASE_LIMITATION = "답변은 현재 심사 회차와 연결된 문서·법령 근거 범위에 한정됩니다."
_GENERAL_GUIDANCE_LIMITATION = (
    "위 내용은 일반 통관 안내이며 현재 심사 건의 판정이나 법적 의무를 뜻하지 않습니다."
)
_URL_RE = re.compile(r"(?i)(?:https?://|www\.)\S+")
_HTML_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_OPAQUE_INTERNAL_RE = re.compile(
    r"(?i)(?:experience_claim|statement|fact|issue|finding|ev|outcome)_[a-f0-9]{32,}"
    r"|(?<![A-Fa-f0-9])[A-Fa-f0-9]{64}(?![A-Fa-f0-9])"
)
_NUMBER_RE = re.compile(
    r"(?<![0-9])(?P<number>\d+(?:,\d{3})*(?:\.\d+)?)"
    r"\s*(?P<unit>%|원|달러|유로|엔|kg|㎏|g|톤|개|건|종|회|일|개월|년|월|조|항|호)?"
    r"(?![0-9])",
    re.IGNORECASE,
)
_ALPHANUMERIC_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9._:/-]{3,}(?![A-Za-z0-9._:/-]))"
    r"(?=[A-Za-z0-9._:/-]*[A-Za-z])(?=[A-Za-z0-9._:/-]*\d)"
    r"[A-Za-z0-9][A-Za-z0-9._:/-]*"
)
_UPPERCASE_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2,}(?:[-_/][A-Z0-9]+)*)(?![A-Za-z0-9])"
)
_KOREAN_LAW_RE = re.compile(
    r"(?<![가-힣])(?:[가-힣]{2,30}(?:시행규칙|시행령|법률|고시)|[가-힣]{2,30}법)"
    r"(?=$|[\s.,;:!?()\[\]{}“”\"']|은|는|이|가|을|를|의|에|에서|으로|로|과|와|도|만)"
)
_NON_LAW_SUFFIXES = (
    "방법",
    "기법",
    "해법",
    "문법",
    "어법",
    "용법",
    "화법",
    "농법",
    "공법",
    "수법",
    "비법",
    "요법",
)
_KOREAN_INSTITUTION_RE = re.compile(
    r"(?<![가-힣])(?:[가-힣]{2,24}\s*)"
    r"(?:관세청|세관|주식회사|유한회사|법인|은행|항만공사|항만|공항)"
    r"(?=$|[\s.,;:!?()\[\]{}“”\"']|은|는|이|가|을|를|의|에|에서|으로|로|과|와|도|만)"
)
_ASCII_ORGANIZATION_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Z][A-Za-z&.-]*\s+){0,4}"
    r"(?:CORP(?:ORATION)?|CO(?:MPANY)?|LTD|INC|LLC)(?![A-Za-z])",
    re.IGNORECASE,
)
_COUNTRY_RE = re.compile(
    r"(?<![가-힣])(?:대한민국|한국|중국|일본|미국|독일|프랑스|영국|베트남|태국|"
    r"인도네시아|인도|호주|캐나다|러시아|대만|홍콩|싱가포르|말레이시아|필리핀|"
    r"네덜란드|이탈리아|스페인|브라질|멕시코)"
    r"(?=$|[\s.,;:!?()\[\]{}“”\"']|은|는|이|가|을|를|의|에|에서|으로|로|과|와|도|만|산)"
)
_KOREAN_PORT_RE = re.compile(
    r"(?<![가-힣])(?:[가-힣]{2,24}(?:항구|항))"
    r"(?=$|[\s.,;:!?()\[\]{}“”\"']|은|는|이|가|을|를|의|에|에서|으로|로|과|와|도|만)"
)
_NON_PORT_SUFFIXES = ("사항", "문항", "조항", "항목")
_GENERAL_GUIDANCE_CASE_ASSERTION_RE = re.compile(
    r"(?:현재|이|해당)\s*(?:심사\s*건|건|case).{0,40}"
    r"(?:판정|결과|의무|적용|승인|자동\s*완료|문제\s*(?:없|있))",
    re.IGNORECASE,
)
_COUNT_UNITS = {"개": "count", "건": "count", "종": "count"}
_PRESENTATION_MIN_TOTAL_MATERIAL_CHARS = 650
_PRESENTATION_MIN_SECTION_CHARS = 28
_PRESENTATION_MIN_MATERIAL_STATEMENT_CHARS = 18
_PRESENTATION_MIN_KOREAN_RATIO = 0.65
_DOCUMENT_TYPE_LABELS = {
    "invoice": "상업송장(CI)",
    "packing_list": "포장명세서(PL)",
    "bill_of_lading": "선하증권(B/L)",
    "certificate_of_origin": "원산지증명서(C/O)",
    "unknown": "제출 문서",
}
_FIELD_LABELS = {
    "invoice_no": "송장번호",
    "invoice_number": "송장번호",
    "invoice_date": "송장 발행일",
    "packing_no": "포장명세서 번호",
    "bl_no": "선하증권 번호",
    "certificate_no": "원산지증명서 번호",
    "seller": "판매자",
    "buyer": "구매자",
    "exporter": "수출자",
    "importer": "수입자",
    "shipper": "송하인",
    "consignee": "수하인",
    "notify_party": "착하통지처",
    "vessel": "선박명",
    "voyage_no": "항차",
    "port_of_loading": "선적항",
    "port_of_discharge": "양륙항",
    "departure_date": "출항일",
    "country_of_origin": "원산지",
    "origin_country": "원산지",
    "hs_code": "품목분류번호(HSK)",
    "description": "품명",
    "goods_description": "물품 설명",
    "quantity": "수량",
    "unit_price": "단가",
    "line_amount": "품목 금액",
    "total_amount": "총금액",
    "currency": "통화",
    "packages": "포장 수량",
    "gross_weight_kg": "총중량",
    "total_gross_weight_kg": "전체 총중량",
    "net_weight_kg": "순중량",
    "measurement_cbm": "용적",
    "container_numbers": "컨테이너 번호",
    "seal_numbers": "봉인 번호",
    "shipping_marks": "화인",
}
_FIELD_PRIORITY = {
    name: index
    for index, name in enumerate(
        (
            "invoice_no",
            "invoice_number",
            "invoice_date",
            "packing_no",
            "bl_no",
            "certificate_no",
            "hs_code",
            "description",
            "goods_description",
            "quantity",
            "unit_price",
            "line_amount",
            "total_amount",
            "currency",
            "country_of_origin",
            "origin_country",
            "seller",
            "buyer",
            "exporter",
            "importer",
            "shipper",
            "consignee",
            "vessel",
            "voyage_no",
            "port_of_loading",
            "port_of_discharge",
            "departure_date",
            "packages",
            "gross_weight_kg",
            "total_gross_weight_kg",
            "net_weight_kg",
            "measurement_cbm",
            "container_numbers",
            "seal_numbers",
            "shipping_marks",
        )
    )
}
_SAFE_STAGE_COUNT_KEYS = frozenset(
    {
        "documents",
        "issues",
        "findings",
        "evidence",
        "statements",
        "turns",
        "citations",
    }
)
_PRESENTATION_TASK_ID = "customs_experience_report"
_CHAT_TASK_ID = "customs_case_answer_v2"
_CITATION_KINDS = frozenset({"field_source", "issue", "finding", "evidence", "outcome"})
PresentationSectionCode = Literal[
    "conclusion",
    "process",
    "document_comparison",
    "issues_and_causes",
    "regulatory_meaning",
    "business_impact",
    "next_actions",
    "scope_and_limits",
]
PresentationRole = Literal[
    "conclusion",
    "observation",
    "reason",
    "evidence",
    "impact",
    "action",
    "limitation",
]
CitationKind = Literal["field_source", "issue", "finding", "evidence", "outcome"]
AssistantStageCode = Literal[
    "queued",
    "request_validation",
    "conversation_binding",
    "answer_planning",
    "answer_composition",
    "grounding_verification",
    "result_validation",
    "persistence",
    "completed",
    "failed",
]


class CitationReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CitationKind
    reference_id: str = Field(min_length=1, max_length=256)


class AnswerScopeClassificationAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["claude", "deterministic_guard", "fallback"]
    task_id: Literal["customs_case_scope_classifier"]
    classified_scope: Literal[
        "case_grounded",
        "evidence_grounded",
        "general_customs_guidance",
        "clarification_needed",
        "insufficient_evidence",
        "out_of_scope",
    ]
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0, le=300_000)
    reason_code: str = Field(min_length=1, max_length=128)
    guidance_topics: list[
        Literal[
            "invoice",
            "packing_list",
            "bill_of_lading",
            "certificate_of_origin",
            "hsk",
            "hs_hsk_comparison",
            "customs_review",
            "import_declaration",
            "customs_valuation",
            "duties_and_taxes",
            "free_trade_agreement",
            "import_requirements",
            "inspection_and_supplement",
            "document_correction",
            "customs_broker",
        ]
    ] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def classification_audit_is_complete(self) -> "AnswerScopeClassificationAuditV1":
        if self.mode == "claude":
            if self.model_id is None or self.input_tokens <= 0 or self.output_tokens <= 0:
                raise ValueError("Claude scope classification requires complete model audit")
        elif self.model_id is not None or self.input_tokens or self.output_tokens:
            raise ValueError("non-model scope classification cannot claim a model call")
        if self.mode == "deterministic_guard" and self.classified_scope != "out_of_scope":
            raise ValueError("deterministic scope guard is reserved for blocked input")
        if self.mode == "fallback" and self.classified_scope != "clarification_needed":
            raise ValueError("scope-classification fallback must request clarification")
        if self.classified_scope == "general_customs_guidance":
            if not self.guidance_topics:
                raise ValueError("general guidance classification requires a topic")
            if len(self.guidance_topics) != len(set(self.guidance_topics)):
                raise ValueError("general guidance topics must be unique")
        elif self.guidance_topics:
            raise ValueError("guidance topics are allowed only for general guidance")
        return self


class ExperienceGenerationAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["claude", "deterministic_fallback"]
    task_id: str = Field(min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0, le=300_000)
    attempt_count: int = Field(default=1, ge=1, le=2)
    scope_classification: AnswerScopeClassificationAuditV1 | None = None

    @model_validator(mode="after")
    def mode_matches_model_audit(self) -> "ExperienceGenerationAuditV1":
        if self.mode == "claude":
            if self.model_id is None or self.input_tokens <= 0 or self.output_tokens <= 0:
                raise ValueError("Claude experience generation requires complete model audit")
        elif self.model_id is not None or self.input_tokens or self.output_tokens:
            raise ValueError("deterministic experience generation cannot claim a model call")
        return self


class ExperienceVerificationAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "fallback"]
    deterministic_checks_passed: bool
    entailment_verifier: Literal["claude", "not_run"]
    verifier_model_id: str | None = Field(default=None, min_length=1, max_length=128)
    verifier_prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    verifier_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    verifier_context_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    checked_statement_count: int = Field(ge=0, le=1000)
    verified_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    reason_codes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def verifier_identity_is_consistent(self) -> "ExperienceVerificationAuditV1":
        claude = self.entailment_verifier == "claude"
        if claude != (
            self.verifier_model_id is not None and self.verifier_context_sha256 is not None
        ):
            raise ValueError("entailment verifier identity is incomplete")
        if not claude and self.verifier_context_sha256 is not None:
            raise ValueError("a non-model verifier cannot claim a context identity")
        if self.status == "passed" and (not self.deterministic_checks_passed or self.reason_codes):
            raise ValueError("passed verification cannot contain blockers")
        if self.status == "fallback" and not self.reason_codes:
            raise ValueError("fallback verification requires a reason")
        return self


class CanonicalExperienceClaimV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^experience_claim_[a-f0-9]{64}$")
    section_code: PresentationSectionCode
    role: PresentationRole
    canonical_text: str = Field(min_length=1, max_length=4_000)
    citation_references: list[CitationReferenceV1] = Field(
        default_factory=list,
        max_length=100,
    )
    material: bool = True

    @model_validator(mode="after")
    def stable_and_grounded(self) -> "CanonicalExperienceClaimV1":
        if self.material and not self.citation_references:
            raise ValueError("material presentation claims require citations")
        payload = self.model_dump(mode="json", exclude={"claim_id"})
        if self.claim_id != f"experience_claim_{canonical_digest(payload)}":
            raise ValueError("experience claim ID does not bind its contents")
        return self


_DEFAULT_ISSUE_MEANING = (
    "문서에서 추가 확인이 필요한 항목이 발견되었습니다",
    "문서 사이의 값 또는 계산 결과가 정해진 검증 기준과 일치하지 않습니다.",
    "값을 확정하기 전에는 담당자 검토가 필요합니다.",
)
_DEFAULT_FINDING_MEANING = (
    "현재 법령 근거와 제출 자료만으로 요건 충족 여부를 확정하지 못했습니다.",
    "규정 요건을 확인하기 전에는 담당자 검토가 필요합니다.",
)
_UNKNOWN_ACTION_TEXT = "해석되지 않은 확인 항목이 있어 담당자가 연결된 근거를 직접 확인해야 합니다."


def _provider_action_text(
    catalog: MeaningCatalogV1,
    action_code: str,
) -> str:
    known = catalog.actions.get(action_code)
    if known is not None:
        return known
    if action_code.startswith("resolve_"):
        source_code = action_code.removeprefix("resolve_").upper()
        issue = catalog.issues.get(source_code)
        if issue is not None:
            return issue.next_action
        finding = catalog.findings.get(source_code)
        if finding is not None:
            return finding.next_action
    return _UNKNOWN_ACTION_TEXT


def _canonical_claim(
    section_code: PresentationSectionCode,
    role: PresentationRole,
    text: str,
    citations: list[CitationReferenceV1],
) -> CanonicalExperienceClaimV1:
    deduplicated = list({(item.kind, item.reference_id): item for item in citations}.values())
    canonical_text = " ".join(text.split())
    payload = {
        "section_code": section_code,
        "role": role,
        "canonical_text": canonical_text,
        "citation_references": [item.model_dump(mode="json") for item in deduplicated],
        "material": True,
    }
    return CanonicalExperienceClaimV1(
        claim_id=f"experience_claim_{canonical_digest(payload)}",
        section_code=section_code,
        role=role,
        canonical_text=canonical_text,
        citation_references=deduplicated,
        material=True,
    )


def _normalized_atom(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", normalized).replace(",", "").casefold().strip(".,;:()[]{}“”\"'")


def _factual_atoms(text: str) -> frozenset[str]:
    """Extract values that may not be invented by a presentation model."""

    text = unicodedata.normalize("NFKC", text)
    atoms: set[str] = set()
    for match in _NUMBER_RE.finditer(text):
        number = match.group("number").replace(",", "")
        atoms.add(f"number:{number}")
        unit = (match.group("unit") or "").casefold()
        if unit:
            atoms.add(f"number-unit:{number}:{_COUNT_UNITS.get(unit, unit)}")
    for pattern, prefix in (
        (_ALPHANUMERIC_IDENTIFIER_RE, "identifier"),
        (_UPPERCASE_IDENTIFIER_RE, "identifier"),
        (_KOREAN_LAW_RE, "law"),
        (_KOREAN_INSTITUTION_RE, "entity"),
        (_ASCII_ORGANIZATION_RE, "entity"),
        (_COUNTRY_RE, "entity"),
        (_KOREAN_PORT_RE, "entity"),
    ):
        for match in pattern.finditer(text):
            atom = _normalized_atom(match.group(0))
            if prefix == "law" and atom.endswith(_NON_LAW_SUFFIXES):
                continue
            if pattern is _KOREAN_PORT_RE and atom.endswith(_NON_PORT_SUFFIXES):
                continue
            if atom:
                atoms.add(f"{prefix}:{atom}")
    for match in re.finditer(r"[“\"]([^“”\"\r\n]{1,80})[”\"]", text):
        literal = _normalized_atom(match.group(1))
        if literal:
            atoms.add(f"value:{literal}")
    labelled_value = re.compile(
        r"(?:원산지|선적항|양륙항|도착항|출발항|선박명|통화|판매자|구매자|수출자|"
        r"수입자|송하인|수하인)(?:은|는|이|가|:)?\s*[“\"]?"
        r"([A-Za-z가-힣][A-Za-z가-힣0-9 .,&()/_-]{0,40}?)"
        r"(?:[”\"]?(?:입니다|이며|이고|로 확인|으로 확인|로 기재|으로 기재))"
    )
    for match in labelled_value.finditer(text):
        atom = _normalized_atom(match.group(1))
        if atom:
            atoms.add(f"value:{atom}")
    return frozenset(atoms)


def _grounded_prose_blockers(
    text: str,
    source_texts: list[str],
    *,
    prefix: str,
    minimum_chars: int,
) -> list[str]:
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    source = "\n".join(
        " ".join(unicodedata.normalize("NFKC", item).split()) for item in source_texts
    )
    blockers: list[str] = []
    if len(normalized) < minimum_chars:
        blockers.append(f"{prefix}_too_short")
    if any(
        ord(character) < 32 and character not in "\t\n\r" for character in text
    ) or _HTML_RE.search(normalized):
        blockers.append(f"{prefix}_unsafe_markup")
    if _URL_RE.search(normalized):
        blockers.append(f"{prefix}_url_not_allowed")
    if _OPAQUE_INTERNAL_RE.search(normalized):
        blockers.append(f"{prefix}_internal_identifier_not_allowed")
    if not _factual_atoms(normalized).issubset(_factual_atoms(source)):
        blockers.append(f"{prefix}_unsupported_factual_atom")
    if not normalized.endswith("니다."):
        blockers.append(f"{prefix}_formal_style_required")
    return blockers


def _korean_letter_ratio(text: str) -> float:
    hangul = sum("가" <= character <= "힣" for character in text)
    latin = sum(
        ("a" <= character.casefold() <= "z") for character in text if len(character.casefold()) == 1
    )
    denominator = hangul + latin
    return hangul / denominator if denominator else 0.0


def _presentation_statement_options(
    claims: list[CanonicalExperienceClaimV1],
) -> dict[tuple[str, ...], list[CanonicalExperienceClaimV1]]:
    """Reconstruct the provider's exact non-overlapping factual Claim groups."""

    groups: dict[tuple[str, str], list[CanonicalExperienceClaimV1]] = {}
    for claim in claims:
        groups.setdefault((claim.section_code, claim.role), []).append(claim)
    options: dict[tuple[str, ...], list[CanonicalExperienceClaimV1]] = {}
    for grouped in groups.values():
        chunks: list[list[CanonicalExperienceClaimV1]] = []
        current: list[CanonicalExperienceClaimV1] = []
        current_citations: set[tuple[str, str]] = set()
        for claim in grouped:
            claim_citations = {
                (str(citation.kind), citation.reference_id)
                for citation in claim.citation_references
            }
            candidate = [*current, claim]
            candidate_citations = current_citations | claim_citations
            candidate_source = " ".join(item.canonical_text for item in candidate)
            if current and (
                len(candidate) > 20
                or len(candidate_citations) > 100
                or len(candidate_source) > 4_000
            ):
                chunks.append(current)
                current = [claim]
                current_citations = claim_citations
            else:
                current = candidate
                current_citations = candidate_citations
        if current:
            chunks.append(current)
        for chunk in chunks:
            options[tuple(claim.claim_id for claim in chunk)] = chunk
    return options


def build_controlled_presentation_sections(
    claims: list[CanonicalExperienceClaimV1],
) -> list[PresentationSection]:
    """Build a deterministic plain fixture for each provider Claim group."""

    options = _presentation_statement_options(claims)
    statements_by_section: dict[str, list[PresentationStatement]] = {
        section_code: [] for section_code in _SECTION_ORDER
    }
    for claim_ids, bound_claims in options.items():
        first = bound_claims[0]
        canonical_text = " ".join(claim.canonical_text for claim in bound_claims)
        citations = list(
            {
                (citation.kind, citation.reference_id): citation
                for claim in bound_claims
                for citation in claim.citation_references
            }.values()
        )
        payload = {
            "role": first.role,
            "text": canonical_text,
            "source_claim_ids": list(claim_ids),
            "citation_references": [citation.model_dump(mode="json") for citation in citations],
            "material": any(claim.material for claim in bound_claims),
        }
        statements_by_section[first.section_code].append(
            PresentationStatement.model_validate(
                {
                    "statement_id": f"statement_{canonical_digest(payload)}",
                    **payload,
                }
            )
        )
    return [
        PresentationSection(
            section_code=cast(PresentationSectionCode, section_code),
            title=_SECTION_TITLES[section_code],
            statements=statements_by_section[section_code],
        )
        for section_code in _SECTION_ORDER
    ]


def _build_deterministic_fallback_sections(
    claims: list[CanonicalExperienceClaimV1],
) -> list[PresentationSection]:
    """Mirror the Agent fallback: one canonical Claim per statement."""

    sections: list[PresentationSection] = []
    for section_code in _SECTION_ORDER:
        statements: list[PresentationStatement] = []
        for claim in (item for item in claims if item.section_code == section_code):
            payload = {
                "role": claim.role,
                "text": claim.canonical_text,
                "source_claim_ids": [claim.claim_id],
                "citation_references": [
                    item.model_dump(mode="json") for item in claim.citation_references
                ],
                "material": claim.material,
            }
            statements.append(
                PresentationStatement.model_validate(
                    {
                        "statement_id": f"statement_{canonical_digest(payload)}",
                        **payload,
                    }
                )
            )
        sections.append(
            PresentationSection(
                section_code=cast(PresentationSectionCode, section_code),
                title=_SECTION_TITLES[section_code],
                statements=statements,
            )
        )
    return sections


def _field_fact_claims(
    result: AgentReviewResultV3,
) -> list[CanonicalExperienceClaimV1]:
    document_types = {
        item.get("document_id"): _DOCUMENT_TYPE_LABELS.get(
            str(item.get("document_type")),
            "제출 문서",
        )
        for item in result.document_results
        if isinstance(item, dict) and isinstance(item.get("document_id"), str)
    }
    candidates = [
        item
        for item in result.fact_ledger.get("entries", [])
        if isinstance(item, dict)
        and item.get("kind") == "field_source"
        and isinstance(item.get("reference_id"), str)
        and isinstance(item.get("label"), str)
        and isinstance(item.get("value"), str)
        and item["value"].strip().casefold() not in {"", "null", "none", "[]", "{}"}
    ]

    def sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
        field_name = item["label"].rsplit(".", 1)[-1]
        return (
            _FIELD_PRIORITY.get(field_name, len(_FIELD_PRIORITY) + 1),
            str(item.get("document_id") or ""),
            item["reference_id"],
        )

    claims: list[CanonicalExperienceClaimV1] = []
    for entry in sorted(candidates, key=sort_key)[:80]:
        field_name = entry["label"].rsplit(".", 1)[-1]
        field_label = _FIELD_LABELS.get(field_name, "문서 기재 항목")
        document_label = document_types.get(entry.get("document_id"), "제출 문서")
        value = " ".join(entry["value"].split())[:1_000]
        claims.append(
            _canonical_claim(
                "document_comparison",
                "observation",
                f"{document_label}에서 {field_label}의 기록값은 “{value}”로 확인되었습니다.",
                [
                    CitationReferenceV1(
                        kind="field_source",
                        reference_id=entry["reference_id"],
                    )
                ],
            )
        )
    return claims


def build_answer_experience_claim_catalog(
    result: AgentReviewResultV3,
) -> list[CanonicalExperienceClaimV1]:
    return [
        *build_canonical_experience_claim_catalog(result),
        *_field_fact_claims(result),
    ]


def build_canonical_experience_claim_catalog(
    result: AgentReviewResultV3,
) -> list[CanonicalExperienceClaimV1]:
    """Independently reconstruct the provider's versioned presentation claims."""

    meaning_catalog = provider_meaning_catalog()
    fact_entries = [
        item for item in result.fact_ledger.get("entries", []) if isinstance(item, dict)
    ]
    facts = {
        (item.get("kind"), item.get("reference_id")): item
        for item in fact_entries
        if isinstance(item.get("kind"), str) and isinstance(item.get("reference_id"), str)
    }
    outcome_entry = next(
        (
            item
            for item in fact_entries
            if item.get("kind") == "outcome" and isinstance(item.get("reference_id"), str)
        ),
        None,
    )
    if outcome_entry is None:
        raise ValueError("canonical result has no outcome FactLedger entry")
    outcome = CitationReferenceV1(
        kind="outcome",
        reference_id=outcome_entry["reference_id"],
    )
    claims: list[CanonicalExperienceClaimV1] = []
    if result.assessment.value == "no_issue_detected":
        conclusion = (
            "제출된 문서와 현재 조회된 법령 근거 범위에서는 별도의 불일치나 규정상 누락을 "
            "확인하지 못했습니다."
        )
    elif result.assessment.value == "issues_detected":
        conclusion = "문서 또는 규정 검토에서 담당자가 확인하고 보완해야 할 사항이 발견되었습니다."
    else:
        conclusion = (
            "현재 자료와 근거만으로 심사 결과를 확정하기 어려워 담당자의 추가 확인이 필요합니다."
        )
    claims.extend(
        [
            _canonical_claim("conclusion", "conclusion", conclusion, [outcome]),
            _canonical_claim(
                "conclusion",
                "limitation",
                (
                    "이 결과는 자동 통관 승인이 아니라 내부 사전심사 결과이며, 현재 "
                    "환경에서는 모든 심사 건을 담당자가 최종 확인합니다."
                ),
                [outcome],
            ),
            _canonical_claim(
                "process",
                "observation",
                (
                    "Agent는 문서 분석, 값 정규화, 문서 연결, 교차검증, 법령 근거 조회, "
                    "규정 검토와 결과 검증을 정해진 순서로 수행했습니다."
                ),
                [outcome],
            ),
        ]
    )
    if result.action_trace:
        succeeded = sum(
            isinstance(item, dict) and item.get("status") == "succeeded"
            for item in result.action_trace
        )
        deferred = sum(
            isinstance(item, dict) and item.get("status") == "deferred"
            for item in result.action_trace
        )
        claims.append(
            _canonical_claim(
                "process",
                "observation",
                (
                    f"제한형 추가 조사에서는 {succeeded}개 작업이 완료되었고 "
                    f"{deferred}개 작업은 안전 정책에 따라 담당자 확인으로 남겼습니다."
                ),
                [outcome],
            )
        )
    if result.validation_issues:
        for issue in result.validation_issues:
            code = issue.get("issue_code") if isinstance(issue, dict) else None
            reference = outcome.reference_id
            for (kind, reference_id), fact in facts.items():
                if kind == "issue" and isinstance(reference_id, str) and fact.get("label") == code:
                    reference = reference_id
                    break
            issue_meaning = meaning_catalog.issues.get(str(code))
            title, explanation, impact = (
                (
                    issue_meaning.title,
                    issue_meaning.explanation,
                    issue_meaning.impact,
                )
                if issue_meaning is not None
                else _DEFAULT_ISSUE_MEANING
            )
            citation = CitationReferenceV1(kind="issue", reference_id=reference)
            claims.extend(
                [
                    _canonical_claim(
                        "document_comparison",
                        "observation",
                        title + ".",
                        [citation],
                    ),
                    _canonical_claim(
                        "issues_and_causes",
                        "reason",
                        explanation,
                        [citation],
                    ),
                    _canonical_claim(
                        "business_impact",
                        "impact",
                        impact,
                        [citation],
                    ),
                ]
            )
    else:
        claims.extend(
            [
                _canonical_claim(
                    "document_comparison",
                    "observation",
                    "현재 검증 기준으로 문서 사이의 값 불일치는 발견되지 않았습니다.",
                    [outcome],
                ),
                _canonical_claim(
                    "issues_and_causes",
                    "observation",
                    "문서 교차검증에서 별도로 설명할 오류 원인은 확인되지 않았습니다.",
                    [outcome],
                ),
            ]
        )
    if result.regulatory_findings:
        for finding in result.regulatory_findings:
            code = finding.get("finding_code") if isinstance(finding, dict) else None
            reference = outcome.reference_id
            for (kind, reference_id), fact in facts.items():
                if (
                    kind == "finding"
                    and isinstance(reference_id, str)
                    and fact.get("label") == code
                ):
                    reference = reference_id
                    break
            finding_meaning = meaning_catalog.findings.get(str(code))
            explanation, impact = (
                (finding_meaning.explanation, finding_meaning.impact)
                if finding_meaning is not None
                else _DEFAULT_FINDING_MEANING
            )
            citation = CitationReferenceV1(kind="finding", reference_id=reference)
            claims.extend(
                [
                    _canonical_claim(
                        "issues_and_causes",
                        "reason",
                        explanation,
                        [citation],
                    ),
                    _canonical_claim(
                        "business_impact",
                        "impact",
                        impact,
                        [citation],
                    ),
                ]
            )
    evidence_claims = 0
    method_labels = {
        "exact_hs": "품목코드 구조 조회",
        "dense": "의미 유사도 검색",
        "bm25": "핵심어 검색",
        "graph": "법령 관계 확장",
    }
    for evidence in result.rag_evidence[:12]:
        if not isinstance(evidence, dict):
            continue
        evidence_id = evidence.get("evidence_id")
        if not isinstance(evidence_id, str) or ("evidence", evidence_id) not in facts:
            continue
        identity = " · ".join(
            str(value)
            for value in (
                evidence.get("law_name"),
                evidence.get("article_no"),
                evidence.get("title"),
            )
            if value
        )
        methods = evidence.get("retrieval_methods")
        observed_methods = (
            ", ".join(method_labels[item] for item in methods if item in method_labels)
            if isinstance(methods, list)
            else ""
        )
        text = f"현재 심사 건의 법령 근거로 {identity}을(를) 확인했습니다."
        if observed_methods:
            text += f" 이 근거는 {observed_methods} 경로에서 관측되었습니다."
        claims.append(
            _canonical_claim(
                "regulatory_meaning",
                "evidence",
                text,
                [CitationReferenceV1(kind="evidence", reference_id=evidence_id)],
            )
        )
        evidence_claims += 1
    if not evidence_claims:
        claims.append(
            _canonical_claim(
                "regulatory_meaning",
                "limitation",
                "현재 결과에 사용자에게 제시할 수 있는 법령 근거가 충분히 연결되지 않았습니다.",
                [outcome],
            )
        )
    if not result.validation_issues and not result.regulatory_findings:
        claims.append(
            _canonical_claim(
                "business_impact",
                "impact",
                (
                    "문서상 보완 항목은 없지만 로컬 검증 환경의 자동 처리 기능이 꺼져 있어 "
                    "담당자 확인 후 다음 업무를 진행해야 합니다."
                ),
                [outcome],
            )
        )
    for action in result.required_actions:
        citations: list[CitationReferenceV1] = []
        citation_kinds: tuple[CitationKind, ...] = (
            "field_source",
            "issue",
            "finding",
            "evidence",
            "outcome",
        )
        for reference_id in action.source_reference_ids:
            for kind in citation_kinds:
                if (kind, reference_id) in facts:
                    citations.append(CitationReferenceV1(kind=kind, reference_id=reference_id))
        claims.append(
            _canonical_claim(
                "next_actions",
                "action",
                _provider_action_text(meaning_catalog, action.action_code),
                citations or [outcome],
            )
        )
    if not result.required_actions:
        claims.append(
            _canonical_claim(
                "next_actions",
                "action",
                "담당자는 문서 대조 결과와 법령 근거를 확인하고 심사 의견을 기록합니다.",
                [outcome],
            )
        )
    claims.extend(
        [
            _canonical_claim(
                "scope_and_limits",
                "limitation",
                (
                    "현재 UAT는 텍스트 문서만 처리하며 이미지·PDF의 OCR 결과는 심사 "
                    "입력으로 받지 않습니다."
                ),
                [outcome],
            ),
            _canonical_claim(
                "scope_and_limits",
                "limitation",
                (
                    "법령 조회는 보존된 3개 법령 범위의 실제 RAG를 사용하므로 대한민국 "
                    "통관 법령 전체를 대표하지 않습니다."
                ),
                [outcome],
            ),
            _canonical_claim(
                "scope_and_limits",
                "limitation",
                (
                    "현재 결과는 법적 통관 승인이나 관세 당국의 최종 판단이 아니며 "
                    "Production 자동완료 기능은 꺼져 있습니다."
                ),
                [outcome],
            ),
        ]
    )
    if {item.section_code for item in claims} != set(_SECTION_ORDER):
        raise ValueError("canonical presentation claim catalog is incomplete")
    return claims


class PresentationStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str = Field(pattern=r"^statement_[a-f0-9]{64}$")
    role: Literal[
        "conclusion",
        "observation",
        "reason",
        "evidence",
        "impact",
        "action",
        "limitation",
    ]
    text: str = Field(min_length=1, max_length=4_000)
    source_claim_ids: list[str] = Field(min_length=1, max_length=20)
    citation_references: list[CitationReferenceV1] = Field(
        default_factory=list,
        max_length=100,
    )
    material: bool = True

    @field_validator("source_claim_ids")
    @classmethod
    def canonical_claim_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("presentation claim references must be unique")
        return values

    @model_validator(mode="after")
    def stable_identifier(self) -> "PresentationStatement":
        payload = self.model_dump(mode="json", exclude={"statement_id"})
        if self.statement_id != f"statement_{canonical_digest(payload)}":
            raise ValueError("presentation statement ID does not bind its contents")
        if self.material and not self.citation_references:
            raise ValueError("material presentation statements require citations")
        return self


class PresentationSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_code: Literal[
        "conclusion",
        "process",
        "document_comparison",
        "issues_and_causes",
        "regulatory_meaning",
        "business_impact",
        "next_actions",
        "scope_and_limits",
    ]
    title: str = Field(min_length=1, max_length=120)
    statements: list[PresentationStatement] = Field(min_length=1, max_length=100)


class GroundedPresentationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    headline: str = Field(min_length=1, max_length=300)
    sections: list[PresentationSection] = Field(min_length=8, max_length=8)
    generation_audit: ExperienceGenerationAuditV1
    verification_audit: ExperienceVerificationAuditV1
    presentation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def complete_and_content_bound(self) -> "GroundedPresentationV1":
        if tuple(section.section_code for section in self.sections) != _SECTION_ORDER:
            raise ValueError("presentation sections must be complete and canonically ordered")
        payload = self.model_dump(mode="json", exclude={"presentation_sha256"})
        if self.presentation_sha256 != canonical_digest(payload):
            raise ValueError("presentation SHA-256 does not bind its contents")
        return self


class AgentUatCapabilityAttestationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(min_length=1, max_length=256)
    environment: Literal["local_uat"]
    evidence_profile: Literal["preserved-3law-v3-scenario-eval"]
    loopback_only: Literal[True]
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    agent_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    agent_code_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_id: str = Field(min_length=1, max_length=128)
    prompt_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_registry_id: str = Field(min_length=1, max_length=128)
    tool_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_runtime_v3_sha256: str = Field(pattern=_SHA256_PATTERN)
    max_cycles: Literal[2]
    max_tool_calls: Literal[6]
    max_rereads_per_field: Literal[1]
    adaptive_active: bool
    auto_completion_enabled: Literal[False]
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def valid_window_and_digest(self) -> "AgentUatCapabilityAttestationV1":
        lifetime_seconds = (self.expires_at - self.issued_at).total_seconds()
        if lifetime_seconds <= 0 or lifetime_seconds > 24 * 60 * 60:
            raise ValueError("UAT capability lifetime must be within 24 hours")
        payload = self.model_dump(mode="json", exclude={"capability_sha256"})
        if self.capability_sha256 != canonical_digest(payload):
            raise ValueError("UAT capability SHA-256 does not bind its contents")
        return self


class UserActionEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(pattern=r"^user_action_[a-f0-9]{64}$")
    action_code: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2_000)
    status: Literal["completed", "needs_attention", "deferred"]
    source_reference_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def stable_identifier(self) -> "UserActionEventV1":
        payload = self.model_dump(mode="json", exclude={"event_id"})
        if self.event_id != f"user_action_{canonical_digest(payload)}":
            raise ValueError("user action event ID does not bind its contents")
        return self


class AgentReviewStageEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    sequence: int = Field(ge=0, le=10_000)
    stage_code: Literal[
        "request_validation",
        "document_analysis",
        "normalization",
        "document_pairing",
        "cross_validation",
        "adaptive_investigation",
        "evidence_retrieval",
        "compliance_review",
        "report_composition",
        "grounding_verification",
        "result_validation",
        "conversation_binding",
        "answer_planning",
        "answer_composition",
        "completed",
        "failed",
    ]
    state: Literal["started", "completed", "skipped", "failed"]
    occurred_at: AwareDatetime
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    safe_counts: dict[str, int] = Field(default_factory=dict)
    message_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    event_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def stable_digest(self) -> "AgentReviewStageEventV1":
        if len(self.safe_counts) > 20 or any(
            key not in _SAFE_STAGE_COUNT_KEYS or value < 0 or value > 1_000_000
            for key, value in self.safe_counts.items()
        ):
            raise ValueError("Agent stage safe counts are outside the bounded range")
        if self.message_key != f"agent.stage.{self.stage_code}.{self.state}":
            raise ValueError("Agent stage message key is outside the public catalog")
        if self.state == "started" and self.duration_ms is not None:
            raise ValueError("started stage events cannot claim a duration")
        payload = self.model_dump(mode="json", exclude={"event_sha256"})
        if self.event_sha256 != canonical_digest(payload):
            raise ValueError("Agent stage SHA-256 does not bind its contents")
        return self


AgentAnswerStageEventV1 = AgentReviewStageEventV1


class ReviewExperienceEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    canonical_result: AgentReviewResultV3
    user_presentation: GroundedPresentationV1
    capability_attestation: AgentUatCapabilityAttestationV1
    user_action_events: list[UserActionEventV1] = Field(default_factory=list, max_length=100)
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def content_bound(self) -> "ReviewExperienceEnvelopeV1":
        payload = self.model_dump(mode="json", exclude={"envelope_sha256"})
        if self.envelope_sha256 != canonical_digest(payload):
            raise ValueError("experience envelope SHA-256 does not bind its contents")
        return self


class PublicReviewExperienceV1(BaseModel):
    """Browser-safe projection; the full canonical Agent result remains private."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    bound_provider_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    bound_effective_result_digest: str = Field(pattern=_SHA256_PATTERN)
    user_presentation: GroundedPresentationV1
    capability_attestation: AgentUatCapabilityAttestationV1
    user_action_events: list[UserActionEventV1] = Field(default_factory=list, max_length=100)
    agent_envelope_sha256: str = Field(pattern=_SHA256_PATTERN)
    public_experience_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def public_projection_is_bound(self) -> "PublicReviewExperienceV1":
        payload = self.model_dump(mode="json", exclude={"public_experience_sha256"})
        if self.public_experience_sha256 != canonical_digest(payload):
            raise ValueError("public experience SHA-256 does not bind its contents")
        return self


def public_review_experience(
    envelope: ReviewExperienceEnvelopeV1,
    *,
    effective_result_digest: str,
) -> PublicReviewExperienceV1:
    payload = {
        "schema_version": "1.0",
        "bound_provider_result_sha256": envelope.canonical_result.result_sha256,
        "bound_effective_result_digest": effective_result_digest,
        "user_presentation": envelope.user_presentation.model_dump(mode="json"),
        "capability_attestation": envelope.capability_attestation.model_dump(mode="json"),
        "user_action_events": [
            item.model_dump(mode="json") for item in envelope.user_action_events
        ],
        "agent_envelope_sha256": envelope.envelope_sha256,
    }
    return PublicReviewExperienceV1.model_validate(
        {
            **payload,
            "public_experience_sha256": canonical_digest(payload),
        }
    )


class ReviewPresentationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presentation_id: str = Field(pattern=r"^presentation_[a-f0-9]{64}$")
    review_run_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    bound_provider_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    bound_effective_result_digest: str = Field(pattern=_SHA256_PATTERN)
    envelope: ReviewExperienceEnvelopeV1
    created_at: datetime

    @model_validator(mode="after")
    def binding_is_complete(self) -> "ReviewPresentationRecord":
        if (
            self.bound_provider_result_sha256 != self.envelope.canonical_result.result_sha256
            or self.presentation_id
            != f"presentation_{canonical_digest({'envelope_sha256': self.envelope.envelope_sha256})}"
        ):
            raise ValueError("stored presentation is not bound to its canonical result")
        return self


class ReviewExperienceEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_as_of: date | None = None
    trade_type: Literal["import", "export"] = "import"
    submitted_document_codes: list[str] = Field(default_factory=list, max_length=0)


class ReviewEta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["observed", "validated_default"]
    sample_size: int = Field(ge=0, le=30)
    p50_seconds: int | None = Field(default=None, ge=0)
    p90_seconds: int | None = Field(default=None, ge=0)
    range_min_seconds: int = Field(ge=0)
    range_max_seconds: int = Field(ge=0)


class JobExperienceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    case_id: str
    kind: Literal["review", "revision"]
    parent_run_id: str | None = None
    status: JobStatus
    review_run_id: str
    error_code: str | None = None
    attempt_count: int
    agent_call_state: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    elapsed_seconds: int = Field(ge=0)
    eta: ReviewEta


class ConversationCreateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bound_result_sha256: str = Field(pattern=_SHA256_PATTERN)


class AssistantMessageCreateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    parent_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        pattern=_IDENTIFIER_PATTERN,
    )
    bound_result_sha256: str = Field(pattern=_SHA256_PATTERN)


class AssistantConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(pattern=r"^conversation_[a-f0-9]{64}$")
    review_run_id: str
    case_id: str
    org_id: str
    bound_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    bound_effective_result_digest: str = Field(pattern=_SHA256_PATTERN)
    created_by: str
    created_at: datetime
    updated_at: datetime


class ConversationTurnV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    parent_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)
    created_at: AwareDatetime
    answer_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def answer_digest_matches_role(self) -> "ConversationTurnV2":
        if (self.role == "assistant") != (self.answer_sha256 is not None):
            raise ValueError("only assistant turns require the verified answer digest")
        return self


class CaseConversationRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    conversation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    parent_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    question: str = Field(min_length=1, max_length=2_000)
    bound_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    conversation_context_sha256: str = Field(pattern=_SHA256_PATTERN)
    recent_turns: list[ConversationTurnV2] = Field(default_factory=list, max_length=8)
    review_result: AgentReviewResultV3
    locale: Literal["ko-KR"] = "ko-KR"

    @model_validator(mode="after")
    def backend_history_and_result_are_bound(self) -> "CaseConversationRequestV2":
        message_ids = [item.message_id for item in self.recent_turns]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("conversation context message IDs must be unique")
        if self.parent_message_id != (message_ids[-1] if message_ids else None):
            raise ValueError("conversation parent must be the latest Backend-provided turn")
        for previous, current in zip(self.recent_turns, self.recent_turns[1:], strict=False):
            if current.parent_message_id != previous.message_id:
                raise ValueError("conversation context is not one linear message chain")
        expected_context = canonical_digest(
            [item.model_dump(mode="json") for item in self.recent_turns]
        )
        if self.conversation_context_sha256 != expected_context:
            raise ValueError("conversation context SHA-256 is inconsistent")
        if self.review_result.result_sha256 != self.bound_result_sha256:
            raise ValueError("conversation result binding is stale")
        return self


_ANSWER_VERIFIED_CONTENT_FIELDS = (
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


def assistant_answer_verified_content_sha256(values: dict[str, Any]) -> str:
    """Hash all user-visible and lineage-bearing answer values."""

    return canonical_digest({field: values[field] for field in _ANSWER_VERIFIED_CONTENT_FIELDS})


class AnswerClaimSelectionsV1(BaseModel):
    """Canonical claim IDs selected for each user-visible answer area."""

    model_config = ConfigDict(extra="forbid")

    direct_answer: list[str] = Field(default_factory=list, max_length=1)
    explanation: list[str] = Field(default_factory=list, max_length=40)
    evidence_meaning: list[str] = Field(default_factory=list, max_length=40)
    impact: list[str] = Field(default_factory=list, max_length=40)
    next_steps: list[str] = Field(default_factory=list, max_length=40)

    @field_validator(
        "direct_answer",
        "explanation",
        "evidence_meaning",
        "impact",
        "next_steps",
    )
    @classmethod
    def claim_ids_are_unique_and_typed(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"experience_claim_[a-f0-9]{64}", item) is None for item in value
        ):
            raise ValueError("answer claim selections contain an invalid claim ID")
        return value


class AssistantAnswerV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    answer_scope: Literal[
        "case_grounded",
        "evidence_grounded",
        "general_customs_guidance",
        "clarification_needed",
        "insufficient_evidence",
        "out_of_scope",
    ]
    status: Literal["answered", "clarification_needed", "insufficient_evidence", "out_of_scope"]
    direct_answer: str = Field(min_length=1, max_length=4_000)
    explanation: list[str] = Field(default_factory=list, max_length=20)
    evidence_meaning: list[str] = Field(default_factory=list, max_length=20)
    impact: list[str] = Field(default_factory=list, max_length=20)
    next_steps: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    claim_selections: AnswerClaimSelectionsV1 = Field(default_factory=AnswerClaimSelectionsV1)
    citations: list[CitationReferenceV1] = Field(default_factory=list, max_length=100)
    suggested_questions: list[str] = Field(default_factory=list, max_length=3)
    used_context_message_ids: list[str] = Field(default_factory=list, max_length=8)
    generation_audit: ExperienceGenerationAuditV1
    verification_audit: ExperienceVerificationAuditV1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    answer_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def stable_digest(self) -> "AssistantAnswerV2":
        if len(self.used_context_message_ids) != len(set(self.used_context_message_ids)):
            raise ValueError("used context message IDs must be unique")
        expected_status = {
            "case_grounded": "answered",
            "evidence_grounded": "answered",
            "general_customs_guidance": "answered",
            "clarification_needed": "clarification_needed",
            "insufficient_evidence": "insufficient_evidence",
            "out_of_scope": "out_of_scope",
        }[self.answer_scope]
        if self.status != expected_status:
            raise ValueError("assistant answer status is inconsistent with its scope")
        if (
            self.generation_audit.scope_classification is None
            or self.generation_audit.scope_classification.classified_scope != self.answer_scope
        ):
            raise ValueError("assistant answer is missing its exact scope-classification audit")
        if self.answer_scope in {"case_grounded", "evidence_grounded"} and not self.citations:
            raise ValueError("case and Evidence answers require citations")
        if self.status in {"clarification_needed", "out_of_scope"} and self.citations:
            raise ValueError("clarification and out-of-scope answers cannot claim citations")
        values = self.model_dump(mode="json")
        if (
            self.verification_audit.verified_content_sha256
            != assistant_answer_verified_content_sha256(values)
        ):
            raise ValueError("assistant verifier receipt is not bound to its visible content")
        payload = self.model_dump(mode="json", exclude={"answer_sha256"})
        if self.answer_sha256 != canonical_digest(payload):
            raise ValueError("assistant answer SHA-256 does not bind its contents")
        return self


class AssistantMessageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(pattern=r"^assistant_message_[a-f0-9]{64}$")
    conversation_id: str
    review_run_id: str
    case_id: str
    org_id: str
    parent_message_id: str | None = None
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)
    structured_answer: AssistantAnswerV2 | None = None
    bound_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def role_payload_matches(self) -> "AssistantMessageRecord":
        if (self.role == "assistant") != (self.structured_answer is not None):
            raise ValueError("only assistant messages contain a structured answer")
        if (
            self.structured_answer is not None
            and self.content != self.structured_answer.direct_answer
        ):
            raise ValueError("assistant message content must equal the verified direct answer")
        return self


class AssistantJobState(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    delivery_uncertain = "delivery_uncertain"
    response_received_rejected = "response_received_rejected"
    response_persistence_uncertain = "response_persistence_uncertain"


class AssistantStageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=r"^assistant_event_[a-f0-9]{64}$")
    assistant_job_id: str
    sequence: int = Field(ge=1, le=100)
    stage_code: AssistantStageCode
    state: Literal["started", "completed", "skipped", "failed"]
    occurred_at: AwareDatetime
    duration_ms: int = Field(ge=0, le=3_600_000)
    safe_counts: dict[str, int] = Field(default_factory=dict)
    message_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def stable_identifier(self) -> "AssistantStageEvent":
        payload = self.model_dump(mode="json", exclude={"event_id"})
        if self.event_id != f"assistant_event_{canonical_digest(payload)}":
            raise ValueError("assistant event ID does not bind its contents")
        if any(value < 0 or value > 1_000_000 for value in self.safe_counts.values()):
            raise ValueError("assistant event safe counts are outside the bounded range")
        return self


class AssistantJobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_job_id: str = Field(pattern=r"^assistant_job_[a-f0-9]{64}$")
    conversation_id: str
    review_run_id: str
    case_id: str
    org_id: str
    user_message_id: str
    idempotency_key: str = Field(min_length=1, max_length=512)
    idempotency_request_digest: str = Field(pattern=_SHA256_PATTERN)
    agent_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    request: dict[str, Any]
    state: AssistantJobState = AssistantJobState.queued
    events: list[AssistantStageEvent] = Field(default_factory=list, max_length=100)
    answer_message_id: str | None = None
    error_code: str | None = Field(default=None, max_length=256)
    call_started_at: datetime | None = None
    lease_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    lease_expires_at: datetime | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def state_is_complete(self) -> "AssistantJobRecord":
        if self.state == AssistantJobState.queued and any(
            value is not None
            for value in (self.call_started_at, self.lease_id, self.lease_expires_at)
        ):
            raise ValueError("queued assistant job cannot have a call timestamp or lease")
        if self.state != AssistantJobState.queued and self.call_started_at is None:
            raise ValueError("started assistant job requires a call timestamp")
        if self.state == AssistantJobState.running:
            if (
                self.call_started_at is None
                or self.lease_id is None
                or self.lease_expires_at is None
            ):
                raise ValueError("running assistant job requires an exact call-once lease")
            if self.lease_expires_at <= self.call_started_at:
                raise ValueError("assistant job lease must expire after the call starts")
        elif self.lease_id is not None or self.lease_expires_at is not None:
            raise ValueError("terminal assistant job cannot retain its call-once lease")
        if self.state == AssistantJobState.succeeded:
            if self.answer_message_id is None or self.error_code is not None:
                raise ValueError("successful assistant job requires only an answer message")
        elif self.answer_message_id is not None:
            raise ValueError("non-successful assistant job cannot expose an answer")
        return self


class AssistantJobView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_job_id: str
    request_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    conversation_id: str
    review_run_id: str
    case_id: str
    state: AssistantJobState
    events: list[AssistantStageEvent]
    answer_message_id: str | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class AssistantJobEnqueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: AssistantJobView
    user_message: AssistantMessageRecord
    idempotency_reused: bool


def claim_catalog(result: AgentReviewResultV3) -> tuple[set[str], set[tuple[str, str]]]:
    payload = result.model_dump(mode="json")
    claim_ids: set[str] = set()
    references: set[tuple[str, str]] = set()
    for entry in payload.get("fact_ledger", {}).get("entries", []):
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        reference_id = entry.get("reference_id")
        fact_id = entry.get("fact_id")
        if isinstance(kind, str) and isinstance(reference_id, str):
            references.add((kind, reference_id))
            claim_ids.add(reference_id)
        if isinstance(fact_id, str):
            claim_ids.add(fact_id)
    for key in ("validation_issues", "regulatory_findings", "rag_evidence", "action_trace"):
        for item in payload.get(key, []):
            if not isinstance(item, dict):
                continue
            for id_key in (
                "issue_id",
                "finding_id",
                "evidence_id",
                "action_id",
                "observation_id",
            ):
                value = item.get(id_key)
                if isinstance(value, str):
                    claim_ids.add(value)
    for section in (
        "summary",
        "document_consistency",
        "regulatory_review",
        "required_actions",
    ):
        for claim in payload.get("narrative_report", {}).get(section, []):
            if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str):
                claim_ids.add(claim["claim_id"])
    for gap in payload.get("review_gaps", []):
        if isinstance(gap, dict) and isinstance(gap.get("gap_id"), str):
            claim_ids.add(gap["gap_id"])
    for action in payload.get("required_actions", []):
        if isinstance(action, dict) and isinstance(action.get("action_code"), str):
            claim_ids.add(action["action_code"])
    return claim_ids, references


def _expected_presentation_headline(result: AgentReviewResultV3) -> str:
    if result.assessment.value == "no_issue_detected":
        return "문서와 법령 근거 범위에서 별도의 문제를 확인하지 못했습니다"
    if result.assessment.value == "issues_detected":
        return "담당자가 확인하고 보완해야 할 사항이 발견되었습니다"
    return "현재 근거만으로 확정하기 어려워 담당자 확인이 필요합니다"


def _expected_user_action_events(result: AgentReviewResultV3) -> list[UserActionEventV1]:
    catalog = provider_meaning_catalog()
    events: list[UserActionEventV1] = []
    for action in result.required_actions:
        payload = {
            "schema_version": "1.0",
            "action_code": action.action_code,
            "title": "담당자 확인이 필요합니다",
            "description": _provider_action_text(catalog, action.action_code),
            "status": "needs_attention",
            "source_reference_ids": action.source_reference_ids,
        }
        events.append(
            UserActionEventV1.model_validate(
                {
                    "event_id": f"user_action_{canonical_digest(payload)}",
                    **payload,
                }
            )
        )
    trace_titles = {
        "document.reread_span/v1": "문서 값을 다시 확인했습니다",
        "evidence.retrieve/v3": "법령 근거를 다시 조회했습니다",
        "human.request/v1": "담당자 확인을 요청했습니다",
    }
    for trace in result.action_trace:
        tool_name = trace.get("tool_name")
        if tool_name not in trace_titles:
            continue
        status = trace.get("status")
        payload = {
            "schema_version": "1.0",
            "action_code": tool_name,
            "title": trace_titles[tool_name],
            "description": (
                "안전하게 허용된 범위에서 추가 확인을 수행했습니다."
                if status == "succeeded"
                else "자동 확인을 완료하지 못해 담당자 검토로 남겼습니다."
            ),
            "status": (
                "completed"
                if status == "succeeded"
                else ("deferred" if status == "deferred" else "needs_attention")
            ),
            "source_reference_ids": trace.get("input_reference_ids", []),
        }
        events.append(
            UserActionEventV1.model_validate(
                {
                    "event_id": f"user_action_{canonical_digest(payload)}",
                    **payload,
                }
            )
        )
    return events


def _validate_presentation_audit(
    envelope: ReviewExperienceEnvelopeV1,
    *,
    statement_count: int,
) -> None:
    presentation = envelope.user_presentation
    generation = presentation.generation_audit
    verification = presentation.verification_audit
    lock = validate_agent_experience_contract_bundle().lock
    prompts = lock.prompt_identities
    schemas = lock.output_schema_identities
    verified_content_sha256 = canonical_digest(
        {
            "headline": presentation.headline,
            "sections": [section.model_dump(mode="json") for section in presentation.sections],
        }
    )
    canonical_claims = build_canonical_experience_claim_catalog(envelope.canonical_result)
    verifier_context_sha256 = canonical_digest(
        {
            "canonical_claims": [claim.model_dump(mode="json") for claim in canonical_claims],
            "candidate_statements": [
                statement.model_dump(mode="json")
                for section in presentation.sections
                for statement in section.statements
            ],
        }
    )
    if (
        generation.task_id != _PRESENTATION_TASK_ID
        or generation.prompt_sha256 != prompts["presentation"]
        or generation.output_schema_sha256 != schemas["presentation"]
        or verification.verifier_prompt_sha256 != prompts["entailment"]
        or verification.verifier_schema_sha256 != schemas["entailment"]
        or verification.checked_statement_count != statement_count
        or verification.verified_content_sha256 != verified_content_sha256
        or not verification.deterministic_checks_passed
    ):
        raise ValueError("presentation generation or verification identity is not pinned")
    if generation.mode == "claude":
        if (
            generation.model_id != envelope.capability_attestation.model_id
            or verification.status != "passed"
            or verification.entailment_verifier != "claude"
            or verification.verifier_model_id != envelope.capability_attestation.model_id
            or verification.verifier_context_sha256 != verifier_context_sha256
            or verification.reason_codes
        ):
            raise ValueError("Claude presentation audit is incomplete")
    elif (
        generation.model_id is not None
        or verification.status != "fallback"
        or verification.entailment_verifier != "not_run"
        or verification.verifier_model_id is not None
        or verification.verifier_context_sha256 is not None
        or not verification.reason_codes
    ):
        raise ValueError("deterministic presentation fallback audit is inconsistent")


def validate_presentation_against_result(envelope: ReviewExperienceEnvelopeV1) -> None:
    result = envelope.canonical_result
    capability = envelope.capability_attestation
    runtime = result.runtime_release
    contract_lock = validate_agent_experience_contract_bundle().lock
    if (
        capability.agent_commit != runtime.agent_git_sha
        or capability.agent_code_snapshot_sha256 != runtime.agent_code_snapshot_sha256
        or capability.model_id != runtime.model_id
        or capability.tool_registry_id != runtime.tool_registry_id
        or capability.evidence_runtime_v3_sha256 != runtime.evidence_runtime_v3_sha256
        or capability.auto_completion_enabled is not False
        or capability.agent_commit != contract_lock.provider_commit
        or capability.prompt_bundle_sha256 != contract_lock.experience_prompt_bundle_sha256
        or capability.schema_bundle_sha256 != contract_lock.experience_schema_bundle_sha256
        or capability.tool_registry_id != contract_lock.uat_tool_registry_id
        or capability.tool_registry_sha256 != contract_lock.uat_tool_registry_sha256
    ):
        raise ValueError("experience capability does not bind the canonical runtime")
    _result_claim_ids, references = claim_catalog(result)
    expected_claims = build_canonical_experience_claim_catalog(result)
    claim_index = {item.claim_id: item for item in expected_claims}
    presentation_options = _presentation_statement_options(expected_claims)
    statement_count = sum(
        len(section.statements) for section in envelope.user_presentation.sections
    )
    _validate_presentation_audit(envelope, statement_count=statement_count)
    if envelope.user_presentation.headline != _expected_presentation_headline(result):
        raise ValueError("presentation headline differs from the deterministic outcome label")
    used_claims: set[str] = set()
    for section in envelope.user_presentation.sections:
        if section.title != _SECTION_TITLES[section.section_code]:
            raise ValueError("presentation section title differs from the provider catalog")
        for statement in section.statements:
            claims = [claim_index.get(claim_id) for claim_id in statement.source_claim_ids]
            if any(claim is None for claim in claims):
                raise ValueError("presentation cites an unknown canonical experience claim")
            bound_claims = [claim for claim in claims if claim is not None]
            if any(
                claim.section_code != section.section_code or claim.role != statement.role
                for claim in bound_claims
            ):
                raise ValueError("presentation claim section or role is inconsistent")
            if used_claims.intersection(statement.source_claim_ids):
                raise ValueError("presentation reuses a canonical experience claim")
            used_claims.update(statement.source_claim_ids)
            expected_citations = list(
                {
                    (citation.kind, citation.reference_id): citation
                    for claim in bound_claims
                    for citation in claim.citation_references
                }.values()
            )
            if statement.citation_references != expected_citations:
                raise ValueError("presentation citations differ from canonical claims")
            for citation in statement.citation_references:
                if citation.kind not in _CITATION_KINDS:
                    raise ValueError("presentation citation kind is unsupported")
                if (citation.kind, citation.reference_id) not in references:
                    raise ValueError("presentation citation is outside the canonical FactLedger")
            if envelope.user_presentation.generation_audit.mode == "deterministic_fallback":
                if len(bound_claims) != 1 or statement.text != bound_claims[0].canonical_text:
                    raise ValueError("deterministic presentation fallback differs from its claim")
            else:
                expected_group = presentation_options.get(tuple(statement.source_claim_ids))
                if expected_group is None:
                    raise ValueError(
                        "presentation Claim grouping differs from the provider contract"
                    )
                blockers = _grounded_prose_blockers(
                    statement.text,
                    [claim.canonical_text for claim in expected_group],
                    prefix="presentation",
                    minimum_chars=_PRESENTATION_MIN_MATERIAL_STATEMENT_CHARS,
                )
                if blockers:
                    raise ValueError(
                        "presentation prose failed deterministic grounding: " + ",".join(blockers)
                    )
    if used_claims != set(claim_index):
        raise ValueError("presentation canonical claim coverage is incomplete")
    if envelope.user_presentation.generation_audit.mode == "claude":
        section_lengths = {
            section.section_code: len(
                " ".join(statement.text for statement in section.statements).strip()
            )
            for section in envelope.user_presentation.sections
        }
        if any(length < _PRESENTATION_MIN_SECTION_CHARS for length in section_lengths.values()):
            raise ValueError("Claude presentation contains an underdeveloped section")
        material_text = " ".join(
            statement.text
            for section in envelope.user_presentation.sections
            for statement in section.statements
            if statement.material
        )
        if len(material_text) < _PRESENTATION_MIN_TOTAL_MATERIAL_CHARS:
            raise ValueError("Claude presentation is too short for the user experience contract")
        if _korean_letter_ratio(material_text) < _PRESENTATION_MIN_KOREAN_RATIO:
            raise ValueError("Claude presentation does not meet the Korean-language quality gate")
    allowed_action_sources = _result_claim_ids | {
        reference_id for _kind, reference_id in references
    }
    if any(
        not set(event.source_reference_ids).issubset(allowed_action_sources)
        for event in envelope.user_action_events
    ):
        raise ValueError("user action references are outside the canonical result")
    if envelope.user_action_events != _expected_user_action_events(result):
        raise ValueError("user action copy differs from the provider meaning catalog")


def build_deterministic_review_experience(
    *,
    result: AgentReviewResultV3,
    capability_attestation: AgentUatCapabilityAttestationV1,
    reason_code: str,
) -> ReviewExperienceEnvelopeV1:
    """Create a grounded fallback presentation for an approved child revision."""

    claims = build_canonical_experience_claim_catalog(result)
    sections = _build_deterministic_fallback_sections(claims)
    lock = validate_agent_experience_contract_bundle().lock
    headline = _expected_presentation_headline(result)
    verified_content_sha256 = canonical_digest(
        {
            "headline": headline,
            "sections": [section.model_dump(mode="json") for section in sections],
        }
    )
    presentation_payload = {
        "schema_version": "1.0",
        "headline": headline,
        "sections": [section.model_dump(mode="json") for section in sections],
        "generation_audit": {
            "mode": "deterministic_fallback",
            "task_id": _PRESENTATION_TASK_ID,
            "model_id": None,
            "prompt_sha256": lock.prompt_identities["presentation"],
            "output_schema_sha256": lock.output_schema_identities["presentation"],
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0,
            "attempt_count": 1,
            "scope_classification": None,
        },
        "verification_audit": {
            "status": "fallback",
            "deterministic_checks_passed": True,
            "entailment_verifier": "not_run",
            "verifier_model_id": None,
            "verifier_prompt_sha256": lock.prompt_identities["entailment"],
            "verifier_schema_sha256": lock.output_schema_identities["entailment"],
            "verifier_context_sha256": None,
            "checked_statement_count": sum(len(section.statements) for section in sections),
            "verified_content_sha256": verified_content_sha256,
            "reason_codes": [reason_code],
        },
    }
    presentation = GroundedPresentationV1.model_validate(
        {
            **presentation_payload,
            "presentation_sha256": canonical_digest(presentation_payload),
        }
    )
    envelope_payload = {
        "schema_version": "1.0",
        "canonical_result": result.model_dump(mode="json"),
        "user_presentation": presentation.model_dump(mode="json"),
        "capability_attestation": capability_attestation.model_dump(mode="json"),
        "user_action_events": [
            item.model_dump(mode="json") for item in _expected_user_action_events(result)
        ],
    }
    envelope = ReviewExperienceEnvelopeV1.model_validate(
        {
            **envelope_payload,
            "envelope_sha256": canonical_digest(envelope_payload),
        }
    )
    validate_presentation_against_result(envelope)
    return envelope


def _expected_fallback_answer_fields(
    *,
    answer: AssistantAnswerV2,
    result: AgentReviewResultV3,
    expected_context_message_ids: list[str],
) -> dict[str, Any]:
    classification = answer.generation_audit.scope_classification
    if classification is None:
        raise ValueError("assistant fallback omitted scope-classification audit")
    scope = answer.answer_scope
    claims = build_answer_experience_claim_catalog(result)
    guidance: dict[str, str] = {}
    if scope == "evidence_grounded":
        claims = [item for item in claims if item.section_code == "regulatory_meaning"]
    elif scope == "case_grounded":
        pass
    elif scope == "general_customs_guidance":
        catalog = provider_meaning_catalog()
        try:
            guidance = {
                topic: catalog.general_guidance[topic] for topic in classification.guidance_topics
            }
        except KeyError as exc:
            raise ValueError("assistant guidance topic is outside the provider catalog") from exc
        claims = []
    else:
        claims = []
    claims = claims[:100]
    selected_claims: list[CanonicalExperienceClaimV1] = []
    claim_selections: dict[str, list[str]] = {
        "direct_answer": [],
        "explanation": [],
        "evidence_meaning": [],
        "impact": [],
        "next_steps": [],
    }
    if scope == "out_of_scope":
        status = "out_of_scope"
        direct = (
            "이 요청은 통관 심사 지원 범위에서 답변할 수 없습니다. 현재 심사 건의 문서, "
            "법령 근거, 보완 조치 또는 일반 통관 절차를 질문해 주십시오."
        )
        explanation: list[str] = []
    elif scope == "clarification_needed":
        status = "clarification_needed"
        direct = (
            "질문의 대상을 조금 더 구체적으로 알려주시면 현재 심사 결과에 맞추어 설명하겠습니다."
        )
        explanation = [
            "예를 들어 발견된 문제의 원인, 적용된 법령 근거, 업무상 영향 또는 다음 조치 중 무엇을 "
            "확인하려는지 알려주십시오."
        ]
    elif scope == "insufficient_evidence":
        status = "insufficient_evidence"
        direct = (
            "현재 심사 결과에는 이 질문에 답할 수 있는 충분한 법령 근거가 연결되어 있지 않습니다."
        )
        explanation = ["추가 법령 범위 또는 담당자 확인이 필요합니다."]
    elif scope == "general_customs_guidance":
        if not guidance:
            raise ValueError("assistant general-guidance fallback has no approved topic")
        status = "answered"
        direct = next(iter(guidance.values()))
        explanation = list(guidance.values())[1:]
    else:
        if not claims:
            raise ValueError("assistant Case fallback has no canonical claim")
        status = "answered"
        direct_claim = claims[0]
        explanation_claims = [
            item for item in claims[1:] if item.role in _ANSWER_AREA_ROLES["explanation"]
        ][:5]
        direct = direct_claim.canonical_text
        explanation = [item.canonical_text for item in explanation_claims]
        claim_selections["direct_answer"] = [direct_claim.claim_id]
        claim_selections["explanation"] = [item.claim_id for item in explanation_claims]
        selected_claims.extend([direct_claim, *explanation_claims])
    evidence_claims = (
        [item for item in claims if item.role in _ANSWER_AREA_ROLES["evidence_meaning"]][:5]
        if status == "answered" and scope != "general_customs_guidance"
        else []
    )
    impact_claims = (
        [item for item in claims if item.role in _ANSWER_AREA_ROLES["impact"]][:5]
        if status == "answered" and scope != "general_customs_guidance"
        else []
    )
    action_claims = (
        [item for item in claims if item.role in _ANSWER_AREA_ROLES["next_steps"]][:5]
        if status == "answered" and scope != "general_customs_guidance"
        else []
    )
    selected_claims.extend([*evidence_claims, *impact_claims, *action_claims])
    claim_selections["evidence_meaning"] = [item.claim_id for item in evidence_claims]
    claim_selections["impact"] = [item.claim_id for item in impact_claims]
    claim_selections["next_steps"] = [item.claim_id for item in action_claims]
    evidence_meaning = [item.canonical_text for item in evidence_claims]
    impact = [item.canonical_text for item in impact_claims]
    next_steps = [item.canonical_text for item in action_claims]
    citations = list(
        {
            (citation.kind, citation.reference_id): citation
            for claim in selected_claims
            for citation in claim.citation_references
        }.values()
    )
    limitations = [
        (_GENERAL_GUIDANCE_LIMITATION if scope == "general_customs_guidance" else _CASE_LIMITATION)
    ]
    return {
        "status": status,
        "direct_answer": direct,
        "explanation": explanation,
        "evidence_meaning": evidence_meaning,
        "impact": impact,
        "next_steps": next_steps,
        "limitations": limitations,
        "claim_selections": AnswerClaimSelectionsV1.model_validate(claim_selections),
        "citations": citations if status == "answered" else [],
        "suggested_questions": _SAFE_SUGGESTED_QUESTIONS,
        "used_context_message_ids": expected_context_message_ids,
    }


def validate_answer_against_context(
    *,
    answer: AssistantAnswerV2,
    request: CaseConversationRequestV2,
) -> None:
    result = request.review_result
    expected_context_message_ids = [item.message_id for item in request.recent_turns]
    expected_request_sha256 = canonical_digest(request.model_dump(mode="json"))
    permitted_context_message_ids = set(expected_context_message_ids)
    _claim_ids, references = claim_catalog(result)
    if not set(answer.used_context_message_ids).issubset(permitted_context_message_ids):
        raise ValueError("assistant answer used conversation history outside the Backend window")
    for citation in answer.citations:
        if citation.kind not in _CITATION_KINDS:
            raise ValueError("assistant citation kind is unsupported")
        if (citation.kind, citation.reference_id) not in references:
            raise ValueError("assistant citation is outside the canonical FactLedger")
    material_scopes = {"case_grounded", "evidence_grounded"}
    if answer.answer_scope in material_scopes and not answer.citations:
        raise ValueError("case or Evidence answer requires canonical citations")
    lock = validate_agent_experience_contract_bundle().lock
    prompts = lock.prompt_identities
    schemas = lock.output_schema_identities
    generation = answer.generation_audit
    verification = answer.verification_audit
    classification = generation.scope_classification
    expected_claude_count = (
        1
        + len(answer.explanation)
        + len(answer.evidence_meaning)
        + len(answer.impact)
        + len(answer.next_steps)
        + len(answer.limitations)
    )
    expected_fallback_count = (
        1
        + len(answer.explanation)
        + len(answer.evidence_meaning)
        + len(answer.impact)
        + len(answer.next_steps)
        + len(answer.limitations)
    )
    verified_content_sha256 = canonical_digest(
        {
            "request_sha256": answer.request_sha256,
            "answer_scope": answer.answer_scope,
            "status": answer.status,
            "direct_answer": answer.direct_answer,
            "explanation": answer.explanation,
            "evidence_meaning": answer.evidence_meaning,
            "impact": answer.impact,
            "next_steps": answer.next_steps,
            "limitations": answer.limitations,
            "claim_selections": answer.claim_selections.model_dump(mode="json"),
            "citations": [citation.model_dump(mode="json") for citation in answer.citations],
            "suggested_questions": answer.suggested_questions,
            "used_context_message_ids": answer.used_context_message_ids,
        }
    )
    if answer.request_sha256 != expected_request_sha256:
        raise ValueError("assistant answer request binding differs")
    if (
        generation.task_id != _CHAT_TASK_ID
        or generation.prompt_sha256 != prompts["chat"]
        or generation.output_schema_sha256 != schemas["chat"]
        or classification is None
        or classification.prompt_sha256 != prompts["scope_classifier"]
        or classification.output_schema_sha256 != schemas["scope_classifier"]
        or classification.semantic_output_schema_sha256 != schemas["scope_classifier_semantic"]
        or verification.verifier_prompt_sha256 != prompts["chat_verifier"]
        or verification.verifier_schema_sha256 != schemas["chat_verifier"]
        or verification.verified_content_sha256 != verified_content_sha256
        or not verification.deterministic_checks_passed
    ):
        raise ValueError("assistant generation or verification identity is not pinned")
    answer_areas = {
        "direct_answer": [answer.direct_answer],
        "explanation": answer.explanation,
        "evidence_meaning": answer.evidence_meaning,
        "impact": answer.impact,
        "next_steps": answer.next_steps,
    }
    answer_claims = build_answer_experience_claim_catalog(result)
    if answer.answer_scope == "evidence_grounded":
        answer_claims = [
            claim for claim in answer_claims if claim.section_code == "regulatory_meaning"
        ]
    elif answer.answer_scope != "case_grounded":
        answer_claims = []
    answer_claims = answer_claims[:100]
    guidance: dict[str, str] = {}
    if answer.answer_scope == "general_customs_guidance":
        catalog = provider_meaning_catalog()
        try:
            guidance = {
                topic: catalog.general_guidance[topic] for topic in classification.guidance_topics
            }
        except KeyError as exc:
            raise ValueError("assistant guidance topic is outside the provider catalog") from exc

    if answer.answer_scope in material_scopes:
        claim_index = {claim.claim_id: claim for claim in answer_claims}
        selected_claims: dict[str, CanonicalExperienceClaimV1] = {}
        claim_selections = answer.claim_selections.model_dump(mode="json")
        for area, texts in answer_areas.items():
            claim_ids = claim_selections[area]
            claims = [claim_index.get(claim_id) for claim_id in claim_ids]
            if any(claim is None for claim in claims):
                raise ValueError(f"material assistant {area} selected an unknown claim")
            typed_claims = [claim for claim in claims if claim is not None]
            if any(claim.role not in _ANSWER_AREA_ROLES[area] for claim in typed_claims):
                raise ValueError(f"material assistant {area} selected a claim with wrong role")
            if bool(texts) != bool(typed_claims):
                raise ValueError(f"material assistant {area} claim coverage is inconsistent")
            if generation.mode == "claude":
                source_texts = [claim.canonical_text for claim in typed_claims]
                blockers = [
                    blocker
                    for text in texts
                    for blocker in _grounded_prose_blockers(
                        text,
                        source_texts,
                        prefix=f"assistant_{area}",
                        minimum_chars=_PRESENTATION_MIN_MATERIAL_STATEMENT_CHARS,
                    )
                ]
                if blockers:
                    raise ValueError(
                        f"material assistant {area} prose failed deterministic grounding: "
                        + ",".join(blockers)
                    )
            for claim in typed_claims:
                selected_claims[claim.claim_id] = claim
        if not answer.direct_answer or not answer.claim_selections.direct_answer:
            raise ValueError("material assistant direct answer has no selected claim")
        if generation.mode == "claude":
            if not answer.limitations:
                raise ValueError("material assistant omitted the Case limitation")
            limitation_blockers = [
                blocker
                for text in answer.limitations
                for blocker in _grounded_prose_blockers(
                    text,
                    [_CASE_LIMITATION],
                    prefix="assistant_limitation",
                    minimum_chars=_PRESENTATION_MIN_MATERIAL_STATEMENT_CHARS,
                )
            ]
            if limitation_blockers:
                raise ValueError(
                    "material assistant limitation failed deterministic grounding: "
                    + ",".join(limitation_blockers)
                )
        expected_citations = {
            (citation.kind, citation.reference_id)
            for claim in selected_claims.values()
            for citation in claim.citation_references
        }
        observed_citations = {
            (citation.kind, citation.reference_id) for citation in answer.citations
        }
        if expected_citations != observed_citations:
            raise ValueError("assistant claim citation binding differs from selected claims")
    elif answer.answer_scope == "general_customs_guidance":
        guidance_values = list(guidance.values())
        if (
            any(answer.claim_selections.model_dump(mode="json").values())
            or not guidance_values
            or not answer.direct_answer
            or any(answer_areas[area] for area in ("evidence_meaning", "impact", "next_steps"))
            or answer.citations
        ):
            raise ValueError("general customs guidance differs from the pinned safe catalog")
        if generation.mode == "claude":
            guidance_texts = [
                answer.direct_answer,
                *answer.explanation,
            ]
            blockers = [
                blocker
                for text in guidance_texts
                for blocker in _grounded_prose_blockers(
                    text,
                    guidance_values,
                    prefix="assistant_general_guidance",
                    minimum_chars=_PRESENTATION_MIN_MATERIAL_STATEMENT_CHARS,
                )
            ]
            if any(_GENERAL_GUIDANCE_CASE_ASSERTION_RE.search(text) for text in guidance_texts):
                blockers.append("assistant_general_guidance_case_assertion")
            if not answer.limitations:
                blockers.append("assistant_general_guidance_limitation_missing")
            else:
                blockers.extend(
                    blocker
                    for text in answer.limitations
                    for blocker in _grounded_prose_blockers(
                        text,
                        [_GENERAL_GUIDANCE_LIMITATION],
                        prefix="assistant_general_guidance_limitation",
                        minimum_chars=_PRESENTATION_MIN_MATERIAL_STATEMENT_CHARS,
                    )
                )
            if blockers:
                raise ValueError(
                    "general customs guidance failed deterministic grounding: " + ",".join(blockers)
                )
    elif any(answer.claim_selections.model_dump(mode="json").values()):
        raise ValueError("non-grounded assistant answer cannot select canonical claims")

    candidate_answer = {
        "direct_answer": answer.direct_answer,
        "explanation": answer.explanation,
        "evidence_meaning": answer.evidence_meaning,
        "impact": answer.impact,
        "next_steps": answer.next_steps,
        "limitations": answer.limitations,
        "claim_selections": answer.claim_selections.model_dump(mode="json"),
        "citation_references": [citation.model_dump(mode="json") for citation in answer.citations],
        "suggested_questions": answer.suggested_questions,
        "used_context_message_ids": answer.used_context_message_ids,
    }
    verifier_context_sha256 = canonical_digest(
        {
            "question": request.question,
            "recent_turns": [turn.model_dump(mode="json") for turn in request.recent_turns],
            "answer_scope": answer.answer_scope,
            "allowed_claims": [claim.model_dump(mode="json") for claim in answer_claims],
            "general_guidance": guidance,
            "fixed_limitation_source": (
                _GENERAL_GUIDANCE_LIMITATION
                if answer.answer_scope == "general_customs_guidance"
                else _CASE_LIMITATION
            ),
            "candidate_answer": candidate_answer,
        }
    )
    if classification.mode == "claude":
        if classification.model_id != result.runtime_release.model_id:
            raise ValueError("assistant scope classifier model identity differs")
    elif classification.model_id is not None:
        raise ValueError("non-model scope classifier claimed a model identity")
    if generation.mode == "claude":
        if (
            generation.model_id != result.runtime_release.model_id
            or verification.status != "passed"
            or verification.entailment_verifier != "claude"
            or verification.verifier_model_id != result.runtime_release.model_id
            or verification.verifier_context_sha256 != verifier_context_sha256
            or verification.checked_statement_count != expected_claude_count
            or verification.reason_codes
        ):
            raise ValueError("Claude assistant answer audit is incomplete")
    else:
        if (
            generation.model_id is not None
            or verification.status != "fallback"
            or verification.entailment_verifier != "not_run"
            or verification.verifier_model_id is not None
            or verification.verifier_context_sha256 is not None
            or verification.checked_statement_count != expected_fallback_count
            or not verification.reason_codes
        ):
            raise ValueError("deterministic assistant fallback audit is inconsistent")
        expected = _expected_fallback_answer_fields(
            answer=answer,
            result=result,
            expected_context_message_ids=expected_context_message_ids,
        )
        observed = {
            key: getattr(answer, key)
            for key in (
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
        }
        if observed != expected:
            raise ValueError("deterministic assistant fallback differs from canonical content")
    if answer.suggested_questions != _SAFE_SUGGESTED_QUESTIONS:
        raise ValueError("assistant suggested questions are not the pinned safe catalog")


BACKEND_STATUS_LABELS_KO: dict[str, str] = {
    "queued": "심사 요청됨",
    "running": "심사 진행 중",
    "succeeded": "심사 완료",
    "failed": "심사 실패",
    "delivery_uncertain": "전달 상태 확인 필요",
    "response_received_rejected": "안전 검증에서 응답 거절",
    "response_persistence_uncertain": "결과 저장 상태 확인 필요",
    "request_validation": "요청 확인",
    "document_analysis": "문서 분석",
    "normalization": "값 정리",
    "document_pairing": "문서 연결",
    "cross_validation": "문서 대조",
    "adaptive_investigation": "Agent 추가 조사",
    "evidence_retrieval": "법령 근거 조회",
    "compliance_review": "규정 확인",
    "report_composition": "설명 보고서 작성",
    "grounding_verification": "근거 검증",
    "result_validation": "최종 결과 검증",
    "conversation_binding": "대화 맥락 확인",
    "answer_planning": "답변 구성",
    "answer_composition": "답변 작성",
    "persistence": "결과 저장",
    "completed": "완료",
}
