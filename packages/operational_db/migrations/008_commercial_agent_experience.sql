-- TASK-0027 additive persistence for verified presentations and multi-turn assistant.
-- Existing Review Run, provider result, Chat V1, HITL, and Audit rows remain unchanged.

-- The v3 experience endpoint uses the existing durable call-once boundary.
-- Widen only the endpoint discriminator; all hash/time/state requirements stay
-- identical to the v1/v2 review and revision paths.
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
      AND agent_call_endpoint IN ('review','revision','review_experience')
      AND agent_call_request_sha256 ~ '^[a-f0-9]{64}$'
      AND agent_call_started_at IS NOT NULL)
  );

CREATE TABLE IF NOT EXISTS operations.review_presentations (
  presentation_id text PRIMARY KEY,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id)
    ON DELETE RESTRICT,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  bound_provider_result_sha256 text NOT NULL
    CHECK (bound_provider_result_sha256 ~ '^[a-f0-9]{64}$'),
  bound_effective_result_digest text NOT NULL
    CHECK (bound_effective_result_digest ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL,
  record_json jsonb NOT NULL,
  UNIQUE (org_id, review_run_id)
);
CREATE INDEX IF NOT EXISTS idx_review_presentations_run
  ON operations.review_presentations(org_id, review_run_id);

CREATE TABLE IF NOT EXISTS operations.assistant_conversations (
  conversation_id text PRIMARY KEY,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id)
    ON DELETE RESTRICT,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  bound_result_sha256 text NOT NULL CHECK (bound_result_sha256 ~ '^[a-f0-9]{64}$'),
  bound_effective_result_digest text NOT NULL
    CHECK (bound_effective_result_digest ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL,
  record_json jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_conversations_run
  ON operations.assistant_conversations(org_id, review_run_id, created_at);

CREATE TABLE IF NOT EXISTS operations.assistant_jobs (
  assistant_job_id text PRIMARY KEY,
  conversation_id text NOT NULL
    REFERENCES operations.assistant_conversations(conversation_id) ON DELETE RESTRICT,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id)
    ON DELETE RESTRICT,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  idempotency_key text NOT NULL,
  idempotency_request_digest text NOT NULL
    CHECK (idempotency_request_digest ~ '^[a-f0-9]{64}$'),
  created_by text NOT NULL,
  state text NOT NULL CHECK (
    state IN (
      'queued',
      'running',
      'succeeded',
      'failed',
      'delivery_uncertain',
      'response_received_rejected',
      'response_persistence_uncertain'
    )
  ),
  created_at timestamptz NOT NULL,
  record_json jsonb NOT NULL,
  UNIQUE (org_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_assistant_jobs_conversation
  ON operations.assistant_jobs(org_id, conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assistant_jobs_run_actor_quota
  ON operations.assistant_jobs(org_id, review_run_id, created_by, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_assistant_jobs_active_conversation
  ON operations.assistant_jobs(conversation_id)
  WHERE state IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS operations.assistant_messages (
  message_id text PRIMARY KEY,
  conversation_id text NOT NULL
    REFERENCES operations.assistant_conversations(conversation_id) ON DELETE RESTRICT,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id)
    ON DELETE RESTRICT,
  org_id text NOT NULL,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  created_at timestamptz NOT NULL,
  record_json jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_messages_conversation
  ON operations.assistant_messages(org_id, conversation_id, created_at, message_id);

DROP TRIGGER IF EXISTS review_presentations_append_only ON operations.review_presentations;
CREATE TRIGGER review_presentations_append_only
BEFORE UPDATE OR DELETE ON operations.review_presentations
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

DROP TRIGGER IF EXISTS assistant_conversations_append_only
  ON operations.assistant_conversations;
CREATE TRIGGER assistant_conversations_append_only
BEFORE UPDATE OR DELETE ON operations.assistant_conversations
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

DROP TRIGGER IF EXISTS assistant_messages_append_only ON operations.assistant_messages;
CREATE TRIGGER assistant_messages_append_only
BEFORE UPDATE OR DELETE ON operations.assistant_messages
FOR EACH ROW EXECUTE FUNCTION operations.reject_append_only_mutation();

REVOKE ALL ON
  operations.review_presentations,
  operations.assistant_conversations,
  operations.assistant_jobs,
  operations.assistant_messages
FROM PUBLIC;

GRANT SELECT, INSERT ON
  operations.review_presentations,
  operations.assistant_conversations,
  operations.assistant_messages
TO customs_runtime;

GRANT SELECT, INSERT, UPDATE ON operations.assistant_jobs TO customs_runtime;

REVOKE UPDATE, DELETE ON
  operations.review_presentations,
  operations.assistant_conversations,
  operations.assistant_messages
FROM PUBLIC, customs_runtime;
