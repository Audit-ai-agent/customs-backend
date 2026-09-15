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

from customs_backend.domain import CaseRecord, DocumentRecord, HsSelectionRecord, ReviewRunRecord
from customs_backend.duty_reference import (
    UNIPASS_SOURCE_URI,
    DutyReferenceStore,
    load_store,
)

_HS_FACT_NAMES = frozenset({"hs_code", "hs_codes"})
_ORIGIN_FACT_NAMES = frozenset({"origin_country", "country_of_origin"})
_AMOUNT_FACT_NAMES = ("total_amount", "amount")
_DESCRIPTION_FACT_NAMES = frozenset({"description", "goods_description"})
_HS_CODE_PATTERN = re.compile(r"\b([0-9]{10})\b")
# Document text lines that name a tariff code; a bare 10-digit number
# elsewhere (invoice or container numbers) is not an HS candidate.
_HS_LINE_PATTERN = re.compile(r"(?i)\bH\.?S\.?K?\b|세번|품목\s*번호|tariff")
_DESCRIPTION_LINE_PATTERN = re.compile(r"(?i)\bdescription\b|\bgoods\b|품명|물품명|품목명")
# "Description: Frozen mackerel | HS Code: ... | Quantity: ..." -> "Frozen mackerel".
_DESCRIPTION_LABEL_PATTERN = re.compile(r"(?i)^(?:goods\s+)?(?:description|품명|물품명|품목명)\s*[:：]\s*")
# A description cell rendered as its own text line ("Description of Goods",
# "Goods Description", "6. Description of Goods / Quantity", "⑪ Description of
# Goods"): the item name then sits on one of the following lines, among other
# table cells.  Box numbers come in many shapes (6., (6), ⑪), so anything that
# is not a letter may precede the label.
_CELL_LABEL_PREFIX = r"[^A-Za-z가-힣]*"
_DESCRIPTION_HEADER_ONLY_PATTERN = re.compile(
    r"(?i)^"
    + _CELL_LABEL_PREFIX
    + r"(?:goods\s+description|description\s+of\s+goods|description|품명|물품명|품목명)"
    r"(?:\s*/\s*(?:quantity|qty|수량))?\s*[:：]?\s*$"
)
# Other table labels: column headers before the values and the total/weight/
# signature cells after them.
_TABLE_CELL_NOISE_PATTERN = re.compile(
    r"(?i)^"
    + _CELL_LABEL_PREFIX
    + r"(?:quantity|qty|unit\s*price|amount|net\s*weight|gross\s*weight|measurement|"
    r"marks?(?:\s*(?:&|and)\s*(?:no\.?|numbers?))?|no\.?\s*(?:&\s*kinds?\s*)?of\s*(?:pkgs|packages|containers)|"
    r"containers?\s*/\s*packages|total|signed|remarks?|hs\s*code|origin|country\s*of\s*origin|"
    r"freight|revenue\s*tons|rate|per|prepaid|collect|place\s+(?:and|of)|signature|"
    r"수량|단가|금액|순중량|총중량|합계)\b"
)
# "31,214 PCS", "4 CNTR", "11,927.0 KGS": a count with its unit is a quantity
# cell, never an item name.
_QUANTITY_CELL_PATTERN = re.compile(r"^\(?[0-9][0-9,.]*\s*[A-Za-z]{1,8}\)?\.?$")
_DESCRIPTION_FOLLOW_LINES = 16
_CASE_TEXT_LINE_LIMIT = 400


def _looks_like_item_name(line: str) -> bool:
    """A table cell that can be an item name: mostly words, not a number/unit cell."""

    if _TABLE_CELL_NOISE_PATTERN.match(line) or _QUANTITY_CELL_PATTERN.match(line):
        return False
    if _DESCRIPTION_HEADER_ONLY_PATTERN.match(line):
        return False
    letters = len(re.findall(r"[A-Za-z가-힣]", line))
    digits = len(re.findall(r"[0-9]", line))
    return letters >= 4 and letters > digits * 2


