"""Backend-derived trade views: HS classification, origin verification, duty.

These views are computed from the immutable Agent result already stored on a
review run plus packaged, provenance-labelled reference data.  They never
mutate the Agent result and never enter ``provider_result_sha256``; the only
persisted state is the reviewer's HS code selection, which follows the same
append-only ownership rules as finding decisions.

Every number shown to a person here is either quoted from a stored document
fact or taken from the packaged rate snapshot; when either input is missing
the view reports the gap instead of inventing a value.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from customs_backend.domain import HsSelectionRecord, ReviewRunRecord

_HS_FACT_NAMES = frozenset({"hs_code", "hs_codes"})
_ORIGIN_FACT_NAMES = frozenset({"origin_country", "country_of_origin"})
_AMOUNT_FACT_NAMES = ("total_amount", "amount")
_DESCRIPTION_FACT_NAMES = frozenset({"description", "goods_description"})
_HS_CODE_PATTERN = re.compile(r"\b([0-9]{10})\b")
_MONEY_PATTERN = re.compile(r"(?:([A-Z]{3})\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)")
_COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")


def _load_reference(filename: str) -> dict[str, Any]:
    package = resources.files("customs_backend.reference")
    with (package / filename).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"reference dataset {filename} is malformed")
    return value


@lru_cache(maxsize=1)
def hsk_names() -> dict[str, Any]:
    return _load_reference("hsk_names_20260101.json")


@lru_cache(maxsize=1)
def duty_rates() -> dict[str, Any]:
    return _load_reference("duty_rates_20260101.json")


# ── View models (public API DTOs) ────────────────────────────────


class HsAncestor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: int
    code: str
    name_ko: str


class HsCandidateBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    fact_id: str
    fact_name: str
    raw_value: str


class HsCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[0-9]{10}$")
    name_ko: str | None = None
    name_en: str | None = None
    ancestors: list[HsAncestor] = Field(default_factory=list)
    # Share of HS-bearing documents in this run that cite the code (0..1).
    # It is a document-agreement ratio, not a model probability.  A
    # description-match candidate instead carries its capped lexical
    # overlap share, so it always ranks below any document-cited code.
    confidence: float = Field(ge=0.0, le=1.0)
    in_reference: bool
    recommended: bool
    origin: Literal["document_code", "description_match"] = "document_code"
    basis: list[HsCandidateBasis] = Field(default_factory=list)


class HsReferenceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str
    source: str


class HsClassificationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    reference: HsReferenceInfo
    candidates: list[HsCandidate]
    selection: HsSelectionRecord | None = None


class HsReferenceEntryView(BaseModel):
    """One packaged HSK reference row for a manually typed 10-digit code."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[0-9]{10}$")
    in_reference: bool
    name_ko: str | None = None
    name_en: str | None = None
    ancestors: list[HsAncestor] = Field(default_factory=list)
    reference: HsReferenceInfo


class HsReferenceSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[0-9]{10}$")
    name_ko: str | None = None
    name_en: str | None = None
    ancestors: list[HsAncestor] = Field(default_factory=list)


class HsReferenceSearchView(BaseModel):
    """Packaged HSK reference rows matching a code prefix or an item name."""

    model_config = ConfigDict(extra="forbid")

    query: str
    mode: Literal["code_prefix", "name"]
    results: list[HsReferenceSearchResult]
    truncated: bool
    reference: HsReferenceInfo


class OriginCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: Literal["certificate_no", "origin_country", "hs_code"]
    certificate_value: str | None = None
    reference_value: str | None = None
    reference_document_id: str | None = None
    status: Literal["match", "mismatch", "missing"]


class OriginFtaHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agreement_code: str
    agreement_name: str
    origin_covered: bool
    certificate_form: str
    source_uri: str


class OriginCertificateInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    filename: str
    extraction_status: str


class OriginVerificationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    status: Literal["verified", "mismatch", "missing_certificate", "needs_review"]
    certificate: OriginCertificateInfo | None = None
    origin_country: str | None = None
    checks: list[OriginCheck] = Field(default_factory=list)
    fta_hints: list[OriginFtaHint] = Field(default_factory=list)


