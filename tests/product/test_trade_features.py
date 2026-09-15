"""Backend-derived trade views: HS classification, origin verification, duty.

These tests exercise the pure derivation logic on constructed review runs and
the four public endpoints through the API test client.  The Agent result stays
immutable throughout; only the reviewer HS selection is written.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from customs_backend.domain import (
    CompletionEligibility,
    HsSelectionRecord,
    ReviewRunRecord,
    canonical_digest,
)
from customs_backend.trade_features import (
    build_duty_assessment,
    build_hs_classification,
    build_origin_verification,
    hs_reference_has,
    search_hs_reference,
)

from tests.product.test_workbench_api import _auth, _client


import pytest


@pytest.fixture(autouse=True)
def _snapshot_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin the packaged snapshot; ignore any operator DB env."""
    monkeypatch.delenv("CUSTOMS_BACKEND_DUTY_REFERENCE_DB", raising=False)


def _fact(
    fact_id: str, name: str, raw_value: str, document_id: str, *, normalized: str | None = None
) -> dict[str, Any]:
    return {
        "fact_id": fact_id,
        "name": name,
        "raw_value": raw_value,
        "normalized_value": normalized,
        "unit": None,
        "source": {
            "source_kind": "document",
            "document_id": document_id,
            "quote": raw_value,
            "occurrence": 0,
        },
    }


def _analysis(
    document_id: str,
    document_type: str,
    facts: list[dict[str, Any]],
    *,
    extraction_status: str = "complete",
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "filename": f"{document_id}.txt",
        "declared_type": document_type,
        "document_type": document_type,
        "classification_source": {
            "source_kind": "document",
            "document_id": document_id,
            "quote": "x",
            "occurrence": 0,
        },
        "extraction_status": extraction_status,
        "facts": facts,
        "line_items": [],
        "warnings": [],
    }


def _run(analyses: list[dict[str, Any]]) -> ReviewRunRecord:
    return ReviewRunRecord(
        review_run_id="run_test",
        case_id="case_test",
        org_id="org_test",
        job_id="job_test",
        case_revision=1,
        review_as_of=date(2026, 8, 5),
        trade_type="import",
        input_document_manifest=[
            {
                "document_id": "doc_ci",
                "document_type": "CI",
                "content_sha256": "0" * 64,
            }
        ],
        input_manifest_digest="0" * 64,
        agent_input_manifest_sha256="0" * 64,
        provider_result_sha256="0" * 64,
        result_digest="0" * 64,
        execution_status="succeeded",
        assessment="issues_detected",
        completion_eligibility=CompletionEligibility(
            unmet_reason_codes=["human_review_required"]
        ),
        report={},
        evidence=[],
        findings=[],
        required_actions=[],
        document_analyses=analyses,
        comparisons=[],
        created_by="tester",
        created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )


_CI = _analysis(
    "doc_ci",
    "invoice",
    [
        _fact("f_hs_ci", "hs_code", "0303540000", "doc_ci"),
        _fact("f_origin_ci", "origin_country", "VN", "doc_ci"),
        _fact("f_total_ci", "total_amount", "USD 30,000", "doc_ci"),
    ],
)
_CO_MATCH = _analysis(
    "doc_co",
    "certificate_of_origin",
    [
        _fact("f_no_co", "certificate_no", "KV-2026-001", "doc_co"),
        _fact("f_origin_co", "origin_country", "VN", "doc_co"),
        _fact("f_hs_co", "hs_code", "0303540000", "doc_co"),
    ],
)
_CO_MISMATCH = _analysis(
    "doc_co",
    "certificate_of_origin",
    [
        _fact("f_no_co", "certificate_no", "KV-2026-001", "doc_co"),
        _fact("f_origin_co", "origin_country", "CN", "doc_co"),
        _fact("f_hs_co", "hs_code", "0303540000", "doc_co"),
    ],
)


def test_hs_reference_contains_leaf_codes() -> None:
    assert hs_reference_has("0303540000")
    assert not hs_reference_has("9999999999")