def _description_values(lines: list[str]) -> list[tuple[int, str]]:
    """Item-name text from labelled lines and from cells below a header cell.

    Below a header cell the text layer emits the remaining column headers,
    then the row's cells (marks, item name, quantities).  Quantity cells are
    skipped, and the row ends at the next label cell (TOTAL, Gross Weight,
    Signed …) once something has been collected -- a bill of lading lists
    "4 CNTR / 372 CTNS" before the item name, so a unit alone must not end
    the row.
    """

    values: list[tuple[int, str]] = []
    follow_until = -1
    collected_below_header = 0
    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if line_no <= follow_until:
            if _looks_like_item_name(stripped):
                values.append((line_no, stripped[:200]))
                collected_below_header += 1
            elif collected_below_header and _TABLE_CELL_NOISE_PATTERN.match(stripped):
                follow_until = -1
            continue
        keyword = _DESCRIPTION_LINE_PATTERN.search(stripped)
        # A label sits at the start of its line; "…the goods described above"
        # in running prose is not a description cell.
        if keyword is None or keyword.start() > 25:
            continue
        if _DESCRIPTION_HEADER_ONLY_PATTERN.match(stripped):
            follow_until = line_no + _DESCRIPTION_FOLLOW_LINES
            collected_below_header = 0
            continue
        value = _DESCRIPTION_LABEL_PATTERN.sub("", stripped.split("|", 1)[0]).strip()
        if value:
            values.append((line_no, value[:200]))
    return values
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
    origin: Literal["document_code", "description_match", "llm_match"] = "document_code"
    # Present only for llm_match: the Agent's one-sentence reason for the pick.
    explanation: str | None = Field(default=None, max_length=400)
    basis: list[HsCandidateBasis] = Field(default_factory=list)


class HsReferenceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str
    source: str


