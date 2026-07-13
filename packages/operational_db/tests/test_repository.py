import pytest
from decimal import Decimal
from pydantic import ValidationError

from operational_db import InMemoryOperationalRepository, OperationalIntegrityError


def _snapshot(run_id: str = "run_1", parent: str | None = None) -> dict:
    evidence = {
        "evidence_id": "ev_1",
        "item_id": "item_1",
        "source_record_id": "requirement:req_1",
        "version_hash": "abc123",
        "source_uri": "https://example.invalid/source/1",
        "source_type": "customs_requirement",
        "text": "required document",
    }
    return {
        "review_run_id": run_id,
        "case_id": "case_1",
        "parent_run_id": parent,
        "final_status": "needs_review",
        "requires_human_review": True,
        "knowledge_snapshot_id": "kb_1",
        "items": [{"item_id": "item_1", "description": "fish", "hs_code": "0303540000"}],
        "evidence": [evidence],
        "regulatory_findings": [
            {
                "finding_code": "REQUIRED_CERT_MISSING",
                "severity": "high",
                "item_id": "item_1",
                "evidence_ids": ["ev_1"],
            }
        ],
        "report": {"overall_status": "needs_review"},
    }


def test_save_is_idempotent_and_immutable():
    repo = InMemoryOperationalRepository()
    repo.save_run(_snapshot())
    assert repo.save_run(_snapshot()).review_run_id == "run_1"
    changed = _snapshot()
    changed["knowledge_snapshot_id"] = "kb_2"
    with pytest.raises(OperationalIntegrityError):
        repo.save_run(changed)


def test_rerun_requires_parent_and_is_new_immutable_run():
    repo = InMemoryOperationalRepository()
    with pytest.raises(OperationalIntegrityError):
        repo.save_run(_snapshot("run_2", parent="run_1"))
    repo.save_run(_snapshot("run_1"))
    assert repo.save_run(_snapshot("run_2", parent="run_1")).parent_run_id == "run_1"


def test_finding_must_reference_same_item_evidence():
    bad = _snapshot()
    bad["regulatory_findings"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(ValidationError):
        InMemoryOperationalRepository().save_run(bad)


def test_status_must_match_report_and_hitl():
    bad = _snapshot()
    bad["report"]["overall_status"] = "completed"
    with pytest.raises(ValidationError):
        InMemoryOperationalRepository().save_run(bad)


def test_postgres_json_encoder_handles_agent_decimal_values():
    from operational_db.repository import PostgresOperationalRepository

    encoded = PostgresOperationalRepository._json({"quantity": Decimal("1200.50")})
    assert encoded == '{"quantity": "1200.50"}'
