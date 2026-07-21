-- Runtime/migration separation, logical call uniqueness, and immutable ledgers.
-- Alembic runs as the object-owning migration principal. The application must
-- connect as customs_runtime; Settings rejects any other production username.

DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'customs_runtime') THEN
    CREATE ROLE customs_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$roles$;

ALTER TABLE operations.product_jobs
  ADD COLUMN IF NOT EXISTS case_revision integer,
  ADD COLUMN IF NOT EXISTS logical_request_sha256 text;

UPDATE operations.product_jobs
SET case_revision = COALESCE((record_json->'request'->>'case_revision')::integer, 0),
    logical_request_sha256 = request_digest,
    record_json = record_json || jsonb_build_object(
      'case_revision', COALESCE((record_json->'request'->>'case_revision')::integer, 0),
      'logical_request_sha256', request_digest
    )
WHERE case_revision IS NULL OR logical_request_sha256 IS NULL;

ALTER TABLE operations.product_jobs
  ALTER COLUMN case_revision SET NOT NULL,
  ALTER COLUMN logical_request_sha256 SET NOT NULL,
  ADD CONSTRAINT product_jobs_case_revision_check CHECK (case_revision >= 0),
  ADD CONSTRAINT product_jobs_logical_request_sha256_check
    CHECK (logical_request_sha256 ~ '^[a-f0-9]{64}$');

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_jobs_active_logical_request
  ON operations.product_jobs(org_id, case_id, case_revision, logical_request_sha256)
  WHERE status IN ('queued', 'running', 'delivery_uncertain');

ALTER TABLE operations.product_jobs
  DROP CONSTRAINT IF EXISTS product_jobs_agent_call_state_check,
  DROP CONSTRAINT IF EXISTS product_jobs_agent_call_status_check;

ALTER TABLE operations.product_jobs
  ADD CONSTRAINT product_jobs_agent_call_state_check
  CHECK (agent_call_state IN (
    'not_started', 'started', 'completed', 'delivery_uncertain', 'delivery_reconciled'
  )),
  ADD CONSTRAINT product_jobs_agent_call_status_check
  CHECK (
    (status = 'succeeded' AND agent_call_state = 'completed')
    OR (status = 'delivery_uncertain' AND agent_call_state = 'delivery_uncertain')
    OR (status = 'running' AND agent_call_state IN ('not_started','started'))
    OR (status = 'queued' AND agent_call_state = 'not_started')
    OR (status = 'failed' AND agent_call_state IN ('not_started','delivery_reconciled'))
  );

CREATE OR REPLACE FUNCTION operations.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  RAISE EXCEPTION 'table %.% is append-only', TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = '55000';
END
$function$;

DROP TRIGGER IF EXISTS product_review_runs_append_only ON operations.product_review_runs;
CREATE TRIGGER product_review_runs_append_only
BEFORE UPDATE OR DELETE ON operations.product_review_runs
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

DROP TRIGGER IF EXISTS human_decisions_append_only ON operations.human_decisions;
CREATE TRIGGER human_decisions_append_only
BEFORE UPDATE OR DELETE ON operations.human_decisions
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

DROP TRIGGER IF EXISTS product_audit_log_append_only ON operations.product_audit_log;
CREATE TRIGGER product_audit_log_append_only
BEFORE UPDATE OR DELETE ON operations.product_audit_log
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

DROP TRIGGER IF EXISTS review_stage_events_append_only ON operations.review_stage_events;
CREATE TRIGGER review_stage_events_append_only
BEFORE UPDATE OR DELETE ON operations.review_stage_events
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

REVOKE ALL ON SCHEMA operations FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA operations FROM PUBLIC;
GRANT USAGE ON SCHEMA operations TO customs_runtime;

GRANT SELECT, INSERT ON
  operations.product_documents,
  operations.product_review_runs,
  operations.human_decisions,
  operations.case_chat_messages,
  operations.product_audit_log,
  operations.review_stage_events
TO customs_runtime;

GRANT SELECT, INSERT, UPDATE ON
  operations.product_cases,
  operations.supporting_documents,
  operations.product_jobs,
  operations.correction_proposals,
  operations.product_outbox
TO customs_runtime;

REVOKE UPDATE, DELETE ON
  operations.product_review_runs,
  operations.human_decisions,
  operations.product_audit_log,
  operations.review_stage_events
FROM PUBLIC, customs_runtime;