class CaseHsClassificationView(BaseModel):
    """HSK candidates read from the case's confirmed document text.

    Available before any AI review: codes are collected only from text lines
    that name a tariff code, and a description match is offered when no code
    is cited.  Confirming a code here feeds the first review's law-evidence
    retrieval as the reviewer HS selection.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    basis_kind: Literal["document_text", "agent_ranked"] = "document_text"
    reference: HsReferenceInfo
    candidates: list[HsCandidate]
    selection: HsSelectionRecord | None = None


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
    # True when no row matched every query word, so the results are ranked
    # by how many words they do match instead of being exact matches.
    partial: bool = False
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


DutyRateKind = Literal["basic", "wto", "preferential", "conditional", "fta"]


class DutyRate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_kind: DutyRateKind
    agreement_code: str | None = None
    # 관세율구분코드 from the full reference dataset (e.g. "A", "C", "FVN1").
    code: str | None = None
    label: str
    rate_pct: float = Field(ge=0)
    source_uri: str
    requirements: list[DutyRateRequirement] = Field(default_factory=list)
    applicable: bool


class DutyEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_kind: DutyRateKind
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
        # Words that name the *column*, not the goods: they appear in table
        # labels ("Description of Goods") and in many tariff lines alike.
        "goods", "good", "description", "descriptions", "item", "items",
        "article", "articles", "commodity", "commodities", "merchandise", "cargo",
        # Units next to a size or count ("24 INCH", "372 CTNS") describe the
        # quantity, not the goods.
        "inch", "inches", "mm", "cm", "kg", "kgs", "pcs", "pc", "ctn", "ctns", "cbm", "unit", "units",
        "기타", "그", "밖의", "이나", "및", "것", "한정한다", "제외한다", "포함한다",
        "물품", "품명", "품목",
    }
)
_DESCRIPTION_CANDIDATE_LIMIT = 5
_DESCRIPTION_CONFIDENCE_CAP = 0.4


def _description_stems(text: str) -> frozenset[str]:
    """Lower-cased word stems; a plural keeps only its singular so one word counts once."""

    lowered = text.lower().replace("'", "")
    stems: set[str] = set()
    for compact in (lowered, lowered.replace("-", "")):
        for token in _DESCRIPTION_TOKEN_PATTERN.findall(compact):
            if len(token) < 2 or token in _DESCRIPTION_STOPWORDS or token.isdigit():
                continue
            if len(token) > 3 and token.endswith("s"):
                token = token[:-1]
                if token in _DESCRIPTION_STOPWORDS:
                    continue
            stems.add(token)
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
    return _description_candidates_from_facts(_description_facts(analyses))


def _description_candidates_from_facts(
    facts: list[tuple[str, dict[str, Any]]],
    *,
    limit: int = _DESCRIPTION_CANDIDATE_LIMIT,
) -> list[HsCandidate]:
    """Match each description value on its own, then merge the codes.

    Values are scored separately rather than as one pooled bag of words: a
    stray cell picked up next to the item name ("ROTTERDAM", "4 CNTR") must
    not dilute the real name's share, and a generic word must not let hundreds
    of unrelated tariff lines tie with the goods actually described.  The
    same value repeated across the invoice, packing list, bill of lading and
    certificate of origin counts as agreement between documents.
    """

    # Distinct stem sets, with every fact (and document) that produced them.
    groups: dict[frozenset[str], list[tuple[str, dict[str, Any]]]] = {}
    for document_id, fact in facts:
        stems = _description_stems(_fact_value(fact))
        if stems:
            groups.setdefault(stems, []).append((document_id, fact))
    if not groups:
        return []

    # code -> (best matched, best share, distinct documents, basis facts)
    scored: dict[str, tuple[int, float, set[str], list[tuple[str, dict[str, Any]]]]] = {}
    for stems, group in groups.items():
        for code, entry_stems in _description_index():
            matched = len(stems & entry_stems)
            if matched == 0:
                continue
            share = matched / len(stems)
            current = scored.get(code)
            if current is None:
                scored[code] = (matched, share, {doc for doc, _ in group}, list(group))
                continue
            best_matched, best_share, documents, basis = current
            documents.update(doc for doc, _ in group)
            basis.extend(group)
            scored[code] = (max(best_matched, matched), max(best_share, share), documents, basis)
    ranked = sorted(
        scored.items(),
        key=lambda item: (-item[1][0], -item[1][1], -len(item[1][2]), item[0]),
    )

    candidates: list[HsCandidate] = []
    for code, (_, share, _, basis_facts) in ranked[:limit]:
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
                basis=[
                    HsCandidateBasis(
                        document_id=document_id,
                        fact_id=str(fact.get("fact_id", "")),
                        fact_name=str(fact.get("name", "")),
                        raw_value=str(fact.get("raw_value", ""))[:200],
                    )
                    for document_id, fact in basis_facts[:20]
                ],
            )
        )
    return candidates


def _code_candidates(
    *,
    code_documents: dict[str, set[str]],
    code_basis: dict[str, list[HsCandidateBasis]],
    documents_with_hs: set[str],
) -> list[HsCandidate]:
    total_documents = len(documents_with_hs)
    candidates: list[HsCandidate] = []
    best_share = 0.0
    for documents in code_documents.values():
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
    return candidates


class _CaseTextScan:
    """What the document text itself says: cited codes and item-name cells."""

    def __init__(self) -> None:
        self.code_basis: dict[str, list[HsCandidateBasis]] = {}
        self.code_documents: dict[str, set[str]] = {}
        self.documents_with_hs: set[str] = set()
        self.description_facts: list[tuple[str, dict[str, Any]]] = []


def _scan_case_text(documents: list[tuple[DocumentRecord, str]]) -> _CaseTextScan:
    scan = _CaseTextScan()
    code_basis, code_documents, documents_with_hs, description_facts = (
        scan.code_basis,
        scan.code_documents,
        scan.documents_with_hs,
        scan.description_facts,
    )
    for record, text in documents:
        lines = text.splitlines()[:_CASE_TEXT_LINE_LIMIT]
        for line_no, value in _description_values(lines):
            description_facts.append(
                (
                    record.document_id,
                    {
                        "fact_id": f"text-line-{line_no}",
                        "name": "description",
                        "raw_value": value,
                        "normalized_value": value,
                    },
                )
            )
        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if _HS_LINE_PATTERN.search(stripped):
                for code in _hs_codes_in(stripped):
                    documents_with_hs.add(record.document_id)
                    code_documents.setdefault(code, set()).add(record.document_id)
                    code_basis.setdefault(code, []).append(
                        HsCandidateBasis(
                            document_id=record.document_id,
                            fact_id=f"text-line-{line_no}",
                            fact_name="document_text",
                            raw_value=stripped[:200],
                        )
                    )
    return scan


def _reference_info() -> HsReferenceInfo:
    reference = hsk_names()
    return HsReferenceInfo(
        as_of=str(reference.get("as_of", "")),
        source=str(reference.get("source", "")),
    )


def build_case_hs_classification(
    case: CaseRecord, documents: list[tuple[DocumentRecord, str]]
) -> CaseHsClassificationView:
    """Offer HSK candidates from confirmed document text, before any review."""

    scan = _scan_case_text(documents)
    candidates = _code_candidates(
        code_documents=scan.code_documents,
        code_basis=scan.code_basis,
        documents_with_hs=scan.documents_with_hs,
    )
    if not candidates:
        candidates = _description_candidates_from_facts(scan.description_facts)
    return CaseHsClassificationView(
        case_id=case.case_id,
        reference=_reference_info(),
        candidates=candidates,
        selection=case.hs_selection,
    )


_AGENT_SHORTLIST_LIMIT = 40
_AGENT_TEXT_LIMIT = 20_000
_AGENT_SEARCH_CONFIDENCE = 0.3
_AGENT_CITED_PREFIX_PATTERN = re.compile(r"^[0-9]{6,9}$")


class AgentHsItem(BaseModel):
    """One commodity as the Agent read it from the documents.

    The Agent reads the confirmed text by meaning, so this is the layout- and
    language-independent starting point for the shortlist: the item name as
    written, the documents it appears in, any tariff code the documents cite
    for it, and Korean tariff-nomenclature terms for looking it up.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    description: str = Field(min_length=1, max_length=300)
    document_ids: list[str] = Field(default_factory=list)
    cited_hs_codes: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)


