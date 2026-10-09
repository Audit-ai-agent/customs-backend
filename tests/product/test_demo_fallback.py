"""Demo fallback: pre-registered review results replayed when the Agent (LLM) is unavailable."""

from __future__ import annotations

from typing import Any

from tests.product.test_trade_features import (
    _case_with_documents,
    _review_result_with_facts,
    _tshirt_agent,
)
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from tests.product.test_workbench_api import _approver_auth, _auth, _client, _tiny_png

_INVOICE = "\n".join(["COMMERCIAL INVOICE", "Description of Goods", "MEN'S COTTON T-SHIRTS", "64,444 PCS"])
_PACKING = "PACKING LIST\nMEN'S COTTON T-SHIRTS\n64,444 PCS"


def _review(client: Any, case_id: str, key: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/cases/{case_id}/reviews",
        json={},
        headers={**_auth(), "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


def _run(client: Any, review_run_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/review-runs/{review_run_id}", headers=_auth())
    assert response.status_code == 200, response.text
    return dict(response.json())


def _register(client: Any, name: str, review_run_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/admin/demo-fallback/sets",
        json={"name": name, "review_run_id": review_run_id},
        headers=_approver_auth(),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _set_mode(client: Any, mode: str, **extra: Any) -> dict[str, Any]:
    response = client.put(
        "/api/v1/admin/demo-fallback",
        json={"mode": mode, **extra},
        headers=_approver_auth(),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_demo_fallback_is_approver_only_and_starts_off() -> None:
    with _client(review_handler=_review_result_with_facts) as client:
        assert client.get("/api/v1/admin/demo-fallback", headers=_auth()).status_code == 403
        body = client.get("/api/v1/admin/demo-fallback", headers=_approver_auth()).json()
        assert body["settings"]["mode"] == "off" and body["sets"] == []
        assert client.put("/api/v1/admin/demo-fallback", json={}, headers=_approver_auth()).status_code == 422
        assert client.put("/api/v1/admin/demo-fallback", json={"mode": "maybe"}, headers=_approver_auth()).status_code == 422


def test_registered_result_is_replayed_for_the_same_documents_in_always_mode() -> None:
    calls: list[str] = []

    def review(request: Any) -> dict[str, Any]:
        calls.append(request.review_run_id)
        return _review_result_with_facts(request)

    with _client(review_handler=review, hs_candidates_handler=_tshirt_agent([])) as client:
        # 1. A normal review, with the Agent, becomes the registered set.
        source_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE), ("PL", "pl.txt", _PACKING)])
        client.get(f"/api/v1/cases/{source_case}/hs-classification?agent=true", headers=_auth())
        source = _review(client, source_case, "fallback-source")
        source_run_id = str(source["job"]["review_run_id"])
        view = _register(client, "시연 1", source_run_id)
        assert len(view["sets"]) == 1
        registered = view["sets"][0]
        assert registered["source_review_run_id"] == source_run_id
        assert sorted(item["document_type"] for item in registered["documents"]) == ["CI", "PL"]
        assert registered["has_hs_ranking"] is True and registered["used_count"] == 0
        # The same documents cannot be registered twice.
        assert (
            client.post(
                "/api/v1/admin/demo-fallback/sets",
                json={"name": "dup", "review_run_id": source_run_id},
                headers=_approver_auth(),
            ).status_code
            == 409
        )

        # 2. Always: a new case with the same documents never reaches the Agent.
        _set_mode(client, "always", delay_seconds=0)
        replay_case = _case_with_documents(client, [("PL", "pl.txt", _PACKING), ("CI", "ci.txt", _INVOICE)])
        before = len(calls)
        replayed = _review(client, replay_case, "fallback-replay")
        assert replayed["job"]["status"] == "succeeded"
        assert len(calls) == before
        run = _run(client, str(replayed["job"]["review_run_id"]))
        source_run = _run(client, source_run_id)
        # The result is re-bound to the new run: ids, documents, hashes.
        assert run["case_id"] == replay_case and run["review_run_id"] != source_run_id
        new_documents = {item["document_id"] for item in run["input_document_manifest"]}
        assert new_documents.isdisjoint({item["document_id"] for item in source_run["input_document_manifest"]})
        assert {analysis["document_id"] for analysis in run["document_analyses"]} <= new_documents
        assert len(run["findings"]) == len(source_run["findings"])
        assert run["report"]["summary"] == source_run["report"]["summary"]
        assert run["provider_result_sha256"] != source_run["provider_result_sha256"]
        # The AI HSK ranking is replayed too, without an Agent call.
        hs = client.get(f"/api/v1/cases/{replay_case}/hs-classification?agent=true", headers=_auth()).json()
        assert hs["basis_kind"] == "agent_ranked"
        assert hs["candidates"][0]["code"] == "6109101000"
        assert all(basis["document_id"] in new_documents for basis in hs["candidates"][0]["basis"])
        usage = client.get("/api/v1/admin/demo-fallback", headers=_approver_auth()).json()["sets"][0]
        assert usage["used_count"] == 2 and usage["last_used_at"]

        # 3. Different documents still go to the Agent.
        other_case = _case_with_documents(client, [("CI", "ci.txt", "COMMERCIAL INVOICE\nHS CODE: 0303540000")])
        _review(client, other_case, "fallback-other")
        assert len(calls) == before + 1

        # 4. Delete: the set is gone and the next same-document review calls the Agent.
        assert (
            client.delete(
                f"/api/v1/admin/demo-fallback/sets/{registered['fallback_id']}", headers=_approver_auth()
            ).status_code
            == 200
        )
        assert client.get("/api/v1/admin/demo-fallback", headers=_approver_auth()).json()["sets"] == []
        again_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE), ("PL", "pl.txt", _PACKING)])
        _review(client, again_case, "fallback-again")
        assert len(calls) == before + 2


