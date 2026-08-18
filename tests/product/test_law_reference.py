"""Reviewer-facing statute search and catalog proxied through the Agent.

The unit tests exercise request building and response validation; the endpoint
tests drive the API client against the deterministic fixture Agent.
"""

from __future__ import annotations

from datetime import date

import pytest

from customs_backend.law_reference import (
    LawReferenceError,
    build_law_search_request,
    law_catalog_view,
    law_search_view,
)

from tests.product.test_workbench_api import _auth, _client


def test_law_search_request_is_bounded_and_deterministic() -> None:
    first = build_law_search_request("  원산지증명서  ", None, date(2026, 8, 18))
    second = build_law_search_request("원산지증명서", None, date(2026, 8, 18))
    assert first == second
    assert first["query"] == "원산지증명서"
    assert first["top_k"] == 10
    with pytest.raises(LawReferenceError):
        build_law_search_request("   ", None, date(2026, 8, 18))
    with pytest.raises(LawReferenceError):
        build_law_search_request("관세", 21, date(2026, 8, 18))


def test_law_search_view_rejects_non_official_source_links() -> None:
    response = {
        "as_of": "2026-08-18",
        "results": [
            {
                "law_name": "관세법",
                "title": "관세법 제226조",
                "text": "조문",
                "source_uri": "https://evil.example/law",
                "score": 0.5,
            }
        ],
    }
    with pytest.raises(LawReferenceError):
        law_search_view("관세", response)


def test_law_catalog_view_validates_entries() -> None:
    view = law_catalog_view(
        {
            "as_of": "2026-08-18",
            "laws": [
                {"law_id": "L1", "law_name": "관세법", "hierarchy": "법률", "article_count": 280}
            ],
        }
    )
    assert view.laws[0].law_name == "관세법"
    with pytest.raises(LawReferenceError):
        law_catalog_view({"as_of": "2026-08-18", "laws": [{"law_id": "", "law_name": "x", "article_count": 1}]})


def test_law_reference_endpoints_round_trip() -> None:
    with _client() as client:
        search = client.get("/api/v1/law-search", params={"q": "허가 승인"}, headers=_auth())
        assert search.status_code == 200
        body = search.json()
        assert body["query"] == "허가 승인"
        (result,) = body["results"]
        assert result["law_name"] == "관세법"
        assert result["source_uri"].startswith("https://www.law.go.kr/")

        catalog = client.get("/api/v1/laws", headers=_auth())
        assert catalog.status_code == 200
        laws = catalog.json()["laws"]
        assert [law["law_name"] for law in laws] == ["관세법", "관세법 시행령"]

        empty = client.get("/api/v1/law-search", params={"q": ""}, headers=_auth())
        assert empty.status_code == 422