def agent_hs_items(response: dict[str, Any]) -> list[AgentHsItem]:
    """The goods the Agent read from the documents; malformed entries are dropped."""

    items: list[AgentHsItem] = []
    raw_items = response.get("items")
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            continue
        items.append(
            AgentHsItem(
                description=" ".join(description.split())[:300],
                document_ids=_string_list(raw.get("document_ids"), limit=20, max_length=128),
                cited_hs_codes=[
                    code
                    for code in _string_list(raw.get("cited_hs_codes"), limit=10, max_length=10)
                    if re.fullmatch(r"[0-9]{6,10}", code)
                ],
                search_terms=_string_list(raw.get("search_terms"), limit=10, max_length=80),
            )
        )
    return items[:20]


def _string_list(value: object, *, limit: int, max_length: int) -> list[str]:
    result: list[str] = []
    for raw in value if isinstance(value, list) else []:
        if isinstance(raw, str):
            text = " ".join(raw.split())
            if text and len(text) <= max_length and text not in result:
                result.append(text)
        if len(result) >= limit:
            break
    return result


def _agent_item_basis(item: AgentHsItem, index: int) -> list[HsCandidateBasis]:
    return [
        HsCandidateBasis(
            document_id=document_id,
            fact_id=f"agent-item-{index}",
            fact_name="description",
            raw_value=item.description[:200],
        )
        for document_id in (item.document_ids or ["agent"])
    ]


def _agent_search_queries(item: AgentHsItem) -> list[str]:
    """Reference-table lookups for one item: its product noun, all its terms, and cited prefixes."""

    queries: list[str] = []
    if item.search_terms:
        queries.append(item.search_terms[0])
        if len(item.search_terms) > 1:
            queries.append(" ".join(item.search_terms))
    # A 6- to 9-digit code on the documents names a heading or subheading;
    # every HSK leaf under it belongs in the shortlist.
    queries.extend(code for code in item.cited_hs_codes if _AGENT_CITED_PREFIX_PATTERN.match(code))
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _agent_search_candidates(items: list[AgentHsItem]) -> list[HsCandidate]:
    """Reference rows found by the Agent's Korean lookup terms and cited code prefixes."""

    candidates: list[HsCandidate] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        basis = _agent_item_basis(item, index)
        for query in _agent_search_queries(item):
            for result in search_hs_reference(query).results:
                if result.code in seen:
                    continue
                seen.add(result.code)
                candidates.append(
                    HsCandidate(
                        code=result.code,
                        name_ko=result.name_ko,
                        name_en=result.name_en,
                        ancestors=result.ancestors,
                        confidence=_AGENT_SEARCH_CONFIDENCE,
                        in_reference=True,
                        recommended=False,
                        origin="description_match",
                        basis=basis,
                    )
                )
    return candidates


