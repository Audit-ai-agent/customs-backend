"""Verify real revision-005 -> 006 -> 007 forward-only backfills.

This tool is deliberately split into ``seed`` and ``verify`` phases so CI and
local release evidence execute the actual Alembic 005 -> 006 transition between
them. It never creates or drops databases and is safe only for an ephemeral test
database selected by the caller.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from customs_backend.domain import (
    ChatInteractionRecord,
    ChatRecord,
    CorrectionProposalRecord,
    HumanDecisionRecord,
)


CASE_ID = "legacy-case-005"
RUN_ID = "legacy-run-005"
PROPOSAL_ID = "legacy-proposal-005"
DECISION_ID = "legacy-decision-005"
MESSAGE_ID = "legacy-message-005"
INVALID_INTERACTION_ID = "legacy-invalid-actor-006"
ORG_ID = "legacy-org"
SHA = "a" * 64
CREATED_AT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _psycopg_dsn(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def seed(dsn: str) -> None:
    proposal = {
        "proposal_id": PROPOSAL_ID,
        "org_id": ORG_ID,
        "case_id": CASE_ID,
        "review_run_id": RUN_ID,
        "parent_result_digest": SHA,
        "parent_input_manifest_sha256": SHA,
        "case_revision": 0,
        "corrections": [
            {
                "document_id": "legacy-document-005",
                "field_path": "fields.invoice_number",
                "new_value": "INV-LEGACY",
                "reason": "legacy source recheck",
                "confidence": 1.0,
            }
        ],
        "actions": [],
        "status": "pending",
        "created_by": "legacy-reviewer",
        "created_at": CREATED_AT.isoformat(),
        "decided_by": None,
        "decided_at": None,
        "decision_reason": None,
        "child_job_id": None,
    }
    decision = {
        "decision_id": DECISION_ID,
        "review_run_id": RUN_ID,
        "case_id": CASE_ID,
        "org_id": ORG_ID,
        "decision": "approve",
        "reason": "legacy independent approval",
        "target_revision_id": None,
        "proposal_id": PROPOSAL_ID,
        "decided_by": "legacy-approver",
        "created_at": CREATED_AT.isoformat(),
    }
    message = {
        "message_id": MESSAGE_ID,
        "review_run_id": RUN_ID,
        "case_id": CASE_ID,
        "org_id": ORG_ID,
        "question": "legacy grounded question",
        "answer": "legacy grounded answer",
        "answer_status": "answered",
        "citations": [],
        "bound_result_digest": SHA,
        "created_by": "legacy-reviewer",
        "created_at": CREATED_AT.isoformat(),
    }
    with psycopg.connect(dsn, autocommit=True) as connection:
        revision = connection.execute(
            "SELECT version_num FROM operations.alembic_version"
        ).fetchone()
        if revision != ("20260722_0005",):
            raise RuntimeError(f"legacy fixture requires revision 005, got {revision!r}")
        connection.execute(
            """
            INSERT INTO operations.product_cases(case_id, org_id, revision, record_json)
            VALUES (%s, %s, 0, %s)
            """,
            (CASE_ID, ORG_ID, Jsonb({"case_id": CASE_ID, "org_id": ORG_ID})),
        )
        connection.execute(
            """
            INSERT INTO operations.product_review_runs(
              review_run_id, case_id, org_id, case_revision, result_digest,
              execution_status, assessment, review_disposition, assurance_level,
              provider_result_json, record_json
            ) VALUES (%s, %s, %s, 0, %s, 'succeeded', 'no_issue_detected',
                      'human_review_required', 'diagnostic', '{}'::jsonb, '{}'::jsonb)
            """,
            (RUN_ID, CASE_ID, ORG_ID, SHA),
        )
        connection.execute(
            """
            INSERT INTO operations.correction_proposals(
              proposal_id, review_run_id, case_id, org_id, status, record_json, created_at
            ) VALUES (%s, %s, %s, %s, 'pending', %s, %s)
            """,
            (PROPOSAL_ID, RUN_ID, CASE_ID, ORG_ID, Jsonb(proposal), CREATED_AT),
        )
        connection.execute(
            """
            INSERT INTO operations.human_decisions(
              decision_id, review_run_id, org_id, decision, decision_scope,
              record_json, created_at
            ) VALUES (%s, %s, %s, 'approve', %s, %s, %s)
            """,
            (
                DECISION_ID,
                RUN_ID,
                ORG_ID,
                f"proposal:{PROPOSAL_ID}",
                Jsonb(decision),
                CREATED_AT,
            ),
        )
        connection.execute(
            """
            INSERT INTO operations.case_chat_messages(
              message_id, review_run_id, org_id, bound_result_digest, record_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (MESSAGE_ID, RUN_ID, ORG_ID, SHA, Jsonb(message), CREATED_AT),
        )


