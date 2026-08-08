"""Fail-closed text boundary for Case-independent knowledge conversations.

The standalone knowledge assistant has no legitimate reason to receive a Case
identifier, a review identifier, a source filename, or pasted document fields.
This module intentionally detects only high-confidence structural markers.  It
does not attempt to classify ordinary Korean trade questions, and it never
returns or logs the supplied text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from customs_backend.ports import KnowledgeInputRejectedError


_FILENAME = re.compile(
    r"(?ix)(?:^|[\s\"'`(])[^\s\"'`()]{1,180}\.(?:"
    r"txt|pdf|docx?|xlsx?|csv|xml|json|eml|msg|jpe?g|png|tiff?)(?=$|[\s\"'`).,;:])"
)
_INTERNAL_IDENTIFIER = re.compile(
    r"(?ix)(?:"
    r"\b(?:case|review(?:[_ -]?run)?|document|finding|comparison|evidence|proposal|"
    r"job|message|fact)[_-][a-z0-9][a-z0-9_-]{5,}\b"
    r"|\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b"
    r"|\b[a-f0-9]{24,}\b"
    r"|\b(?:sha(?:256)?|hash)\s*[:=]\s*[a-f0-9]{16,}\b"
    r")"
)
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

_LINE_BREAKS = frozenset(
    {"\r", "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}
)


def _has_line_break(value: str) -> bool:
    """Recognise every Python text line separator, not only CR/LF."""

    return any(character in _LINE_BREAKS for character in value)


def _looks_like_unsafe_knowledge_input(value: str) -> bool:
    """Return only a boolean so the caller cannot accidentally surface text."""

    if _FILENAME.search(value) or _INTERNAL_IDENTIFIER.search(value):
        return True
    lines = value.splitlines()
    if _DOCUMENT_HEADER.fullmatch(value):
        return True
    # One label can occur in a legitimate question.  Two labelled lines are a
    # high-confidence pasted record and are disallowed only in this global,
    # Case-independent channel.
    return len(lines) > 1 and sum(bool(_STRUCTURAL_FIELD.match(line)) for line in lines) >= 2


def require_safe_knowledge_title(title: str | None) -> None:
    # A standalone knowledge conversation never needs a caller-authored
    # multiline title.  Reject it before persistence even if it happens not
    # to contain two recognised document labels; otherwise a pasted document
    # could become durable list text through this smaller side channel.
    if title is not None and (_has_line_break(title) or _looks_like_unsafe_knowledge_input(title)):
        raise KnowledgeInputRejectedError()


def canonical_knowledge_title(
    title: str | None, trade_type: Literal["import", "export", "both"]
) -> str:
    """Return the only durable title shape for a global knowledge thread.

    The public request retains its optional ``title`` field for additive API
    compatibility, but this Case-independent channel must not turn arbitrary
    caller prose into a durable label.  A safe one-line legacy/custom title is
    therefore accepted but deliberately normalised to a small trade-direction
    label; unsafe title input is rejected above.
    """

    require_safe_knowledge_title(title)
    return {
        "import": "수입 통관 지식 대화",
        "export": "수출 통관 지식 대화",
        "both": "통관 지식 대화",
    }[trade_type]


def require_safe_knowledge_message(content: str) -> None:
    if _has_line_break(content) or _looks_like_unsafe_knowledge_input(content):
        raise KnowledgeInputRejectedError()


def require_safe_knowledge_history(*, title: str | None, turns: Iterable[tuple[str, str]]) -> None:
    """Reject legacy unsafe context without deleting or rewriting it.

    The caller deliberately performs this check before constructing the Agent
    request.  It therefore keeps historical raw content out of Agent and
    Claude while preserving the existing database records for audit/recovery.
    User text retains the strict one-line boundary.  Agent-authored assistant
    prose may legitimately contain paragraph/list separators, so it keeps
    those separators but is still rejected for every document or internal
    structural marker.
    """

    require_safe_knowledge_title(title)
    for role, content in turns:
        if role == "user":
            require_safe_knowledge_message(content)
        elif role == "assistant":
            if _looks_like_unsafe_knowledge_input(content):
                raise KnowledgeInputRejectedError()
        else:
            raise KnowledgeInputRejectedError()