def case_hs_shortlist(
    documents: list[tuple[DocumentRecord, str]],
    *,
    agent_items: list[AgentHsItem] | None = None,
) -> list[HsCandidate]:
    """Reference codes the Agent may choose among: cited codes first, then name matches.

    With ``agent_items`` (the Agent's own reading of the documents) the
    shortlist is built from what the Agent read -- item names, lookup terms
    and cited codes -- instead of from the layout-bound heuristic cells, so a
    document in an unfamiliar form or language still reaches the right rows.
    Codes on the documents' own tariff lines are kept in both cases.  Every
    entry still comes from the packaged HSK reference table.
    """

    scan = _scan_case_text(documents)
    items = list(agent_items or [])
    if items:
        scan.description_facts = []
    for index, item in enumerate(items, start=1):
        document_ids = item.document_ids or ["agent"]
        for code in item.cited_hs_codes:
            if len(code) != 10 or not hs_reference_has(code):
                continue
            for document_id in document_ids:
                scan.documents_with_hs.add(document_id)
                scan.code_documents.setdefault(code, set()).add(document_id)
                scan.code_basis.setdefault(code, []).append(
                    HsCandidateBasis(
                        document_id=document_id,
                        fact_id=f"agent-cited-{index}",
                        fact_name="cited_hs_code",
                        raw_value=code,
                    )
                )
        # The item name and each lookup term are matched separately (see
        # _description_candidates_from_facts); the basis always shows the
        # name as the Agent read it.
        for term_index, value in enumerate([item.description, *item.search_terms]):
            suffix = f"-term-{term_index}" if term_index else ""
            fact = {
                "fact_id": f"agent-item-{index}{suffix}",
                "name": "description",
                "raw_value": item.description[:200],
                "normalized_value": value[:200],
            }
            for document_id in document_ids:
                scan.description_facts.append((document_id, fact))
    shortlist = [
        candidate
        for candidate in _code_candidates(
            code_documents=scan.code_documents,
            code_basis=scan.code_basis,
            documents_with_hs=scan.documents_with_hs,
        )
        if candidate.in_reference
    ]
    seen = {candidate.code for candidate in shortlist}
    for candidate in [
        *_agent_search_candidates(items),
        *_description_candidates_from_facts(scan.description_facts, limit=_AGENT_SHORTLIST_LIMIT),
    ]:
        if candidate.code not in seen:
            seen.add(candidate.code)
            shortlist.append(candidate)
    return shortlist[:_AGENT_SHORTLIST_LIMIT]


def agent_hs_request(
    case_id: str,
    documents: list[tuple[DocumentRecord, str]],
    shortlist: list[HsCandidate],
    *,
    max_picks: int = 5,
) -> dict[str, Any]:
    """The Agent request: confirmed text plus the reference codes it may pick from.

    An empty ``shortlist`` asks for the extraction-only round: the Agent reads
    the goods and returns no picks because it has nothing to pick from.
    """

    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "documents": [
            {
                "document_id": record.document_id,
                "document_type": str(record.document_type),
                "content_text": text[:_AGENT_TEXT_LIMIT] or "-",
            }
            for record, text in documents
        ][:20],
        "reference_candidates": [
            {
                "code": candidate.code,
                "name_ko": candidate.name_ko,
                "name_en": candidate.name_en,
                "ancestors": [item.name_ko for item in candidate.ancestors if item.name_ko][:8],
            }
            for candidate in shortlist
        ][:_AGENT_SHORTLIST_LIMIT],
        "max_picks": max_picks,
    }