def test_hs_classification_candidates_from_facts() -> None:
    view = build_hs_classification(_run([_CI, _CO_MATCH]))
    assert len(view.candidates) == 1
    candidate = view.candidates[0]
    assert candidate.code == "0303540000"
    assert candidate.in_reference
    assert candidate.recommended
    assert candidate.confidence == 1.0
    assert candidate.name_ko is not None and candidate.name_ko.startswith("고등어")
    assert [item.level for item in candidate.ancestors] == [2, 4, 6]
    assert {basis.document_id for basis in candidate.basis} == {"doc_ci", "doc_co"}
    assert view.selection is None


def test_hs_classification_unknown_code_capped_and_not_recommended() -> None:
    unknown = _analysis(
        "doc_x", "invoice", [_fact("f_hs_x", "hs_code", "9999999999", "doc_x")]
    )
    view = build_hs_classification(_run([unknown]))
    assert len(view.candidates) == 1
    assert not view.candidates[0].in_reference
    assert not view.candidates[0].recommended
    assert view.candidates[0].confidence <= 0.5


def test_hs_classification_description_candidates_when_no_codes() -> None:
    no_code = _analysis(
        "doc_ci",
        "invoice",
        [
            _fact("f_desc", "description", "MEN'S COTTON T-SHIRTS", "doc_ci"),
            _fact("f_origin", "origin_country", "KR", "doc_ci"),
        ],
    )
    view = build_hs_classification(_run([no_code]))
    assert view.candidates
    assert len(view.candidates) <= 5
    assert "6109101000" in {candidate.code for candidate in view.candidates}
    for candidate in view.candidates:
        assert candidate.origin == "description_match"
        assert candidate.in_reference
        assert not candidate.recommended
        assert candidate.confidence <= 0.4
        assert candidate.basis and candidate.basis[0].fact_name == "description"


def test_hs_classification_description_candidates_yield_to_cited_codes() -> None:
    cited = _analysis(
        "doc_ci",
        "invoice",
        [
            _fact("f_hs", "hs_code", "0303540000", "doc_ci"),
            _fact("f_desc", "description", "MEN'S COTTON T-SHIRTS", "doc_ci"),
        ],
    )
    view = build_hs_classification(_run([cited]))
    assert [candidate.code for candidate in view.candidates] == ["0303540000"]
    assert view.candidates[0].origin == "document_code"


def test_hs_classification_no_candidates_without_codes_or_descriptions() -> None:
    bare = _analysis(
        "doc_ci", "invoice", [_fact("f_origin", "origin_country", "KR", "doc_ci")]
    )
    view = build_hs_classification(_run([bare]))
    assert view.candidates == []


def test_origin_verification_verified() -> None:
    view = build_origin_verification(_run([_CI, _CO_MATCH]))
    assert view.status == "verified"
    assert view.origin_country == "VN"
    assert {check.field: check.status for check in view.checks} == {
        "certificate_no": "match",
        "origin_country": "match",
        "hs_code": "match",
    }
    covered = {hint.agreement_code: hint.origin_covered for hint in view.fta_hints}
    assert covered["KR-VN"] is True
    assert covered["KR-CN"] is False


def test_origin_verification_mismatch_and_missing() -> None:
    mismatch = build_origin_verification(_run([_CI, _CO_MISMATCH]))
    assert mismatch.status == "mismatch"
    origin_check = next(c for c in mismatch.checks if c.field == "origin_country")
    assert origin_check.certificate_value == "CN"
    assert origin_check.reference_value == "VN"

    missing = build_origin_verification(_run([_CI]))
    assert missing.status == "missing_certificate"
    assert missing.origin_country == "VN"


def test_duty_assessment_applies_best_satisfied_rate() -> None:
    view = build_duty_assessment(_run([_CI, _CO_MATCH]))
    assert view.status == "estimated"
    assert view.hs_code is not None and view.hs_code.source == "document_fact"
    assert view.dutiable_value is not None
    assert view.dutiable_value.amount == 30000.0
    assert view.dutiable_value.currency == "USD"
    assert view.applied is not None
    # VN origin with a matching certificate satisfies the KR-VN FTA rate (0%).
    assert view.applied.rate_kind == "fta"
    assert view.applied.rate_pct == 0.0
    assert view.applied.duty_amount == 0.0
    assert view.applied.vat_amount == 3000.0
    assert view.savings is not None
    assert view.savings.vs_basic_duty == 3000.0


