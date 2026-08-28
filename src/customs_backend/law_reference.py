"""Reviewer-facing statute reference views proxied through the Agent to Data.

These are read-only reference lookups over the loaded 65-statute store: they
carry no case, item, or HS meaning, never contribute to a review result, and
every field shown to a person comes verbatim from the Data store's own rows.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_MAX_QUERY_LENGTH = 200
_MAX_TOP_K = 20
_DEFAULT_TOP_K = 10


class LawReferenceError(ValueError):
    """The Browser request or the upstream reference payload is invalid."""


class LawSearchResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    law_id: str | None = None
    law_name: str
    article_no: str | None = None
    title: str
    text: str
    source_uri: str
    valid_from: str | None = None
    valid_to: str | None = None
    score: float = Field(ge=0.0, le=1.0)


class LawSearchView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    as_of: str
    results: list[LawSearchResultView] = Field(max_length=_MAX_TOP_K)


class LawCatalogEntryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    law_id: str
    law_name: str
    hierarchy: str | None = None
    article_count: int = Field(ge=0)


class LawCatalogView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: str
    laws: list[LawCatalogEntryView] = Field(max_length=500)


def _request_id(prefix: str, seed: str) -> str:
    return f"{prefix}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def build_law_search_request(query: str, top_k: int | None, as_of: date) -> dict[str, Any]:
    cleaned = query.strip()
    if not 1 <= len(cleaned) <= _MAX_QUERY_LENGTH:
        raise LawReferenceError("a law search query must be 1-200 characters")
    bounded_top_k = _DEFAULT_TOP_K if top_k is None else top_k
    if not 1 <= bounded_top_k <= _MAX_TOP_K:
        raise LawReferenceError("a law search top_k must be between 1 and 20")
    return {
        "schema_version": "3.0",
        "request_id": _request_id("law-search", f"{cleaned}|{bounded_top_k}|{as_of.isoformat()}"),
        "query": cleaned,
        "top_k": bounded_top_k,
        "as_of": as_of.isoformat(),
    }


def build_law_catalog_request(as_of: date) -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "request_id": _request_id("law-catalog", as_of.isoformat()),
        "as_of": as_of.isoformat(),
    }


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def law_search_view(query: str, response: dict[str, Any]) -> LawSearchView:
    results_raw = response.get("results")
    as_of = response.get("as_of")
    if not isinstance(results_raw, list) or not isinstance(as_of, str):
        raise LawReferenceError("the law search response is invalid")
    results: list[LawSearchResultView] = []
    for entry in results_raw[:_MAX_TOP_K]:
        if not isinstance(entry, dict):
            raise LawReferenceError("the law search response is invalid")
        law_name = entry.get("law_name")
        title = entry.get("title")
        text = entry.get("text")
        source_uri = entry.get("source_uri")
        score = entry.get("score")
        if (
            not isinstance(law_name, str)
            or not isinstance(title, str)
            or not isinstance(text, str)
            or not isinstance(source_uri, str)
            or not source_uri.startswith("https://www.law.go.kr/")
            or not isinstance(score, (int, float))
        ):
            raise LawReferenceError("the law search response is invalid")
        results.append(
            LawSearchResultView(
                law_id=_optional_text(entry.get("law_id")),
                law_name=law_name,
                article_no=_optional_text(entry.get("article_no")),
                title=title,
                text=text,
                source_uri=source_uri,
                valid_from=_optional_text(entry.get("valid_from")),
                valid_to=_optional_text(entry.get("valid_to")),
                score=max(0.0, min(1.0, float(score))),
            )
        )
    return LawSearchView(query=query.strip(), as_of=as_of, results=results)


def law_catalog_view(response: dict[str, Any]) -> LawCatalogView:
    laws_raw = response.get("laws")
    as_of = response.get("as_of")
    if not isinstance(laws_raw, list) or not isinstance(as_of, str):
        raise LawReferenceError("the law catalog response is invalid")
    laws: list[LawCatalogEntryView] = []
    for entry in laws_raw[:500]:
        if not isinstance(entry, dict):
            raise LawReferenceError("the law catalog response is invalid")
        law_id = entry.get("law_id")
        law_name = entry.get("law_name")
        article_count = entry.get("article_count")
        if (
            not isinstance(law_id, str)
            or not law_id
            or not isinstance(law_name, str)
            or not law_name
            or not isinstance(article_count, int)
            or article_count < 0
        ):
            raise LawReferenceError("the law catalog response is invalid")
        laws.append(
            LawCatalogEntryView(
                law_id=law_id,
                law_name=law_name,
                hierarchy=_optional_text(entry.get("hierarchy")),
                article_count=article_count,
            )
        )
    return LawCatalogView(as_of=as_of, laws=laws)


__all__ = [
    "LawCatalogEntryView",
    "LawCatalogView",
    "LawReferenceError",
    "LawSearchResultView",
    "LawSearchView",
    "build_law_catalog_request",
    "build_law_search_request",
    "law_catalog_view",
    "law_search_view",
]
