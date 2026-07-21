-- Additive hardening for immutable review delivery and maker-checker decisions.
ALTER TABLE operations.product_jobs
  ADD COLUMN IF NOT EXISTS agent_call_state text NOT NULL DEFAULT 'not_started',
  ADD COLUMN IF NOT EXISTS agent_call_endpoint text,
  ADD COLUMN IF NOT EXISTS agent_call_request_sha256 text,
  ADD COLUMN IF NOT EXISTS agent_call_started_at timestamptz;

ALTER TABLE operations.product_jobs
  DROP CONSTRAINT IF EXISTS product_jobs_status_check;

UPDATE operations.product_jobs
SET record_json = record_json || jsonb_build_object(
  'idempotency_request_digest', request_digest,
  'agent_call_state', 'not_started'
);

-- A running process, or a legacy queued retry with a prior attempt, may already
-- have delivered an external request. It is deliberately quarantined instead
-- of being made retryable.
UPDATE operations.product_jobs
SET status = 'delivery_uncertain',
    agent_call_state = 'delivery_uncertain',
    agent_call_endpoint = kind,
    agent_call_request_sha256 = request_digest,
    agent_call_started_at = created_at,
    lease_id = NULL,
    lease_expires_at = NULL,
    record_json = jsonb_set(
      jsonb_set(
        jsonb_set(
          jsonb_set(record_json, '{status}', '"delivery_uncertain"'::jsonb),
          '{agent_call_state}', '"delivery_uncertain"'::jsonb
        ),
        '{agent_call_endpoint}', to_jsonb(kind)
      ),
      '{agent_call_request_sha256}', to_jsonb(request_digest)
    ) || jsonb_build_object(
      'agent_call_started_at', created_at,
      'lease_id', NULL,
      'lease_expires_at', NULL,
      'error_code', 'agent_call_state_migrated_from_running'
    )
WHERE status = 'running' OR (status = 'queued' AND attempt_count > 0);

UPDATE operations.product_jobs
SET agent_call_state = 'completed',
    agent_call_endpoint = kind,
    agent_call_request_sha256 = request_digest,
    agent_call_started_at = created_at,
    record_json = record_json || jsonb_build_object(
      'agent_call_state', 'completed',
      'agent_call_endpoint', kind,
      'agent_call_request_sha256', request_digest,
      'agent_call_started_at', created_at
    )
WHERE status = 'succeeded';

ALTER TABLE operations.product_jobs
  ADD CONSTRAINT product_jobs_status_check
  CHECK (status IN ('queued','running','succeeded','failed','delivery_uncertain'));
ALTER TABLE operations.product_jobs
  ADD CONSTRAINT product_jobs_agent_call_state_check
  CHECK (agent_call_state IN ('not_started','started','completed','delivery_uncertain')),
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
  ),
  ADD CONSTRAINT product_jobs_agent_call_status_check
  CHECK (
    (status = 'succeeded' AND agent_call_state = 'completed')
    OR (status = 'delivery_uncertain' AND agent_call_state = 'delivery_uncertain')
    OR (status = 'running' AND agent_call_state IN ('not_started','started'))
    OR (status IN ('queued','failed') AND agent_call_state = 'not_started')
  );

ALTER TABLE operations.human_decisions
  ADD COLUMN IF NOT EXISTS decision_scope text;
UPDATE operations.human_decisions
SET decision_scope = CASE
  WHEN record_json->>'proposal_id' IS NOT NULL
    THEN 'proposal:' || (record_json->>'proposal_id')
  ELSE 'run:' || review_run_id
END
WHERE decision_scope IS NULL;
ALTER TABLE operations.human_decisions
  ALTER COLUMN decision_scope SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_human_decisions_org_scope
  ON operations.human_decisions(org_id, decision_scope);