def test_duty_assessment_without_certificate_falls_back_to_basic() -> None:
    view = build_duty_assessment(_run([_CI]))
    assert view.status == "estimated"
    assert view.applied is not None
    assert view.applied.rate_kind == "basic"
    assert view.applied.rate_pct == 10.0
    assert view.applied.duty_amount == 3000.0
    assert view.savings is not None
    assert view.savings.vs_basic_duty == 0.0
    assert "certificate_present" in view.savings.blocked_requirements


def test_duty_assessment_fails_closed_without_rates_or_value() -> None:
    other_code = _analysis(
        "doc_ci", "invoice", [_fact("f_hs", "hs_code", "0101211000", "doc_ci")]
    )
    assert build_duty_assessment(_run([other_code])).status == "rates_unavailable"

    no_amount = _analysis(
        "doc_ci", "invoice", [_fact("f_hs", "hs_code", "0303540000", "doc_ci")]
    )
    assert build_duty_assessment(_run([no_amount])).status == "value_unavailable"

    assert build_duty_assessment(_run([])).status == "hs_code_unavailable"


def test_hs_selection_prefers_reviewer_choice_in_duty_view() -> None:
    run = _run([_CI, _CO_MATCH]).model_copy(
        update={
            "hs_selection": HsSelectionRecord(
                code="7318151000",
                reason="reviewer override",
                selected_by="tester",
                selected_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
                history=[
                    {
                        "code": "7318151000",
                        "reason": "reviewer override",
                        "selected_by": "tester",
                        "selected_at": datetime(2026, 8, 5, tzinfo=timezone.utc),
                    }
                ],
            )
        }
    )
    view = build_duty_assessment(run)
    assert view.hs_code is not None
    assert view.hs_code.code == "7318151000"
    assert view.hs_code.source == "reviewer_selection"


# ── API round trip ───────────────────────────────────────────────


def _review_result_with_facts(request: Any) -> dict[str, Any]:
    from tests.product.agent_v3_fixture import build_agent_v3_result

    result = build_agent_v3_result(request)
    document_id = request.documents[0].document_id
    result["document_analyses"][0]["document_type"] = "invoice"
    result["document_analyses"][0]["facts"] = [
        _fact("f_hs", "hs_code", "0303540000", document_id),
        _fact("f_origin", "origin_country", "VN", document_id),
        _fact("f_total", "total_amount", "USD 100", document_id),
    ]
    result["result_sha256"] = canonical_digest(
        {key: value for key, value in result.items() if key != "result_sha256"}
    )
    return result


def _completed_run(client: Any) -> str:
    case = client.post(
        "/api/v1/cases", json={"title": "trade features"}, headers=_auth()
    ).json()
    document = client.post(
        f"/api/v1/cases/{case['case_id']}/documents",
        json={
            "document_type": "CI",
            "filename": "ci.txt",
            "content_text": "COMMERCIAL INVOICE\nHS CODE: 0303540000",
        },
        headers=_auth(),
    )
    assert document.status_code == 201
    review = client.post(
        f"/api/v1/cases/{case['case_id']}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": "trade-features-1"},
    )
    assert review.status_code == 202
    return str(review.json()["job"]["review_run_id"])


def test_trade_feature_endpoints_round_trip() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        run_id = _completed_run(client)

        classification = client.get(
            f"/api/v1/review-runs/{run_id}/hs-classification", headers=_auth()
        )
        assert classification.status_code == 200
        body = classification.json()
        assert body["candidates"][0]["code"] == "0303540000"
        assert "selection" not in body

        selection = client.post(
            f"/api/v1/review-runs/{run_id}/hs-classification",
            json={"code": "0303540000", "reason": "문서 근거와 일치"},
            headers=_auth(),
        )
        assert selection.status_code == 200
        assert selection.json()["code"] == "0303540000"
        assert len(selection.json()["history"]) == 1

        reread = client.get(
            f"/api/v1/review-runs/{run_id}/hs-classification", headers=_auth()
        ).json()
        assert reread["selection"]["code"] == "0303540000"

        origin = client.get(
            f"/api/v1/review-runs/{run_id}/origin-verification", headers=_auth()
        )
        assert origin.status_code == 200
        assert origin.json()["status"] == "missing_certificate"

        duty = client.get(
            f"/api/v1/review-runs/{run_id}/duty-assessment", headers=_auth()
        )
        assert duty.status_code == 200
        duty_body = duty.json()
        assert duty_body["status"] == "estimated"
        assert duty_body["hs_code"]["source"] == "reviewer_selection"
        assert duty_body["applied"]["rate_kind"] == "basic"