def test_auto_mode_replays_only_when_the_agent_fails() -> None:
    agent_down = {"value": False}
    calls: list[str] = []

    def review(request: Any) -> dict[str, Any]:
        calls.append(request.review_run_id)
        if agent_down["value"]:
            raise RuntimeError("provider unavailable")
        return _review_result_with_facts(request)

    def hs_candidates(request: dict[str, Any]) -> dict[str, Any]:
        if agent_down["value"]:
            raise RuntimeError("provider unavailable")
        return _tshirt_agent([])(request)

    with _client(review_handler=review, hs_candidates_handler=hs_candidates) as client:
        source_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE)])
        client.get(f"/api/v1/cases/{source_case}/hs-classification?agent=true", headers=_auth())
        source_run_id = str(_review(client, source_case, "auto-source")["job"]["review_run_id"])
        _register(client, "시연 2", source_run_id)
        _set_mode(client, "auto", delay_seconds=0)

        # Agent healthy: the Agent answers, nothing is replayed.
        healthy_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE)])
        healthy = _review(client, healthy_case, "auto-healthy")
        assert healthy["job"]["status"] == "succeeded" and calls[-1] == healthy["job"]["review_run_id"]
        assert client.get("/api/v1/admin/demo-fallback", headers=_approver_auth()).json()["sets"][0]["used_count"] == 0

        # Agent down: the registered result stands in and the job still succeeds.
        agent_down["value"] = True
        down_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE)])
        down = _review(client, down_case, "auto-down")
        assert down["job"]["status"] == "succeeded"
        run = _run(client, str(down["job"]["review_run_id"]))
        assert run["case_id"] == down_case and run["findings"] is not None
        hs = client.get(f"/api/v1/cases/{down_case}/hs-classification?agent=true", headers=_auth()).json()
        assert hs["basis_kind"] == "agent_ranked"

        # Off: an Agent failure fails the job as before.
        _set_mode(client, "off")
        off_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE)])
        off = _review(client, off_case, "auto-off")
        assert off["job"]["status"] == "failed"


