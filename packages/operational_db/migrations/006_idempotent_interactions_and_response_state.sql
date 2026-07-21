-- Durable proposal/chat idempotency, safe 003-005 legacy backfill, bounded
-- supporting metadata, and precise Agent response persistence state.

ALTER TABLE operations.correction_proposals
  ADD COLUMN IF NOT EXISTS idempotency_key text,
  ADD COLUMN IF NOT EXISTS idempotency_request_digest text;

UPDATE operations.correction_proposals
SET idempotency_key = record_json->>'idempotency_key',
    idempotency_request_digest = record_json->>'idempotency_request_digest'
WHERE record_json ? 'idempotency_key';

UPDATE operations.correction_proposals
SET idempotency_key = 'legacy:proposal:' || org_id || ':' || proposal_id,
    idempotency_request_digest = encode(
      sha256(convert_to('legacy-proposal/v1:' || record_json::text, 'UTF8')),
      'hex'
    )
WHERE idempotency_key IS NULL;

UPDATE operations.correction_proposals
SET record_json = record_json || jsonb_build_object(
  'idempotency_key', idempotency_key,
  'idempotency_request_digest', idempotency_request_digest
);

ALTER TABLE operations.correction_proposals
  ALTER COLUMN idempotency_key SET NOT NULL,
  ALTER COLUMN idempotency_request_digest SET NOT NULL,
  ADD CONSTRAINT correction_proposals_idempotency_digest_check
    CHECK (idempotency_request_digest ~ '^[a-f0-9]{64}$');

CREATE UNIQUE INDEX IF NOT EXISTS uq_correction_proposals_org_idempotency
  ON operations.correction_proposals(org_id, idempotency_key);

-- Migration 005 deliberately made human decisions append-only. Drop and
-- recreate that trigger inside this migration transaction so only the reviewed
-- deterministic legacy backfill can alter old rows.
DROP TRIGGER IF EXISTS human_decisions_append_only ON operations.human_decisions;

ALTER TABLE operations.human_decisions
  ADD COLUMN IF NOT EXISTS idempotency_key text,
  ADD COLUMN IF NOT EXISTS idempotency_request_digest text;

UPDATE operations.human_decisions
SET idempotency_key = record_json->>'idempotency_key',
    idempotency_request_digest = record_json->>'idempotency_request_digest'
WHERE record_json ? 'idempotency_key';

UPDATE operations.human_decisions
SET idempotency_key = 'legacy:proposal-decision:' || org_id || ':' || decision_id,
    idempotency_request_digest = encode(
      sha256(convert_to('legacy-proposal-decision/v1:' || record_json::text, 'UTF8')),
      'hex'
    )
WHERE record_json->>'proposal_id' IS NOT NULL
  AND idempotency_key IS NULL;

UPDATE operations.human_decisions
SET record_json = record_json || jsonb_build_object(
  'idempotency_key', idempotency_key,
  'idempotency_request_digest', idempotency_request_digest
)
WHERE idempotency_key IS NOT NULL;

ALTER TABLE operations.human_decisions
  ADD CONSTRAINT human_decisions_idempotency_digest_check
  CHECK (
    (idempotency_key IS NULL AND idempotency_request_digest IS NULL)
    OR (
      idempotency_key IS NOT NULL
      AND idempotency_request_digest ~ '^[a-f0-9]{64}$'
    )
  );