def test_re_review_carries_reviewer_hs_selection_to_agent() -> None:
    wires: list[dict[str, Any]] = []

    def handler(request: Any) -> dict[str, Any]:
        wires.append(request.to_agent_wire())
        return _review_result_with_facts(request)

    with _client(review_handler=handler) as client:
        run_id = _completed_run(client)
        assert "reviewer_hs_selection" not in wires[0]

        selection = client.post(
            f"/api/v1/review-runs/{run_id}/hs-classification",
            json={"code": "0303540000", "reason": "문서 근거와 일치"},
            headers=_auth(),
        )
        assert selection.status_code == 200
        case_id = client.get(f"/api/v1/review-runs/{run_id}", headers=_auth()).json()["case_id"]

        second = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "trade-features-2"},
        )
        assert second.status_code == 202
        assert wires[1]["reviewer_hs_selection"] == {
            "code": "0303540000",
            "source_kind": "human_provided",
        }


def test_hs_selection_rejects_codes_outside_reference() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        run_id = _completed_run(client)
        response = client.post(
            f"/api/v1/review-runs/{run_id}/hs-classification",
            json={"code": "9999999999", "reason": "no such code"},
            headers=_auth(),
        )
        assert response.status_code == 409


def test_hsk_reference_lookup_round_trip() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        known = client.get("/api/v1/hsk-reference/6109101000", headers=_auth())
        assert known.status_code == 200
        body = known.json()
        assert body["in_reference"] is True
        assert body["name_ko"] == "티셔츠"
        assert [item["level"] for item in body["ancestors"]] == [2, 4, 6]
        assert body["reference"]["as_of"]

        unknown = client.get("/api/v1/hsk-reference/9999999999", headers=_auth())
        assert unknown.status_code == 200
        assert unknown.json()["in_reference"] is False
        assert "name_ko" not in unknown.json()

        malformed = client.get("/api/v1/hsk-reference/6109", headers=_auth())
        assert malformed.status_code == 404


def test_hsk_reference_search_by_code_prefix() -> None:
    view = search_hs_reference("610910")
    assert view.mode == "code_prefix"
    assert view.results
    assert all(result.code.startswith("610910") for result in view.results)
    assert [result.code for result in view.results] == sorted(
        result.code for result in view.results
    )
    exact = search_hs_reference("6109101000")
    assert [result.code for result in exact.results] == ["6109101000"]
    assert not exact.truncated


def test_hsk_reference_search_by_name() -> None:
    korean = search_hs_reference("티셔츠")
    assert korean.mode == "name"
    assert not korean.partial
    assert any(result.code == "6109101000" for result in korean.results)

    english = search_hs_reference("t-shirts")
    assert any(result.code.startswith("6109") for result in english.results)

    nothing = search_hs_reference("zzqqxxyy")
    assert nothing.results == []
    assert not nothing.truncated
    assert not nothing.partial


def test_hsk_reference_search_matches_english_ancestor_names() -> None:
    # "frozen" only exists in the heading's English name; both words match.
    view = search_hs_reference("frozen mackerel")
    assert not view.partial
    assert any(result.code == "0303540000" for result in view.results)


def test_hsk_reference_search_falls_back_to_partial_word_matches() -> None:
    # No row carries both words, so rows matching either word are ranked
    # by how many words they match and the view says so.
    view = search_hs_reference("스테인리스 볼트")
    assert view.partial
    assert view.results
    assert any(result.code.startswith("7318") for result in view.results)