def _uploaded_and_extracted(
    client: Any, case_id: str, content: bytes | None = None, filename: str = "invoice.png"
) -> dict[str, Any]:
    asset = client.post(
        f"/api/v1/cases/{case_id}/document-assets",
        params={"document_type": "CI", "filename": filename},
        content=content or _tiny_png(),
        headers={**_auth(), "Content-Type": "image/png"},
    ).json()
    preview = client.post(
        f"/api/v1/cases/{case_id}/document-assets/{asset['asset_id']}/extractions", headers=_auth()
    )
    assert preview.status_code == 202, preview.text
    return {"asset_id": asset["asset_id"], **preview.json()["job"]}


def _confirm_default_text(client: Any, case_id: str, job: dict[str, Any]) -> None:
    # What the workbench pre-fills: every span in page/reading order, one per line.
    text = "\n".join(span["text"] for page in job["result"]["pages"] for span in page["spans"])
    confirmed = client.post(
        f"/api/v1/cases/{case_id}/document-assets/{job['asset_id']}/confirmed-document",
        json={"extraction_job_id": job["extraction_job_id"], "filename": "invoice.txt", "content_text": text},
        headers=_auth(),
    )
    assert confirmed.status_code == 201, confirmed.text


def test_uploaded_file_extraction_is_replayed_so_the_whole_demo_runs_without_the_agent() -> None:
    agent_down = {"value": False}

    def extract(request: Any, content: bytes) -> dict[str, Any]:
        if agent_down["value"]:
            raise RuntimeError("provider unavailable")
        lines = _INVOICE.split("\n")
        return {
            "schema_version": "1.0",
            "document_asset_id": request.document_asset_id,
            "extraction_request_id": request.extraction_request_id,
            "status": "preview_ready",
            "media_type": request.media_type,
            "byte_size": request.byte_size,
            "source_sha256": request.source_sha256,
            "canonical_text_sha256": hashlib.sha256(_INVOICE.encode()).hexdigest(),
            "extractor_revision": "local-preview-test-r1",
            "pages": [
                {
                    "page_number": 1,
                    "width": 1,
                    "height": 1,
                    "coordinate_unit": "pixels",
                    "spans": [
                        {
                            "span_id": f"s{index}",
                            "ordinal": index,
                            "text": line,
                            "bbox": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                            "reading_order": index,
                        }
                        for index, line in enumerate(lines, start=1)
                    ],
                }
            ],
            "warnings": [{"code": "human_confirmation_required"}],
        }

    def review(request: Any) -> dict[str, Any]:
        if agent_down["value"]:
            raise RuntimeError("provider unavailable")
        return _review_result_with_facts(request)

    with _client(review_handler=review, extract_handler=extract) as client:
        source_case = client.post("/api/v1/cases", json={"title": "prep"}, headers=_auth()).json()["case_id"]
        _confirm_default_text(client, source_case, _uploaded_and_extracted(client, source_case))
        source_run_id = str(_review(client, source_case, "upload-source")["job"]["review_run_id"])
        _register(client, "시연 3", source_run_id)
        # The source case is only a vehicle: the set survives its deletion.
        assert client.delete(f"/api/v1/cases/{source_case}", headers=_auth()).status_code == 204
        _set_mode(client, "auto", delay_seconds=0)

        agent_down["value"] = True
        demo_case = client.post("/api/v1/cases", json={"title": "demo"}, headers=_auth()).json()["case_id"]
        job = _uploaded_and_extracted(client, demo_case)
        assert job["status"] == "preview_ready"
        _confirm_default_text(client, demo_case, job)
        assert _review(client, demo_case, "upload-demo")["job"]["status"] == "succeeded"
        usage = client.get("/api/v1/admin/demo-fallback", headers=_approver_auth()).json()["sets"][0]
        assert usage["used_count"] == 2

        # Second chance: a different file under the same name (case-insensitive)
        # still gets the stored text, so the review replays as well.
        other_bytes = BytesIO()
        Image.new("RGB", (2, 2), color=(255, 255, 255)).save(other_bytes, format="PNG")
        renamed_case = client.post("/api/v1/cases", json={"title": "demo 2"}, headers=_auth()).json()["case_id"]
        job = _uploaded_and_extracted(client, renamed_case, other_bytes.getvalue(), "INVOICE.PNG")
        assert job["status"] == "preview_ready"
        _confirm_default_text(client, renamed_case, job)
        assert _review(client, renamed_case, "upload-renamed")["job"]["status"] == "succeeded"
        # An unknown name with unknown bytes is not replayed.
        stranger = client.post("/api/v1/cases", json={"title": "demo 3"}, headers=_auth()).json()["case_id"]
        with pytest.raises(RuntimeError):
            _uploaded_and_extracted(client, stranger, other_bytes.getvalue(), "other.png")