def apply_agent_hs_picks(
    view: CaseHsClassificationView,
    response: dict[str, Any],
    shortlist: list[HsCandidate],
) -> CaseHsClassificationView:
    """Put the Agent's ranked picks first; keep the heuristic candidates behind them.

    A pick is honoured only when its code is in the shortlist the Backend sent,
    so the reference table stays the single source of codes.
    """

    if response.get("status") != "ok":
        return view
    raw_picks = response.get("picks")
    if not isinstance(raw_picks, list) or not raw_picks:
        return view
    by_code = {candidate.code: candidate for candidate in shortlist}
    ranked: list[HsCandidate] = []
    for raw in raw_picks:
        if not isinstance(raw, dict):
            continue
        code = raw.get("code")
        reason = raw.get("reason")
        confidence = raw.get("confidence")
        if not isinstance(code, str) or code not in by_code or code in {c.code for c in ranked}:
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        base = by_code[code]
        ranked.append(
            base.model_copy(
                update={
                    "confidence": round(min(max(float(confidence), 0.0), 1.0), 4),
                    "recommended": not ranked,
                    "origin": "llm_match",
                    "explanation": reason.strip()[:400],
                }
            )
        )
    if not ranked:
        return view
    picked = {candidate.code for candidate in ranked}
    rest = [
        candidate.model_copy(update={"recommended": False})
        for candidate in view.candidates
        if candidate.code not in picked
    ]
    return view.model_copy(update={"basis_kind": "agent_ranked", "candidates": [*ranked, *rest][:10]})


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

    candidates = _code_candidates(
        code_documents=code_documents, code_basis=code_basis, documents_with_hs=documents_with_hs
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
    store = load_store()
    agreements = (
        store.agreement_hint_entries() if store is not None else duty_rates().get("agreements", {})
    )
    for code in sorted(agreements):
        entry = agreements[code]
        if not isinstance(entry, dict):
            continue
        raw_countries = entry.get("countries")
        countries = [str(item) for item in raw_countries] if isinstance(raw_countries, list) else []
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


def _rates_from_store(
    store: DutyReferenceStore,
    hs_code: str,
    origin_country: str | None,
    certificate_present: bool,
    certificate_origin_match: bool,
) -> list[DutyRate]:
    """Build the ladder for one HS code from the full reference dataset.

    Rows are grouped per 세율구분 family; the family's base row (no staging
    suffix) represents it in the ladder, and quota/staging variants are
    surfaced as conditional reference rows only for families the reviewer
    could actually apply.
    """
    rows = store.rates_for(hs_code)
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row.spec.family, []).append(row)

    rates: list[DutyRate] = []
    for _family, members in sorted(groups.items()):
        base = next((item for item in members if not item.spec.is_variant), None)
        representative = base or max(members, key=lambda item: item.rate_pct)
        spec = representative.spec
        requirements: list[DutyRateRequirement] = []
        if spec.category in ("basic", "wto"):
            applicable = True
        elif spec.category == "preferential":
            member_ok = (
                spec.countries is not None
                and origin_country is not None
                and origin_country in spec.countries
            )
            requirements = [
                DutyRateRequirement(code="origin_in_agreement", satisfied=member_ok)
            ]
            applicable = member_ok
        elif spec.category == "fta":
            origin_ok = (
                spec.countries is not None
                and origin_country is not None
                and origin_country in spec.countries
            )
            requirements = [
                DutyRateRequirement(code="origin_in_agreement", satisfied=origin_ok),
                DutyRateRequirement(code="certificate_present", satisfied=certificate_present),
                DutyRateRequirement(
                    code="certificate_origin_match", satisfied=certificate_origin_match
                ),
            ]
            applicable = all(item.satisfied for item in requirements)
        else:  # conditional: quota/safeguard/uncertain — display, never apply
            applicable = False
        rates.append(
            DutyRate(
                rate_kind=spec.category,  # type: ignore[arg-type]
                agreement_code=spec.agreement_code,
                code=representative.kind,
                label=spec.label,
                rate_pct=representative.rate_pct,
                source_uri=UNIPASS_SOURCE_URI,
                requirements=requirements,
                applicable=applicable,
            )
        )
        if applicable:
            for item in members:
                if item is representative:
                    continue
                rates.append(
                    DutyRate(
                        rate_kind="conditional",
                        agreement_code=spec.agreement_code,
                        code=item.kind,
                        label=f"{spec.label} · 세부구분 {item.kind}",
                        rate_pct=item.rate_pct,
                        source_uri=UNIPASS_SOURCE_URI,
                        requirements=[],
                        applicable=False,
                    )
                )
    return rates