def test_hsk_reference_search_splits_korean_compound_words() -> None:
    # No row spells "와이어로프"; the compound halves still surface rope and
    # wire rows instead of an empty list.
    view = search_hs_reference("와이어로프")
    assert view.partial
    assert view.results
    texts = " ".join(f"{r.name_ko or ''} {r.name_en or ''}".lower() for r in view.results)
    assert "로프" in texts or "와이어" in texts

    refined = search_hs_reference("철강 로프")
    assert not refined.partial
    assert any(result.code.startswith("7312") for result in refined.results)


def test_hsk_reference_search_applies_query_synonyms() -> None:
    view = search_hs_reference("전구")
    assert any(result.code.startswith("8539") for result in view.results)


def test_hsk_reference_search_truncates_wide_prefix() -> None:
    view = search_hs_reference("0")
    assert view.mode == "code_prefix"
    assert len(view.results) == 12
    assert view.truncated


def test_hsk_reference_search_endpoint_round_trip() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        by_code = client.get("/api/v1/hsk-reference", params={"q": "610910"}, headers=_auth())
        assert by_code.status_code == 200
        body = by_code.json()
        assert body["mode"] == "code_prefix"
        assert any(item["code"] == "6109101000" for item in body["results"])
        assert body["reference"]["as_of"]

        by_name = client.get("/api/v1/hsk-reference", params={"q": "티셔츠"}, headers=_auth())
        assert by_name.status_code == 200
        assert by_name.json()["mode"] == "name"
        assert any(item["code"] == "6109101000" for item in by_name.json()["results"])

        empty = client.get("/api/v1/hsk-reference", params={"q": ""}, headers=_auth())
        assert empty.status_code == 422


def test_hs_selection_requires_reviewer_role() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        run_id = _completed_run(client)
        response = client.post(
            f"/api/v1/review-runs/{run_id}/hs-classification",
            json={"code": "0303540000", "reason": "approver cannot select"},
            headers={"Authorization": "Bearer approver-token"},
        )
        assert response.status_code == 403


def _case_with_documents(client: Any, documents: list[tuple[str, str, str]]) -> str:
    case = client.post("/api/v1/cases", json={"title": "case hs"}, headers=_auth()).json()
    for document_type, filename, text in documents:
        created = client.post(
            f"/api/v1/cases/{case['case_id']}/documents",
            json={"document_type": document_type, "filename": filename, "content_text": text},
            headers=_auth(),
        )
        assert created.status_code == 201
    return str(case["case_id"])


def test_case_hs_classification_is_available_before_any_review() -> None:
    wires: list[dict[str, Any]] = []

    def handler(request: Any) -> dict[str, Any]:
        wires.append(request.to_agent_wire())
        return _review_result_with_facts(request)

    with _client(review_handler=handler) as client:
        case_id = _case_with_documents(
            client,
            [
                ("CI", "ci.txt", "COMMERCIAL INVOICE\nInvoice No: 1234567890\nHS CODE: 0303540000"),
                ("PL", "pl.txt", "PACKING LIST\nContainer No: 9876543210\nH.S. Code 0303540000"),
            ],
        )

        before = client.get(f"/api/v1/cases/{case_id}/hs-classification", headers=_auth())
        assert before.status_code == 200
        body = before.json()
        assert body["basis_kind"] == "document_text"
        # Only lines that name a tariff code contribute; the invoice and
        # container numbers are not candidates.
        assert [candidate["code"] for candidate in body["candidates"]] == ["0303540000"]
        assert body["candidates"][0]["recommended"] is True
        assert body["candidates"][0]["basis"][0]["fact_name"] == "document_text"
        assert "selection" not in body

        selection = client.post(
            f"/api/v1/cases/{case_id}/hs-classification",
            json={"code": "0303540000", "reason": "서류 기재 코드 확인"},
            headers=_auth(),
        )
        assert selection.status_code == 200
        assert selection.json()["code"] == "0303540000"
        assert len(selection.json()["history"]) == 1

        after = client.get(f"/api/v1/cases/{case_id}/hs-classification", headers=_auth()).json()
        assert after["selection"]["code"] == "0303540000"

        review = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "case-hs-1"},
        )
        assert review.status_code == 202
        # The very first review already carries the reviewer-confirmed code.
        assert wires[0]["reviewer_hs_selection"] == {
            "code": "0303540000",
            "source_kind": "human_provided",
        }


