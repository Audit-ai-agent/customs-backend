"""Add verified review presentation and multi-turn assistant persistence."""

from pathlib import Path

from alembic import op

revision = "20260727_0008"
down_revision = "20260722_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "operational_db"
        / "migrations"
        / "008_commercial_agent_experience.sql"
    )
    op.execute(path.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Rollback is deliberately explicit and is run only after the documented
    # non-sensitive export/drain procedure. Existing V1 tables are untouched.
    op.execute(
        """
        -- The application and workers must already be stopped. These locks make
        -- that operational precondition race-free: a late enqueue/claim cannot
        -- appear between the drain check and the destructive table removal.
        LOCK TABLE operations.product_jobs IN ACCESS EXCLUSIVE MODE;
        LOCK TABLE operations.review_presentations IN ACCESS EXCLUSIVE MODE;
        LOCK TABLE operations.assistant_conversations IN ACCESS EXCLUSIVE MODE;
        LOCK TABLE operations.assistant_jobs IN ACCESS EXCLUSIVE MODE;
        LOCK TABLE operations.assistant_messages IN ACCESS EXCLUSIVE MODE;

        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM operations.product_jobs
            WHERE status IN (
                'queued',
                'running',
                'delivery_uncertain',
                'response_persistence_uncertain'
              )
              AND (
                agent_call_endpoint = 'review_experience'
                OR record_json->'request'->>'experience_api_version' = '3.0'
              )
          ) THEN
            RAISE EXCEPTION
              'TASK-0027 rollback requires every enhanced review job to be drained or reconciled';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM operations.assistant_jobs
            WHERE state IN (
              'queued',
              'running',
              'delivery_uncertain',
              'response_persistence_uncertain'
            )
          ) THEN
            RAISE EXCEPTION
              'TASK-0027 rollback requires every Assistant job to be drained or reconciled';
          END IF;
        END
        $$;

        DROP TABLE IF EXISTS operations.assistant_messages;
        DROP TABLE IF EXISTS operations.assistant_jobs;
        DROP TABLE IF EXISTS operations.assistant_conversations;
        DROP TABLE IF EXISTS operations.review_presentations;

        -- A drained v3 review used the same immutable review request/result wire
        -- as the v2-compatible review boundary. Preserve the hash/call audit but
        -- map the additive endpoint discriminator before restoring the old CHECK.
        UPDATE operations.product_jobs
        SET
          agent_call_endpoint = 'review',
          record_json = jsonb_set(
            record_json,
            '{agent_call_endpoint}',
            to_jsonb('review'::text),
            false
          )
        WHERE agent_call_endpoint = 'review_experience';

        ALTER TABLE operations.product_jobs
          DROP CONSTRAINT IF EXISTS product_jobs_agent_call_binding_check;
        ALTER TABLE operations.product_jobs
          ADD CONSTRAINT product_jobs_agent_call_binding_check
          CHECK (
            (agent_call_state = 'not_started'
              AND agent_call_endpoint IS NULL
              AND agent_call_request_sha256 IS NULL
              AND agent_call_started_at IS NULL)
            OR
            (agent_call_state <> 'not_started'
              AND agent_call_endpoint IN ('review','revision')
              AND agent_call_request_sha256 ~ '^[a-f0-9]{64}$'
              AND agent_call_started_at IS NOT NULL)
          );
        """
    )