def build_duty_assessment(run: ReviewRunRecord) -> DutyAssessmentView:
    dataset = duty_rates()
    store = load_store()
    reference = DutyReferenceInfo(
        as_of=store.as_of if store is not None else str(dataset.get("as_of", "")),
        source=store.source if store is not None else str(dataset.get("source", "")),
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

    certificate_present = origin.certificate is not None
    certificate_origin_match = any(
        check.field == "origin_country" and check.status == "match" for check in origin.checks
    )

    if store is not None:
        store_rates = _rates_from_store(
            store, hs_code.code, origin_country, certificate_present, certificate_origin_match
        )
        if not store_rates:
            return view("rates_unavailable", hs_code=hs_code, origin_country=origin_country)
        applicable = [item for item in store_rates if item.applicable]
        if not applicable:
            return view(
                "rates_unavailable",
                hs_code=hs_code,
                origin_country=origin_country,
                rates=store_rates,
            )
        return _estimate_view(view, run, hs_code, origin_country, store_rates)

    rate_entry = dataset.get("rates", {}).get(hs_code.code)
    if not isinstance(rate_entry, dict):
        return view("rates_unavailable", hs_code=hs_code, origin_country=origin_country)

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

    return _estimate_view(view, run, hs_code, origin_country, rates)


def _estimate_view(
    view: Any,
    run: ReviewRunRecord,
    hs_code: DutyHsCode,
    origin_country: str | None,
    rates: list[DutyRate],
) -> DutyAssessmentView:
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
    vat_rate = float(duty_rates().get("vat_rate_pct", 10.0))

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

# Retrieval aid only: common trade terms mapped to the reference table's own
# vocabulary.  Every result still comes solely from the packaged table and
# goes through the reviewer's pick-reason-confirm flow, so a mapping here can
# widen what a query finds but can never introduce a code.
_QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "전구": ("램프",),
    "형광등": ("램프",),
    "백열전구": ("백열램프",),
    "휴대폰": ("스마트폰", "전화기"),
    "핸드폰": ("스마트폰", "전화기"),
    "bulb": ("램프", "lamp"),
    "bulbs": ("램프", "lamp"),
}


@lru_cache(maxsize=1)
def _name_search_index() -> tuple[tuple[str, str, str, str], ...]:
    """(code, leaf text, leaf+heading text, leaf+all-ancestor text), lowered.

    Ancestor names contribute both Korean and English so English modifiers
    ("frozen", "stainless") have text to match; the HS6 sheet's 5-digit stem
    fallback mirrors _ancestors_for.  The heading text excludes the 2-digit
    chapter title, whose broad wording would otherwise let a chapter-level
    word ("…로프…") rank whole unrelated chapters ahead of the heading that
    actually names the item.
    """

    levels = hsk_names().get("levels", {})
    entries: list[tuple[str, str, str, str]] = []
    for code in sorted(levels.get("10", {})):
        name_ko, name_en = _leaf_names(code)
        leaf_text = " ".join(part for part in (name_ko, name_en) if part).lower()
        heading_parts: list[str] = []
        chapter_parts: list[str] = []
        for level, lengths in ((2, (2,)), (4, (4,)), (6, (6, 5))):
            table = levels.get(str(level), {})
            for length in lengths:
                entry = table.get(code[:length])
                if isinstance(entry, list):
                    bucket = chapter_parts if level == 2 else heading_parts
                    bucket.extend(name for name in entry if isinstance(name, str))
                    break
        heading_text = f"{leaf_text} {' '.join(heading_parts).lower()}"
        full_text = f"{heading_text} {' '.join(chapter_parts).lower()}"
        entries.append((code, leaf_text, heading_text, full_text))
    return tuple(entries)


_LATIN_CHARS = re.compile(r"[a-z0-9]")
_HANGUL_COMPOUND = re.compile(r"[가-힣]{4,}")


def _token_variants(token: str) -> tuple[str, ...]:
    """Deterministic spelling variants: hyphen/apostrophe joins and plural -s."""

    variants = [token]
    compact = token.replace("-", "").replace("'", "")
    if compact != token and len(compact) >= 2:
        variants.append(compact)
    spaced = token.replace("-", " ")
    if spaced != token:
        variants.append(spaced)
    for base in (token, compact):
        if len(base) > 3 and base.endswith("s") and not base.endswith("ss"):
            variants.append(base[:-1])
        if _LATIN_CHARS.search(base) and not base.endswith("s"):
            variants.append(f"{base}s")
    return tuple(dict.fromkeys(variants))


@lru_cache(maxsize=512)
def _bounded_pattern(fragment: str) -> re.Pattern[str]:
    """The fragment must not sit inside a longer Korean word ("네프로프스")."""

    return re.compile(rf"(?<![가-힣]){re.escape(fragment)}(?![가-힣])")


