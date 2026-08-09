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

_STRUCTURAL_FIELD = re.compile(
    r"(?ix)^\s*(?:"
    r"case(?:[_ -]?id)?|review(?:[_ -]?(?:run|id))?|review_run_id|"
    r"document(?:[_ -]?id|[_ -]?type)?|filename|content(?:[_ -]?text)?|"
    r"finding(?:[_ -]?id)?|comparison(?:[_ -]?id)?|evidence(?:[_ -]?id)?|"
    r"proposal(?:[_ -]?id)?|job(?:[_ -]?id)?|message(?:[_ -]?id)?|"
    r"invoice(?:[_ -]?(?:no|number))?|packing(?:[_ -]?(?:no|number))?|"
    r"(?:b/?l|awb)(?:[_ -]?(?:no|number))?|container(?:[_ -]?(?:no|number))?|"
    r"hs(?:[_ -]?code)?|quantity|amount|currency|weight|origin(?:[_ -]?country)?|"
    r"destination(?:[_ -]?country)?|submission(?:[_ -]?(?:status|date))?|"
    r"(?:사건|심사)(?:\s*(?:id|번호|실행))?|문서(?:\s*(?:id|유형|명))?|파일명|"
    r"수량|금액|통화|중량|원산지|도착국|컨테이너(?:\s*번호)?|"
    r"hs\s*코드|신고(?:\s*(?:번호|일자|상태))?|접수(?:\s*(?:번호|일자|상태))?|"
    r"수리(?:\s*(?:번호|일자|상태))?"
    r")\s*[:=：]"
)
_DOCUMENT_HEADER = re.compile(
    r"(?ix)^\s*(?:"
    r"commercial\s+invoice|packing\s+list|bill\s+of\s+lading|certificate\s+of\s+origin|"
    r"air\s+waybill|export\s+declaration|supporting\s+evidence|"
    r"상업송장|포장명세서|선하증권|원산지증명서|수출신고(?:서|필증)?|보완(?:\s*증빙)?"
    r")\s*$"
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
    repeated table delimiter.  This preserves short names/identifiers and
    ordinary multi-line questions while protecting raw document bodies.
    """

    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    field_count = sum(bool(_STRUCTURAL_FIELD.match(line)) for line in lines)
    if field_count >= 2:
        return "unregistered_document_text"
    if _DOCUMENT_HEADER.fullmatch(lines[0]) and field_count >= 1:
        return "unregistered_document_text"
    # A pasted grid may contain a header row that does not use a colon.  Two
    # delimiter-bearing rows is high-confidence while a single Markdown table
    # example in a normal question remains allowed.
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