class DutyHsCode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[0-9]{10}$")
    source: Literal["reviewer_selection", "document_fact"]
    name_ko: str | None = None


class DutiableValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: float = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    basis: str
    document_id: str | None = None


class DutyRateRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["origin_in_agreement", "certificate_present", "certificate_origin_match"]
    satisfied: bool


class DutyRate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_kind: Literal["basic", "fta"]
    agreement_code: str | None = None
    label: str
    rate_pct: float = Field(ge=0)
    source_uri: str
    requirements: list[DutyRateRequirement] = Field(default_factory=list)
    applicable: bool


class DutyEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_kind: Literal["basic", "fta"]
    agreement_code: str | None = None
    rate_pct: float
    duty_amount: float
    vat_rate_pct: float
    vat_amount: float
    total_estimated: float
    currency: str


class DutySavings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vs_basic_duty: float
    currency: str
    blocked_requirements: list[str] = Field(default_factory=list)


class DutyReferenceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str
    source: str
    disclaimer: str


class DutyAssessmentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: str
    case_id: str
    status: Literal["estimated", "hs_code_unavailable", "rates_unavailable", "value_unavailable"]
    reference: DutyReferenceInfo
    hs_code: DutyHsCode | None = None
    origin_country: str | None = None
    dutiable_value: DutiableValue | None = None
    rates: list[DutyRate] = Field(default_factory=list)
    applied: DutyEstimate | None = None
    savings: DutySavings | None = None


# ── Fact extraction helpers ──────────────────────────────────────