@lru_cache(maxsize=512)
def _latin_bounded_pattern(fragment: str) -> re.Pattern[str]:
    """The fragment must not sit inside a longer Latin word ("nightshirts")."""

    return re.compile(rf"(?<![a-z0-9]){re.escape(fragment)}(?![a-z0-9])")


def _fragment_in(fragment: str, text: str) -> bool:
    # Latin fragments match whole words only.  Korean fragments stay plain
    # substrings because the nomenclature agglutinates suffixes ("전구용",
    # "백열램프") that a boundary rule would wrongly reject.  The plain
    # substring test runs first because it is far cheaper than the regex
    # and rules out almost every row.
    if fragment not in text:
        return False
    if _LATIN_CHARS.search(fragment):
        return _latin_bounded_pattern(fragment).search(text) is not None
    return True


def _token_hit(variants: tuple[str, ...], synonyms: tuple[str, ...], text: str) -> int:
    """2 = the word (or a variant/synonym) appears; 1 = half of a compound does."""

    for variant in variants:
        if _fragment_in(variant, text):
            return 2
    for synonym in synonyms:
        for synonym_variant in _token_variants(synonym):
            if _fragment_in(synonym_variant, text):
                return 2
    token = variants[0]
    if _HANGUL_COMPOUND.fullmatch(token):
        # Korean compounds are written without spaces ("와이어로프"); credit a
        # split whose halves both appear, or half-credit a single half.  A
        # half only counts at a word boundary so it cannot match inside a
        # longer transliterated word.
        best = 0
        for split in range(2, len(token) - 1):
            left, right = token[:split], token[split:]
            left_in = left in text and _bounded_pattern(left).search(text) is not None
            right_in = right in text and _bounded_pattern(right).search(text) is not None
            if left_in and right_in:
                return 2
            if left_in or right_in:
                best = 1
        return best
    return 0


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

    A digits-only query is a code-prefix search.  Anything else matches query
    words against the row's Korean/English names (leaf plus ancestors): rows
    matching every word rank first; when no row matches every word, rows are
    ranked by how many words they match and the view is flagged ``partial``.
    Both modes are deterministic lookups over the packaged table — no scoring
    model, no external calls.
    """

    reference = hsk_names()
    cleaned = query.strip()
    compact = cleaned.replace(" ", "")
    matched: list[str]
    partial = False
    if _SEARCH_CODE_PATTERN.fullmatch(compact):
        mode: Literal["code_prefix", "name"] = "code_prefix"
        matched = [
            code for code, _, _, _ in _name_search_index() if code.startswith(compact)
        ]
    else:
        mode = "name"
        prepared = [
            (_token_variants(token), _QUERY_SYNONYMS.get(token, ()))
            for token in cleaned.lower().split()
            if token
        ]
        scored: list[tuple[bool, int, int, int, str]] = []
        for code, leaf_text, heading_text, full_text in _name_search_index():
            hits = [_token_hit(variants, synonyms, full_text) for variants, synonyms in prepared]
            # Korean and English noun phrases are head-final ("스테인리스
            # 볼트", "stainless steel bolts"), so the last word counts twice:
            # rows matching the head noun outrank rows matching a modifier.
            score = sum(hits) + (hits[-1] if hits else 0)
            if score == 0:
                continue
            all_words = bool(hits) and all(hit == 2 for hit in hits)
            leaf_score = sum(
                _token_hit(variants, synonyms, leaf_text) for variants, synonyms in prepared
            )
            heading_score = sum(
                _token_hit(variants, synonyms, heading_text) for variants, synonyms in prepared
            )
            scored.append((all_words, score, leaf_score, heading_score, code))
        if any(item[0] for item in scored):
            scored = [item for item in scored if item[0]]
        else:
            partial = bool(scored)
        scored.sort(key=lambda item: (-item[1], -item[2], -item[3], item[4]))
        matched = [code for _, _, _, _, code in scored]

    return HsReferenceSearchView(
        query=cleaned,
        mode=mode,
        results=[_search_result(code) for code in matched[:_SEARCH_RESULT_LIMIT]],
        truncated=len(matched) > _SEARCH_RESULT_LIMIT,
        partial=partial,
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