def test_same_confirmed_file_names_replay_when_the_text_differs() -> None:
    calls: list[str] = []

    def review(request: Any) -> dict[str, Any]:
        calls.append(request.review_run_id)
        return _review_result_with_facts(request)

    with _client(review_handler=review, hs_candidates_handler=_tshirt_agent([])) as client:
        source_case = _case_with_documents(client, [("CI", "ci.txt", _INVOICE), ("PL", "pl.txt", _PACKING)])
        client.get(f"/api/v1/cases/{source_case}/hs-classification?agent=true", headers=_auth())
        source_run_id = str(_review(client, source_case, "name-source")["job"]["review_run_id"])
        _register(client, "시연 4", source_run_id)
        _set_mode(client, "always", delay_seconds=0)

        # Edited text, same names: replayed and re-bound to the new documents.
        edited = _case_with_documents(
            client, [("CI", "CI.txt", _INVOICE + "\nedited"), ("PL", "pl.txt", _PACKING + " ")]
        )
        before = len(calls)
        replayed = _review(client, edited, "name-replay")
        assert replayed["job"]["status"] == "succeeded" and len(calls) == before
        run = _run(client, str(replayed["job"]["review_run_id"]))
        new_documents = {item["document_id"] for item in run["input_document_manifest"]}
        assert {analysis["document_id"] for analysis in run["document_analyses"]} <= new_documents
        hs = client.get(f"/api/v1/cases/{edited}/hs-classification?agent=true", headers=_auth()).json()
        assert hs["basis_kind"] == "agent_ranked"
        assert all(basis["document_id"] in new_documents for basis in hs["candidates"][0]["basis"])

        # A different set of names still goes to the Agent.
        partial = _case_with_documents(client, [("CI", "ci.txt", _INVOICE + "\nedited")])
        _review(client, partial, "name-partial")
        assert len(calls) == before + 1


def test_a_correction_re_review_never_uses_the_fallback() -> None:
    from customs_backend.domain import AgentReviewRequest, JobRecord, Principal, Role
    from customs_backend.service import WorkbenchService

    principal = Principal(subject="s", org_id="o", roles=frozenset({Role.reviewer}))
    job = JobRecord.model_construct(
        request={"parent_review_run_id": "run-parent", "documents": []},
        case_id="c",
        review_run_id="r",
        org_id="o",
    )
    calls: list[str] = []

    class _Repository:
        def demo_fallback_settings(self):  # noqa: ANN202
            from customs_backend.domain import DemoFallbackSettingsRecord

            return DemoFallbackSettingsRecord(mode="always")

        def list_demo_fallback_sets(self, org_id: str):  # noqa: ANN202
            raise AssertionError("a child run must not look for a fallback set")

    class _Agent:
        def review(self, request: Any) -> dict[str, Any]:
            calls.append("agent")
            return {"ok": True}

    service = WorkbenchService.__new__(WorkbenchService)
    service.repository = _Repository()  # type: ignore[assignment]
    service.agent = _Agent()  # type: ignore[assignment]
    result = service._review_with_demo_fallback(
        principal=principal, job=job, agent_request=AgentReviewRequest.model_construct()
    )
    assert result == {"ok": True} and calls == ["agent"]