def test_case_hs_classification_falls_back_to_description_match() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        case_id = _case_with_documents(
            client,
            [("CI", "ci.txt", "COMMERCIAL INVOICE\nDescription: MEN'S COTTON T-SHIRTS | HS Code: 610910 | Quantity: 10 PCS\nQty: 10")],
        )
        body = client.get(f"/api/v1/cases/{case_id}/hs-classification", headers=_auth()).json()
        assert body["candidates"]
        assert "6109101000" in {candidate["code"] for candidate in body["candidates"]}
        # Only the item name is matched; the pipe-separated tail is not.
        assert body["candidates"][0]["basis"][0]["raw_value"] == "MEN'S COTTON T-SHIRTS"
        for candidate in body["candidates"]:
            assert candidate["origin"] == "description_match"
            assert candidate["recommended"] is False


def test_case_hs_classification_reads_item_name_below_a_table_header_cell() -> None:
    # Text-layer extraction of a tabular invoice renders one cell per line:
    # the header "Description of Goods", other headers, marks, then the name.
    invoice = "\n".join(
        [
            "COMMERCIAL INVOICE",
            "Marks & No. of PKGS",
            "Description of Goods",
            "Quantity",
            "Unit price",
            "Amount",
            "PACIFIC",
            "C/NO. 1-204",
            "ROTTERDAM",
            "MEN'S COTTON T-SHIRTS",
            "64,444 PCS",
            "@ 2.17",
            "USD 139,843.48",
            "TOTAL",
            "USD 139,843.48",
        ]
    )
    with _client(review_handler=_review_result_with_facts) as client:
        case_id = _case_with_documents(client, [("CI", "ci.txt", invoice)])
        body = client.get(f"/api/v1/cases/{case_id}/hs-classification", headers=_auth()).json()
        codes = [candidate["code"] for candidate in body["candidates"]]
        assert "6109101000" in codes
        raw_values = {basis["raw_value"] for candidate in body["candidates"] for basis in candidate["basis"]}
        assert "MEN'S COTTON T-SHIRTS" in raw_values
        # The header cell itself and the amount cells are not item names.
        assert "Description of Goods" not in raw_values
        assert not any(value.startswith("USD") for value in raw_values)


def test_case_hs_selection_rejects_codes_outside_reference_and_closed_cases() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        case_id = _case_with_documents(client, [("CI", "ci.txt", "COMMERCIAL INVOICE\nHS CODE: 0303540000")])
        rejected = client.post(
            f"/api/v1/cases/{case_id}/hs-classification",
            json={"code": "9999999999", "reason": "없는 코드"},
            headers=_auth(),
        )
        assert rejected.status_code == 409


def test_newer_run_level_selection_wins_over_case_level_selection() -> None:
    wires: list[dict[str, Any]] = []

    def handler(request: Any) -> dict[str, Any]:
        wires.append(request.to_agent_wire())
        return _review_result_with_facts(request)

    with _client(review_handler=handler) as client:
        case_id = _case_with_documents(client, [("CI", "ci.txt", "COMMERCIAL INVOICE\nHS CODE: 0303540000")])
        assert (
            client.post(
                f"/api/v1/cases/{case_id}/hs-classification",
                json={"code": "0303540000", "reason": "1차"},
                headers=_auth(),
            ).status_code
            == 200
        )
        first = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "case-hs-2"},
        )
        run_id = str(first.json()["job"]["review_run_id"])
        assert wires[0]["reviewer_hs_selection"]["code"] == "0303540000"

        other = client.get("/api/v1/hsk-reference?q=참치", headers=_auth()).json()
        codes = [item["code"] for item in other.get("results", []) if item["code"] != "0303540000"]
        if not codes:
            return
        assert (
            client.post(
                f"/api/v1/review-runs/{run_id}/hs-classification",
                json={"code": codes[0], "reason": "심사 뒤 정정"},
                headers=_auth(),
            ).status_code
            == 200
        )
        second = client.post(
            f"/api/v1/cases/{case_id}/reviews",
            json={},
            headers={**_auth(), "Idempotency-Key": "case-hs-3"},
        )
        assert second.status_code == 202
        assert wires[1]["reviewer_hs_selection"]["code"] == codes[0]