def verify(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        revision = connection.execute(
            "SELECT version_num FROM operations.alembic_version"
        ).fetchone()
        if revision != ("20260722_0006",):
            raise RuntimeError(
                f"legacy fixture verification requires revision 006, got {revision!r}"
            )
        proposal_json = connection.execute(
            "SELECT record_json FROM operations.correction_proposals WHERE proposal_id = %s",
            (PROPOSAL_ID,),
        ).fetchone()
        decision_json = connection.execute(
            "SELECT record_json FROM operations.human_decisions WHERE decision_id = %s",
            (DECISION_ID,),
        ).fetchone()
        message_json = connection.execute(
            "SELECT record_json FROM operations.case_chat_messages WHERE message_id = %s",
            (MESSAGE_ID,),
        ).fetchone()
        interaction_json = connection.execute(
            """
            SELECT record_json FROM operations.case_chat_interactions
            WHERE message_id = %s
            """,
            (MESSAGE_ID,),
        ).fetchone()
        if not all((proposal_json, decision_json, message_json, interaction_json)):
            raise RuntimeError("revision 006 did not preserve every legacy fixture row")
        proposal = CorrectionProposalRecord.model_validate(proposal_json[0])
        decision = HumanDecisionRecord.model_validate(decision_json[0])
        message = ChatRecord.model_validate(message_json[0])
        interaction = ChatInteractionRecord.model_validate(interaction_json[0])
        if not proposal.idempotency_key.startswith("legacy:proposal:"):
            raise RuntimeError("legacy proposal idempotency key was not backfilled")
        if not decision.idempotency_key or not decision.idempotency_key.startswith(
            "legacy:proposal-decision:"
        ):
            raise RuntimeError("legacy proposal decision idempotency key was not backfilled")
        if not message.idempotency_key.startswith("legacy:chat:"):
            raise RuntimeError("legacy chat idempotency key was not backfilled")
        if interaction.state.value != "completed" or interaction.message_id != message.message_id:
            raise RuntimeError("legacy chat was not bound to a completed call-once interaction")
        try:
            connection.execute(
                "UPDATE operations.human_decisions SET record_json = record_json WHERE decision_id = %s",
                (DECISION_ID,),
            )
        except psycopg.Error as exc:
            if exc.sqlstate != "55000":
                raise
        else:
            raise RuntimeError("human decision append-only trigger was not restored")


def seed_invalid_006(dsn: str) -> None:
    """Insert a corrupt 006 row and prove 007 refuses to invent actor provenance."""

    record = {
        "interaction_id": INVALID_INTERACTION_ID,
        "review_run_id": RUN_ID,
        "case_id": CASE_ID,
        "org_id": ORG_ID,
        "idempotency_key": "legacy:invalid-actor:006",
        "idempotency_request_digest": SHA,
        "agent_request_sha256": SHA,
        "state": "reserved",
        "message_id": None,
        "error_code": None,
        "call_started_at": None,
        # created_by is intentionally absent: 007 must fail closed.
        "created_at": CREATED_AT.isoformat(),
        "updated_at": CREATED_AT.isoformat(),
    }
    with psycopg.connect(dsn, autocommit=True) as connection:
        revision = connection.execute(
            "SELECT version_num FROM operations.alembic_version"
        ).fetchone()
        if revision != ("20260722_0006",):
            raise RuntimeError(f"invalid fixture requires revision 006, got {revision!r}")
        connection.execute(
            """
            INSERT INTO operations.case_chat_interactions(
              interaction_id, review_run_id, case_id, org_id, idempotency_key,
              idempotency_request_digest, agent_request_sha256, state,
              message_id, record_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'reserved', NULL, %s, %s)
            """,
            (
                INVALID_INTERACTION_ID,
                RUN_ID,
                CASE_ID,
                ORG_ID,
                "legacy:invalid-actor:006",
                SHA,
                SHA,
                Jsonb(record),
                CREATED_AT,
            ),
        )


def remove_invalid_006(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        revision = connection.execute(
            "SELECT version_num FROM operations.alembic_version"
        ).fetchone()
        if revision != ("20260722_0006",):
            raise RuntimeError(f"fixture cleanup requires revision 006, got {revision!r}")
        deleted = connection.execute(
            "DELETE FROM operations.case_chat_interactions WHERE interaction_id = %s",
            (INVALID_INTERACTION_ID,),
        ).rowcount
        if deleted != 1:
            raise RuntimeError("invalid revision-006 fixture was not present")


def verify_007(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        revision = connection.execute(
            "SELECT version_num FROM operations.alembic_version"
        ).fetchone()
        if revision != ("20260722_0007",):
            raise RuntimeError(
                f"legacy fixture verification requires revision 007, got {revision!r}"
            )
        row = connection.execute(
            """
            SELECT created_by, created_at
            FROM operations.case_chat_interactions
            WHERE message_id = %s
            """,
            (MESSAGE_ID,),
        ).fetchone()
        if row != ("legacy-reviewer", CREATED_AT):
            raise RuntimeError(f"revision 007 actor provenance backfill drifted: {row!r}")
        index_exists = connection.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM pg_indexes
              WHERE schemaname = 'operations'
                AND indexname = 'idx_case_chat_interactions_actor_window'
            )
            """
        ).fetchone()
        if index_exists != (True,):
            raise RuntimeError("revision 007 actor-window index is missing")
        if connection.execute(
            "SELECT 1 FROM operations.case_chat_interactions WHERE created_by IS NULL"
        ).fetchone():
            raise RuntimeError("revision 007 retained an interaction without actor provenance")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "seed",
            "verify",
            "seed-invalid-006",
            "remove-invalid-006",
            "verify-007",
        ),
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("CUSTOMS_BACKEND_DATABASE_URL"),
        help="Ephemeral PostgreSQL DSN; defaults to CUSTOMS_BACKEND_DATABASE_URL.",
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or CUSTOMS_BACKEND_DATABASE_URL is required")
    dsn = _psycopg_dsn(args.dsn)
    if args.phase == "seed":
        seed(dsn)
    elif args.phase == "verify":
        verify(dsn)
    elif args.phase == "seed-invalid-006":
        seed_invalid_006(dsn)
    elif args.phase == "remove-invalid-006":
        remove_invalid_006(dsn)
    else:
        verify_007(dsn)
    print(f"legacy_005_upgrade_{args.phase}=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