CREATE UNIQUE INDEX IF NOT EXISTS uq_human_decisions_org_idempotency
  ON operations.human_decisions(org_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE TRIGGER human_decisions_append_only
BEFORE UPDATE OR DELETE ON operations.human_decisions
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

ALTER TABLE operations.case_chat_messages
  ADD COLUMN IF NOT EXISTS idempotency_key text,
  ADD COLUMN IF NOT EXISTS idempotency_request_digest text;

UPDATE operations.case_chat_messages
SET idempotency_key = record_json->>'idempotency_key',
    idempotency_request_digest = record_json->>'idempotency_request_digest'
WHERE record_json ? 'idempotency_key';

UPDATE operations.case_chat_messages
SET idempotency_key = 'legacy:chat:' || org_id || ':' || message_id,
    idempotency_request_digest = encode(
      sha256(convert_to('legacy-chat/v1:' || record_json::text, 'UTF8')),
      'hex'
    )
WHERE idempotency_key IS NULL;

UPDATE operations.case_chat_messages
SET record_json = record_json || jsonb_build_object(
  'idempotency_key', idempotency_key,
  'idempotency_request_digest', idempotency_request_digest
);

ALTER TABLE operations.case_chat_messages
  ALTER COLUMN idempotency_key SET NOT NULL,
  ALTER COLUMN idempotency_request_digest SET NOT NULL,
  ADD CONSTRAINT case_chat_idempotency_digest_check
    CHECK (idempotency_request_digest ~ '^[a-f0-9]{64}$');

CREATE UNIQUE INDEX IF NOT EXISTS uq_case_chat_org_idempotency
  ON operations.case_chat_messages(org_id, idempotency_key);

CREATE TABLE IF NOT EXISTS operations.case_chat_interactions (
  interaction_id text PRIMARY KEY,
  review_run_id text NOT NULL
    REFERENCES operations.product_review_runs(review_run_id) ON DELETE RESTRICT,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  idempotency_key text NOT NULL,
  idempotency_request_digest text NOT NULL
    CHECK (idempotency_request_digest ~ '^[a-f0-9]{64}$'),
  agent_request_sha256 text NOT NULL CHECK (agent_request_sha256 ~ '^[a-f0-9]{64}$'),
  state text NOT NULL CHECK (state IN (
    'reserved', 'call_started', 'completed', 'delivery_uncertain',
    'response_received_rejected', 'response_persistence_uncertain'
  )),
  message_id text REFERENCES operations.case_chat_messages(message_id) ON DELETE RESTRICT,
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key),
  CHECK ((state = 'completed' AND message_id IS NOT NULL) OR (state <> 'completed' AND message_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_case_chat_interactions_run
  ON operations.case_chat_interactions(org_id, review_run_id);

INSERT INTO operations.case_chat_interactions (
  interaction_id,
  review_run_id,
  case_id,
  org_id,
  idempotency_key,
  idempotency_request_digest,
  agent_request_sha256,
  state,
  message_id,
  record_json,
  created_at
)
SELECT
  'interaction_legacy_' || encode(
    sha256(convert_to(message.org_id || ':' || message.message_id, 'UTF8')),
    'hex'
  ),
  message.review_run_id,
  message.record_json->>'case_id',
  message.org_id,
  message.idempotency_key,
  message.idempotency_request_digest,
  encode(sha256(convert_to('legacy-chat-request/v1:' || message.record_json::text, 'UTF8')), 'hex'),
  'completed',
  message.message_id,
  jsonb_build_object(
    'interaction_id', 'interaction_legacy_' || encode(
      sha256(convert_to(message.org_id || ':' || message.message_id, 'UTF8')),
      'hex'
    ),
    'review_run_id', message.review_run_id,
    'case_id', message.record_json->>'case_id',
    'org_id', message.org_id,
    'idempotency_key', message.idempotency_key,
    'idempotency_request_digest', message.idempotency_request_digest,
    'agent_request_sha256', encode(
      sha256(convert_to('legacy-chat-request/v1:' || message.record_json::text, 'UTF8')),
      'hex'
    ),
    'state', 'completed',
    'message_id', message.message_id,
    'error_code', NULL,
    'call_started_at', COALESCE(message.record_json->'created_at', to_jsonb(message.created_at)),
    'created_by', message.record_json->>'created_by',
    'created_at', COALESCE(message.record_json->'created_at', to_jsonb(message.created_at)),
    'updated_at', COALESCE(message.record_json->'created_at', to_jsonb(message.created_at))
  ),
  message.created_at
FROM operations.case_chat_messages AS message
ON CONFLICT (org_id, idempotency_key) DO NOTHING;

DROP TRIGGER IF EXISTS case_chat_messages_append_only ON operations.case_chat_messages;
CREATE TRIGGER case_chat_messages_append_only
BEFORE UPDATE OR DELETE ON operations.case_chat_messages
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

-- Enforce the same 100-record Case cap even for concurrent/direct runtime
-- inserts. The advisory lock serializes callers independently of ORM behavior.
CREATE OR REPLACE FUNCTION operations.enforce_supporting_document_case_limit()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('supporting:' || NEW.case_id, 0));
  IF (
    SELECT count(*) FROM operations.supporting_documents
    WHERE org_id = NEW.org_id AND case_id = NEW.case_id
  ) >= 100 THEN
    RAISE EXCEPTION 'Case supporting-document limit of 100 is exhausted'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS supporting_document_case_limit ON operations.supporting_documents;
CREATE TRIGGER supporting_document_case_limit
BEFORE INSERT ON operations.supporting_documents
FOR EACH ROW EXECUTE FUNCTION operations.enforce_supporting_document_case_limit();

ALTER TABLE operations.product_jobs
  DROP CONSTRAINT IF EXISTS product_jobs_status_check,
  DROP CONSTRAINT IF EXISTS product_jobs_agent_call_state_check,
  DROP CONSTRAINT IF EXISTS product_jobs_agent_call_status_check;

ALTER TABLE operations.product_jobs
  ADD CONSTRAINT product_jobs_status_check CHECK (status IN (
    'queued', 'running', 'succeeded', 'failed', 'delivery_uncertain',
    'response_received_rejected', 'response_persistence_uncertain'
  )),
  ADD CONSTRAINT product_jobs_agent_call_state_check
  CHECK (agent_call_state IN (
    'not_started', 'started', 'completed', 'delivery_uncertain',
    'delivery_reconciled', 'response_received_rejected',
    'response_persistence_uncertain'
  )),
  ADD CONSTRAINT product_jobs_agent_call_status_check
  CHECK (
    (status = 'succeeded' AND agent_call_state = 'completed')
    OR (
      status = 'response_received_rejected'
      AND agent_call_state = 'response_received_rejected'
    )
    OR (
      status = 'response_persistence_uncertain'
      AND agent_call_state = 'response_persistence_uncertain'
    )
    OR (status = 'delivery_uncertain' AND agent_call_state = 'delivery_uncertain')
    OR (status = 'running' AND agent_call_state IN ('not_started','started'))
    OR (status = 'queued' AND agent_call_state = 'not_started')
    OR (status = 'failed' AND agent_call_state IN ('not_started','delivery_reconciled'))
  );

DROP INDEX IF EXISTS operations.uq_product_jobs_active_logical_request;
CREATE UNIQUE INDEX uq_product_jobs_active_logical_request
  ON operations.product_jobs(org_id, case_id, case_revision, logical_request_sha256)
  WHERE status IN (
    'queued', 'running', 'delivery_uncertain', 'response_persistence_uncertain'
  );

GRANT SELECT, INSERT ON
  operations.human_decisions,
  operations.case_chat_messages
TO customs_runtime;

GRANT SELECT, INSERT, UPDATE ON
  operations.correction_proposals,
  operations.case_chat_interactions
TO customs_runtime;

REVOKE UPDATE, DELETE ON
  operations.human_decisions,
  operations.case_chat_messages
FROM PUBLIC, customs_runtime;

REVOKE DELETE ON operations.case_chat_interactions FROM PUBLIC, customs_runtime;