def _iter_facts(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    facts = [item for item in analysis.get("facts", []) if isinstance(item, dict)]
    for line_item in analysis.get("line_items", []):
        if isinstance(line_item, dict):
            facts.extend(item for item in line_item.get("facts", []) if isinstance(item, dict))
    return facts


def _fact_value(fact: dict[str, Any]) -> str:
    normalized = fact.get("normalized_value")
    if isinstance(normalized, str) and normalized.strip():
        return normalized.strip()
    raw = fact.get("raw_value")
    return raw.strip() if isinstance(raw, str) else ""


def _hs_codes_in(value: str) -> list[str]:
    return _HS_CODE_PATTERN.findall(value)


def _normalise_country(value: str) -> str | None:
    candidate = value.strip().upper()
    return candidate if _COUNTRY_PATTERN.match(candidate) else None


def _parse_money(value: str) -> tuple[float, str | None] | None:
    match = _MONEY_PATTERN.search(value.strip())
    if match is None:
        return None
    try:
        amount = float(match.group(2).replace(",", ""))
    except ValueError:
        return None
    return amount, match.group(1)


def _analysis_type(analysis: dict[str, Any]) -> str:
    value = analysis.get("document_type")
    return value if isinstance(value, str) else "unknown"


# ── HS classification ────────────────────────────────────────────


def _ancestors_for(code: str) -> list[HsAncestor]:
    levels = hsk_names().get("levels", {})
    ancestors: list[HsAncestor] = []
    # The HS6 sheet mixes 5-digit subheading stems with full 6-digit codes,
    # so the subheading lookup falls back one digit.
    for level, lengths in ((2, (2,)), (4, (4,)), (6, (6, 5))):
        table = levels.get(str(level), {})
        for length in lengths:
            prefix = code[:length]
            entry = table.get(prefix)
            if isinstance(entry, list) and entry and isinstance(entry[0], str):
                ancestors.append(HsAncestor(level=level, code=prefix, name_ko=entry[0]))
                break
    return ancestors


def _leaf_names(code: str) -> tuple[str | None, str | None]:
    entry = hsk_names().get("levels", {}).get("10", {}).get(code)
    if isinstance(entry, list) and len(entry) == 2:
        name_ko = entry[0] if isinstance(entry[0], str) and entry[0] else None
        name_en = entry[1] if isinstance(entry[1], str) and entry[1] else None
        return name_ko, name_en
    return None, None


def hs_reference_has(code: str) -> bool:
    return code in hsk_names().get("levels", {}).get("10", {})


# ── Description-based HSK candidates ─────────────────────────────
#
# When no document carries a 10-digit HS code, the reviewer still needs a
# starting point.  These candidates come from a deterministic lexical match
# of the documents' goods descriptions against the packaged HSK name table
# (leaf plus ancestor names, Korean and English).  They are reference-backed
# suggestions only: confidence is capped, ``recommended`` is never set, and
# the reviewer confirms through the same selection flow as cited codes.

_DESCRIPTION_TOKEN_PATTERN = re.compile(r"[0-9a-z가-힣]+")
_DESCRIPTION_STOPWORDS = frozenset(
    {
        "and", "for", "from", "its", "not", "of", "or", "other", "than",
        "the", "with", "made", "kind", "kinds", "part", "parts",
        "기타", "그", "밖의", "이나", "및", "것", "한정한다", "제외한다", "포함한다",
    }
)
_DESCRIPTION_CANDIDATE_LIMIT = 5
_DESCRIPTION_CONFIDENCE_CAP = 0.4


def _description_stems(text: str) -> frozenset[str]:
    lowered = text.lower().replace("'", "")
    stems: set[str] = set()
    for compact in (lowered, lowered.replace("-", "")):
        for token in _DESCRIPTION_TOKEN_PATTERN.findall(compact):
            if len(token) < 2 or token in _DESCRIPTION_STOPWORDS or token.isdigit():
                continue
            stems.add(token)
            if len(token) > 3 and token.endswith("s"):
                stems.add(token[:-1])
    return frozenset(stems)


@lru_cache(maxsize=1)
def _description_index() -> tuple[tuple[str, frozenset[str]], ...]:
    levels = hsk_names().get("levels", {})
    ancestor_tables = {length: levels.get(str(level), {}) for level, length in ((2, 2), (4, 4), (6, 6))}
    entries: list[tuple[str, frozenset[str]]] = []
    for code, names in levels.get("10", {}).items():
        parts = [name for name in names if isinstance(name, str)]
        for length, table in ancestor_tables.items():
            # The HS6 sheet mixes 5-digit stems with 6-digit codes (see
            # _ancestors_for), so the subheading lookup falls back one digit.
            for prefix_length in (length, length - 1) if length == 6 else (length,):
                entry = table.get(code[:prefix_length])
                if isinstance(entry, list):
                    parts.extend(name for name in entry if isinstance(name, str))
                    break
        stems = _description_stems(" ".join(parts))
        if stems:
            entries.append((code, stems))
    return tuple(entries)


def _description_facts(analyses: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    facts: list[tuple[str, dict[str, Any]]] = []
    for analysis in analyses:
        if not isinstance(analysis, dict):
            continue
        document_id = str(analysis.get("document_id", ""))
        for fact in _iter_facts(analysis):
            name = fact.get("name")
            if isinstance(name, str) and name in _DESCRIPTION_FACT_NAMES and _fact_value(fact):
                facts.append((document_id, fact))
    return facts


def _description_candidates(analyses: list[dict[str, Any]]) -> list[HsCandidate]:
    facts = _description_facts(analyses)
    stems: set[str] = set()
    for _, fact in facts:
        stems |= _description_stems(_fact_value(fact))
    if not stems:
        return []

    scored: list[tuple[int, float, str]] = []
    for code, entry_stems in _description_index():
        matched = len(stems & entry_stems)
        if matched == 0:
            continue
        scored.append((matched, matched / len(stems), code))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

    basis = [
        HsCandidateBasis(
            document_id=document_id,
            fact_id=str(fact.get("fact_id", "")),
            fact_name=str(fact.get("name", "")),
            raw_value=str(fact.get("raw_value", ""))[:200],
        )
        for document_id, fact in facts[:20]
    ]
    candidates: list[HsCandidate] = []
    for matched, share, code in scored[:_DESCRIPTION_CANDIDATE_LIMIT]:
        name_ko, name_en = _leaf_names(code)
        candidates.append(
            HsCandidate(
                code=code,
                name_ko=name_ko,
                name_en=name_en,
                ancestors=_ancestors_for(code),
                confidence=round(min(_DESCRIPTION_CONFIDENCE_CAP, share), 4),
                in_reference=True,
                recommended=False,
                origin="description_match",
                basis=basis,
            )
        )
    return candidates


def build_hs_classification(run: ReviewRunRecord) -> HsClassificationView:
    reference = hsk_names()
    code_basis: dict[str, list[HsCandidateBasis]] = {}
    code_documents: dict[str, set[str]] = {}
    documents_with_hs: set[str] = set()

    for analysis in run.document_analyses:
        if not isinstance(analysis, dict):
            continue
        document_id = str(analysis.get("document_id", ""))
        for fact in _iter_facts(analysis):
            name = fact.get("name")
            if not isinstance(name, str) or name not in _HS_FACT_NAMES:
                continue
            value = _fact_value(fact)
            for code in _hs_codes_in(value):
                documents_with_hs.add(document_id)
                code_documents.setdefault(code, set()).add(document_id)
                code_basis.setdefault(code, []).append(
                    HsCandidateBasis(
                        document_id=document_id,
                        fact_id=str(fact.get("fact_id", "")),
                        fact_name=name,
                        raw_value=str(fact.get("raw_value", ""))[:200],
                    )
                )

    total_documents = len(documents_with_hs)
    candidates: list[HsCandidate] = []
    best_share = 0.0
    for code, documents in code_documents.items():
        share = len(documents) / total_documents if total_documents else 0.0
        best_share = max(best_share, share)
    for code, documents in sorted(
        code_documents.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        share = len(documents) / total_documents if total_documents else 0.0
        in_reference = hs_reference_has(code)
        name_ko, name_en = _leaf_names(code)
        candidates.append(
            HsCandidate(
                code=code,
                name_ko=name_ko,
                name_en=name_en,
                ancestors=_ancestors_for(code),
                confidence=round(share if in_reference else min(share, 0.5), 4),
                in_reference=in_reference,
                recommended=in_reference
                and share == best_share
                and sum(1 for docs in code_documents.values() if len(docs) / total_documents == best_share) == 1,
                basis=code_basis[code][:20],
            )
        )

    if not candidates:
        candidates = _description_candidates(
            [analysis for analysis in run.document_analyses if isinstance(analysis, dict)]
        )

    return HsClassificationView(
        review_run_id=run.review_run_id,
        case_id=run.case_id,
        reference=HsReferenceInfo(
            as_of=str(reference.get("as_of", "")),
            source=str(reference.get("source", "")),
        ),
        candidates=candidates,
        selection=run.hs_selection,
    )


# ── Origin (CO) verification ─────────────────────────────────────


def _first_fact(
    analyses: list[dict[str, Any]], names: frozenset[str] | tuple[str, ...]
) -> tuple[str, str] | None:
    """Return (value, document_id) for the first matching fact, if any."""

    for analysis in analyses:
        document_id = str(analysis.get("document_id", ""))
        for fact in _iter_facts(analysis):
            name = fact.get("name")
            if isinstance(name, str) and name in names:
                value = _fact_value(fact)
                if value:
                    return value, document_id
    return None


def build_origin_verification(run: ReviewRunRecord) -> OriginVerificationView:
    analyses = [item for item in run.document_analyses if isinstance(item, dict)]
    certificates = [a for a in analyses if _analysis_type(a) == "certificate_of_origin"]
    others = [a for a in analyses if _analysis_type(a) != "certificate_of_origin"]

    reference_origin = _first_fact(others, _ORIGIN_FACT_NAMES)
    origin_country = None
    if reference_origin is not None:
        origin_country = _normalise_country(reference_origin[0])

    if not certificates:
        return OriginVerificationView(
            review_run_id=run.review_run_id,
            case_id=run.case_id,
            status="missing_certificate",
            origin_country=origin_country,
            fta_hints=_fta_hints(origin_country),
        )

    certificate = certificates[0]
    certificate_info = OriginCertificateInfo(
        document_id=str(certificate.get("document_id", "")),
        filename=str(certificate.get("filename", "")),
        extraction_status=str(certificate.get("extraction_status", "unknown")),
    )

    checks: list[OriginCheck] = []

    co_certificate_no = _first_fact([certificate], frozenset({"certificate_no"}))
    checks.append(
        OriginCheck(
            field="certificate_no",
            certificate_value=co_certificate_no[0] if co_certificate_no else None,
            status="match" if co_certificate_no else "missing",
        )
    )

    co_origin = _first_fact([certificate], _ORIGIN_FACT_NAMES)
    co_origin_country = _normalise_country(co_origin[0]) if co_origin else None
    if co_origin_country is None or origin_country is None:
        origin_status: Literal["match", "mismatch", "missing"] = "missing"
    elif co_origin_country == origin_country:
        origin_status = "match"
    else:
        origin_status = "mismatch"
    checks.append(
        OriginCheck(
            field="origin_country",
            certificate_value=co_origin_country,
            reference_value=origin_country,
            reference_document_id=reference_origin[1] if reference_origin else None,
            status=origin_status,
        )
    )

    co_hs = _first_fact([certificate], _HS_FACT_NAMES)
    reference_hs = _first_fact(others, _HS_FACT_NAMES)
    co_hs_codes = _hs_codes_in(co_hs[0]) if co_hs else []
    reference_hs_codes = _hs_codes_in(reference_hs[0]) if reference_hs else []
    if not co_hs_codes or not reference_hs_codes:
        hs_status: Literal["match", "mismatch", "missing"] = "missing"
    elif set(co_hs_codes) & set(reference_hs_codes):
        hs_status = "match"
    else:
        hs_status = "mismatch"
    checks.append(
        OriginCheck(
            field="hs_code",
            certificate_value=co_hs_codes[0] if co_hs_codes else None,
            reference_value=reference_hs_codes[0] if reference_hs_codes else None,
            reference_document_id=reference_hs[1] if reference_hs else None,
            status=hs_status,
        )
    )

    verified_origin = co_origin_country or origin_country
    if any(check.status == "mismatch" for check in checks):
        status: Literal["verified", "mismatch", "missing_certificate", "needs_review"] = "mismatch"
    elif certificate_info.extraction_status != "complete" or any(
        check.status == "missing" for check in checks
    ):
        status = "needs_review"
    else:
        status = "verified"

    return OriginVerificationView(
        review_run_id=run.review_run_id,
        case_id=run.case_id,
        status=status,
        certificate=certificate_info,
        origin_country=verified_origin,
        checks=checks,
        fta_hints=_fta_hints(verified_origin),
    )


def _fta_hints(origin_country: str | None) -> list[OriginFtaHint]:
    hints: list[OriginFtaHint] = []
    agreements = duty_rates().get("agreements", {})
    for code in sorted(agreements):
        entry = agreements[code]
        if not isinstance(entry, dict):
            continue
        countries = [str(item) for item in entry.get("countries", [])]
        hints.append(
            OriginFtaHint(
                agreement_code=code,
                agreement_name=str(entry.get("name_ko", code)),
                origin_covered=origin_country is not None and origin_country in countries,
                certificate_form=str(entry.get("certificate_form", "")),
                source_uri=str(entry.get("source_uri", "")),
            )
        )
    return hints


# ── Duty assessment ──────────────────────────────────────────────


def build_duty_assessment(run: ReviewRunRecord) -> DutyAssessmentView:
    dataset = duty_rates()
    reference = DutyReferenceInfo(
        as_of=str(dataset.get("as_of", "")),
        source=str(dataset.get("source", "")),
        disclaimer=str(dataset.get("disclaimer", "")),
    )

    def view(status: str, **kwargs: Any) -> DutyAssessmentView:
        return DutyAssessmentView(
            review_run_id=run.review_run_id,
            case_id=run.case_id,
            status=status,  # type: ignore[arg-type]
            reference=reference,
            **kwargs,
        )

    classification = build_hs_classification(run)
    if run.hs_selection is not None:
        hs_code = DutyHsCode(
            code=run.hs_selection.code,
            source="reviewer_selection",
            name_ko=_leaf_names(run.hs_selection.code)[0],
        )
    else:
        recommended = next(
            (item for item in classification.candidates if item.recommended),
            classification.candidates[0] if classification.candidates else None,
        )
        if recommended is None:
            return view("hs_code_unavailable")
        hs_code = DutyHsCode(
            code=recommended.code, source="document_fact", name_ko=recommended.name_ko
        )

    origin = build_origin_verification(run)
    origin_country = origin.origin_country

    rate_entry = dataset.get("rates", {}).get(hs_code.code)
    if not isinstance(rate_entry, dict):
        return view("rates_unavailable", hs_code=hs_code, origin_country=origin_country)

    certificate_present = origin.certificate is not None
    certificate_origin_match = any(
        check.field == "origin_country" and check.status == "match" for check in origin.checks
    )

    rates: list[DutyRate] = []
    basic = rate_entry.get("basic")
    if isinstance(basic, dict):
        rates.append(
            DutyRate(
                rate_kind="basic",
                label=str(basic.get("label", "기본세율")),
                rate_pct=float(basic.get("rate_pct", 0.0)),
                source_uri=str(basic.get("source_uri", "")),
                applicable=True,
            )
        )
    agreements = dataset.get("agreements", {})
    for fta in rate_entry.get("fta", []):
        if not isinstance(fta, dict):
            continue
        agreement_code = str(fta.get("agreement", ""))
        agreement = agreements.get(agreement_code, {})
        countries = [str(item) for item in agreement.get("countries", [])]
        requirements = [
            DutyRateRequirement(
                code="origin_in_agreement",
                satisfied=origin_country is not None and origin_country in countries,
            ),
            DutyRateRequirement(code="certificate_present", satisfied=certificate_present),
            DutyRateRequirement(
                code="certificate_origin_match", satisfied=certificate_origin_match
            ),
        ]
        rates.append(
            DutyRate(
                rate_kind="fta",
                agreement_code=agreement_code,
                label=str(fta.get("label", agreement_code)),
                rate_pct=float(fta.get("rate_pct", 0.0)),
                source_uri=str(fta.get("source_uri", "")),
                requirements=requirements,
                applicable=all(item.satisfied for item in requirements),
            )
        )

    if not rates:
        return view("rates_unavailable", hs_code=hs_code, origin_country=origin_country)

    analyses = [item for item in run.document_analyses if isinstance(item, dict)]
    invoices = [a for a in analyses if _analysis_type(a) == "invoice"] or analyses
    amount_fact = _first_fact(invoices, _AMOUNT_FACT_NAMES)
    dutiable = None
    if amount_fact is not None:
        parsed = _parse_money(amount_fact[0])
        if parsed is not None:
            amount, currency = parsed
            dutiable = DutiableValue(
                amount=amount,
                currency=currency or "USD",
                basis="invoice_total_amount",
                document_id=amount_fact[1],
            )
    if dutiable is None:
        return view(
            "value_unavailable",
            hs_code=hs_code,
            origin_country=origin_country,
            rates=rates,
        )

    applicable = [item for item in rates if item.applicable]
    chosen = min(applicable, key=lambda item: item.rate_pct)
    basic_rate = next((item for item in rates if item.rate_kind == "basic"), chosen)
    vat_rate = float(dataset.get("vat_rate_pct", 10.0))

    duty_amount = round(dutiable.amount * chosen.rate_pct / 100.0, 2)
    vat_amount = round((dutiable.amount + duty_amount) * vat_rate / 100.0, 2)
    applied = DutyEstimate(
        rate_kind=chosen.rate_kind,
        agreement_code=chosen.agreement_code,
        rate_pct=chosen.rate_pct,
        duty_amount=duty_amount,
        vat_rate_pct=vat_rate,
        vat_amount=vat_amount,
        total_estimated=round(duty_amount + vat_amount, 2),
        currency=dutiable.currency,
    )

    basic_duty = round(dutiable.amount * basic_rate.rate_pct / 100.0, 2)
    best_possible = min(rates, key=lambda item: item.rate_pct)
    blocked: list[str] = []
    if not best_possible.applicable:
        blocked = [
            requirement.code
            for requirement in best_possible.requirements
            if not requirement.satisfied
        ]
    savings = DutySavings(
        vs_basic_duty=round(basic_duty - duty_amount, 2),
        currency=dutiable.currency,
        blocked_requirements=blocked,
    )

    return view(
        "estimated",
        hs_code=hs_code,
        origin_country=origin_country,
        dutiable_value=dutiable,
        rates=rates,
        applied=applied,
        savings=savings,
    )


def validate_hs_selection_code(code: str) -> None:
    if not hs_reference_has(code):
        raise ValueError("the HS code is not present in the packaged HSK reference table")


def lookup_hs_reference(code: str) -> HsReferenceEntryView:
    """Describe one well-formed 10-digit code from the packaged reference.

    An unknown code is a normal answer (``in_reference`` false), not an
    error; a malformed code is rejected by the caller before this lookup.
    """

    reference = hsk_names()
    in_reference = hs_reference_has(code)
    name_ko, name_en = _leaf_names(code) if in_reference else (None, None)
    return HsReferenceEntryView(
        code=code,
        in_reference=in_reference,
        name_ko=name_ko,
        name_en=name_en,
        ancestors=_ancestors_for(code) if in_reference else [],
        reference=HsReferenceInfo(
            as_of=str(reference.get("as_of", "")),
            source=str(reference.get("source", "")),
        ),
    )


_SEARCH_RESULT_LIMIT = 12
_SEARCH_CODE_PATTERN = re.compile(r"[0-9]{1,10}")


@lru_cache(maxsize=1)
def _name_search_index() -> tuple[tuple[str, str, str], ...]:
    """(code, lowered leaf text, lowered leaf+ancestor text) per leaf code."""

    entries: list[tuple[str, str, str]] = []
    for code in sorted(hsk_names().get("levels", {}).get("10", {})):
        name_ko, name_en = _leaf_names(code)
        leaf_text = " ".join(part for part in (name_ko, name_en) if part).lower()
        ancestor_text = " ".join(item.name_ko for item in _ancestors_for(code)).lower()
        entries.append((code, leaf_text, f"{leaf_text} {ancestor_text}"))
    return tuple(entries)


def _search_result(code: str) -> HsReferenceSearchResult:
    name_ko, name_en = _leaf_names(code)
    return HsReferenceSearchResult(
        code=code,
        name_ko=name_ko,
        name_en=name_en,
        ancestors=_ancestors_for(code),
    )


def search_hs_reference(query: str) -> HsReferenceSearchView:
    """Match packaged HSK leaf rows by code prefix or by item name.

    A digits-only query is a code-prefix search; anything else matches every
    query token as a substring of the row's Korean/English names (leaf plus
    ancestors).  Both modes are deterministic lookups over the packaged
    table — no scoring model, no external calls.
    """

    reference = hsk_names()
    cleaned = query.strip()
    compact = cleaned.replace(" ", "")
    matched: list[str]
    if _SEARCH_CODE_PATTERN.fullmatch(compact):
        mode: Literal["code_prefix", "name"] = "code_prefix"
        matched = [
            code for code, _, _ in _name_search_index() if code.startswith(compact)
        ]
    else:
        mode = "name"
        tokens = [token for token in cleaned.lower().split() if token]
        scored: list[tuple[int, str]] = []
        for code, leaf_text, full_text in _name_search_index():
            if tokens and all(token in full_text for token in tokens):
                leaf_hits = sum(1 for token in tokens if token in leaf_text)
                scored.append((leaf_hits, code))
        scored.sort(key=lambda item: (-item[0], item[1]))
        matched = [code for _, code in scored]

    return HsReferenceSearchView(
        query=cleaned,
        mode=mode,
        results=[_search_result(code) for code in matched[:_SEARCH_RESULT_LIMIT]],
        truncated=len(matched) > _SEARCH_RESULT_LIMIT,
        reference=HsReferenceInfo(
            as_of=str(reference.get("as_of", "")),
            source=str(reference.get("source", "")),
        ),
    )


__all__ = [
    "DutyAssessmentView",
    "HsClassificationView",
    "HsReferenceEntryView",
    "HsReferenceSearchView",
    "OriginVerificationView",
    "build_duty_assessment",
    "build_hs_classification",
    "build_origin_verification",
    "hs_reference_has",
    "lookup_hs_reference",
    "search_hs_reference",
    "validate_hs_selection_code",
]
