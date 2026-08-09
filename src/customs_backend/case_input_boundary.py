"""Fail-closed forwarding boundary for Case-assistant document-shaped text.

Case conversations may retain a user's raw text for authorised audit access,
but a pasted document is never a new Review Run input.  This module identifies
only high-confidence document-shaped multi-line input and provides a fixed,
non-sensitive substitute for Agent context.  It deliberately does *not*
reject ordinary one-line document names, numbers, or business values.
"""

from __future__ import annotations

import re
from typing import Literal


CaseInputNotice = Literal["unregistered_document_text"]

# Keep this compatible with the Agent's direct-caller guard.  Backend is the
# earlier boundary, so its label set deliberately includes every Agent field
# label as well as Backend-only Case/review identifiers.  Do not import the
# sibling implementation: repository boundaries require each service to own
# its public-input guard.
_STRUCTURAL_FIELD = re.compile(
    r"(?ix)(?:"
    # Exact Agent labels first: this prevents whitespace/form variations from
    # becoming a Backend-only gap.
    r"invoice\s*(?:no|number)|packing\s*(?:no|number)|"
    r"b\s*/?\s*l\s*(?:no|number)?|awb\s*(?:no|number)?|"
    r"hs\s*code|description|quantity|unit\s*price|amount|"
    r"total(?:\s+amount)?|country\s+of\s+origin|submission\s*(?:no|date|status)|"
    r"신고번호|접수번호|제출일|처리상태|품명|수량|단가|금액|총액|원산지|hs\s*코드|"
    # Backend-only structural labels follow.
    r"case(?:[_ -]?id)?|review(?:[_ -]?(?:run|id))?|review_run_id|"
    r"document(?:[_ -]?id|[_ -]?type)?|filename|content(?:[_ -]?text)?|"
    r"finding(?:[_ -]?id)?|comparison(?:[_ -]?id)?|evidence(?:[_ -]?id)?|"
    r"proposal(?:[_ -]?id)?|job(?:[_ -]?id)?|message(?:[_ -]?id)?|"
    r"invoice(?:[_ -]?(?:no|number))?|packing(?:[_ -]?(?:no|number))?|"
    r"(?:b/?l|awb)(?:[_ -]?(?:no|number))?|container(?:[_ -]?(?:no|number))?|"
    r"hs(?:[_ -]?code)?|description|quantity|unit[_ -]?price|amount|"
    r"total(?:[_ -]?amount)?|currency|weight|origin(?:[_ -]?country)?|"
    r"destination(?:[_ -]?country)?|submission(?:[_ -]?(?:status|date))?|"
    r"(?:사건|심사)(?:\s*(?:id|번호|실행))?|문서(?:\s*(?:id|유형|명))?|파일명|"
    r"품명|수량|단가|금액|총액|통화|중량|원산지|도착국|컨테이너(?:\s*번호)?|"
    r"hs\s*코드|신고(?:\s*(?:번호|일자|상태))?|접수(?:\s*(?:번호|일자|상태))?|"
    r"제출일|처리상태|"
    r"수리(?:\s*(?:번호|일자|상태))?"
    r")\s*[:=：]"
)
_DOCUMENT_HEADING = re.compile(
    r"(?ix)(?:"
    r"commercial\s+invoice|invoice|packing\s+list|bill\s+of\s+lading|"
    r"air\s+waybill|certificate\s+of\s+origin|export\s+declaration|supporting\s+evidence|"
    r"상업송장|포장명세서|선하증권|항공화물운송장|원산지증명서|"
    r"수출신고(?:서|증|필증)?|보완(?:\s*증빙)?"
    r")"
)

# These messages are deliberately code owned.  They contain no user text,
# document ID, filename, or case/review identifier and are therefore safe for
# the Agent HTTP request and Functional-UAT transcript.
CASE_DOCUMENT_CONTEXT_PLACEHOLDER = (
    "[문서형 붙여넣기 내용은 새 심사 근거가 아니므로 Agent 문맥에서 제외되었습니다.]"
)
CASE_DOCUMENT_INPUT_RESPONSE = (
    "붙여넣은 문서형 내용은 현재 심사의 새 근거로 사용하지 않았습니다. "
    "반영하려면 문서를 등록하거나 교체한 뒤 새 Review Run으로 재심사해 주세요."
)


def case_input_notice(content: str) -> CaseInputNotice | None:
    """Return a public-safe notice only for a likely pasted document record.

    A line break alone is not enough: reviewers may use paragraphs or lists.
    The input becomes document-shaped only when it contains a recognised
    document heading plus structured rows, a two-or-more-field record, or a
    table.  The first three branches mirror the Agent's direct-caller guard
    so a Case paste cannot cross the Backend boundary first. This preserves
    short one-line names, identifiers, and business values.
    """

    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    field_count = sum(bool(_STRUCTURAL_FIELD.search(line)) for line in lines)
    if field_count >= 2:
        return "unregistered_document_text"
    if any(_DOCUMENT_HEADING.search(line) for line in lines[:2]) and field_count >= 1:
        return "unregistered_document_text"
    # Agent recognizes a four-line table with two delimiter rows even when a
    # cell has one separator only. Keep that exact lower bound and retain the
    # Backend's stricter short-table detection below.
    agent_table_rows = sum("|" in line or "\t" in line for line in lines)
    if len(lines) >= 4 and agent_table_rows >= 2:
        return "unregistered_document_text"
    # A pasted grid may contain a header row that does not use a colon. Two
    # dense delimiter-bearing rows are also high-confidence on shorter input.
    delimiter_rows = sum(
        1
        for line in lines
        if (line.count("|") >= 2 or line.count("\t") >= 2)
        and any(character.isalnum() for character in line)
    )
    if delimiter_rows >= 2:
        return "unregistered_document_text"
    return None


def safe_case_turn_content(*, role: str, content: str) -> str:
    """Remove legacy raw document-shaped content before Agent context use."""

    if role in {"user", "assistant"} and case_input_notice(content) is not None:
        return CASE_DOCUMENT_CONTEXT_PLACEHOLDER
    return content


def safe_case_transcript_content(content: str) -> str:
    """Redact only document-shaped Case text from Functional-UAT artefacts."""

    return (
        CASE_DOCUMENT_CONTEXT_PLACEHOLDER
        if case_input_notice(content) is not None
        else content
    )


__all__ = [
    "CASE_DOCUMENT_CONTEXT_PLACEHOLDER",
    "CASE_DOCUMENT_INPUT_RESPONSE",
    "CaseInputNotice",
    "case_input_notice",
    "safe_case_transcript_content",
    "safe_case_turn_content",
]
