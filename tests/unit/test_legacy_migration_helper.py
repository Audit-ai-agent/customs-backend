from __future__ import annotations

from customs_backend.domain import ChatRecord
from tools.verify_legacy_005_upgrade import SHA, _legacy_chat_record


def test_legacy_chat_seed_is_canonical_after_migration_006_idempotency_backfill() -> None:
    seed = _legacy_chat_record()

    assert "idempotency_key" not in seed
    assert "idempotency_request_digest" not in seed

    migrated = ChatRecord.model_validate(
        {
            **seed,
            "idempotency_key": "legacy:chat:legacy-org:legacy-message-005",
            "idempotency_request_digest": SHA,
        }
    )

    assert migrated.generation_mode == "deterministic_fallback"
    assert migrated.model_execution_summary.model_dump(mode="json") == {
        "provider": None,
        "model_id": None,
        "text_only": False,
        "call_count": 0,
        "input_tokens_total": 0,
        "output_tokens_total": 0,
        "prompt_hashes_complete": False,
        "schema_hashes_complete": False,
        "runtime_identity_match": False,
        "audit_complete": False,
        "unmet_reason_codes": ["no_model_calls"],
    }
