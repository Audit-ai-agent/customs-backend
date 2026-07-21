-- Product workbench tables are additive. Legacy review snapshots remain immutable.
CREATE TABLE IF NOT EXISTS operations.product_cases (
  case_id text PRIMARY KEY,
  org_id text NOT NULL,
  revision integer NOT NULL CHECK (revision >= 0),
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_product_cases_org ON operations.product_cases(org_id);

CREATE TABLE IF NOT EXISTS operations.product_documents (
  document_id text PRIMARY KEY,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_product_documents_case ON operations.product_documents(org_id, case_id);

CREATE TABLE IF NOT EXISTS operations.supporting_documents (
  supporting_document_id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES operations.product_documents(document_id) ON DELETE RESTRICT,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_supporting_documents_case
  ON operations.supporting_documents(org_id, case_id);

CREATE TABLE IF NOT EXISTS operations.product_jobs (
  job_id text PRIMARY KEY,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  idempotency_key text NOT NULL,
  request_digest text NOT NULL CHECK (request_digest ~ '^[a-f0-9]{64}$'),
  status text NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  kind text NOT NULL CHECK (kind IN ('review','revision')),
  lease_id text,
  lease_expires_at timestamptz,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_product_jobs_case ON operations.product_jobs(org_id, case_id);

CREATE TABLE IF NOT EXISTS operations.product_review_runs (
  review_run_id text PRIMARY KEY,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  parent_run_id text REFERENCES operations.product_review_runs(review_run_id) ON DELETE RESTRICT,
  case_revision integer NOT NULL CHECK (case_revision >= 0),
  result_digest text NOT NULL CHECK (result_digest ~ '^[a-f0-9]{64}$'),
  execution_status text NOT NULL CHECK (execution_status IN ('succeeded','partial','failed')),
  assessment text NOT NULL CHECK (assessment IN ('no_issue_detected','issues_detected','indeterminate')),
  review_disposition text NOT NULL CHECK (review_disposition IN ('auto_review_complete','human_review_required','blocked')),
  assurance_level text NOT NULL CHECK (assurance_level IN ('diagnostic','production_candidate','production')),
  provider_result_json jsonb NOT NULL,
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_product_runs_case ON operations.product_review_runs(org_id, case_id);

CREATE TABLE IF NOT EXISTS operations.human_decisions (
  decision_id text PRIMARY KEY,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approve','reject')),
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_human_decisions_run ON operations.human_decisions(org_id, review_run_id);

CREATE TABLE IF NOT EXISTS operations.correction_proposals (
  proposal_id text PRIMARY KEY,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id) ON DELETE RESTRICT,
  case_id text NOT NULL REFERENCES operations.product_cases(case_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending','approved','rejected')),
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_correction_proposals_run
  ON operations.correction_proposals(org_id, review_run_id);

CREATE TABLE IF NOT EXISTS operations.case_chat_messages (
  message_id text PRIMARY KEY,
  review_run_id text NOT NULL REFERENCES operations.product_review_runs(review_run_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  bound_result_digest text NOT NULL CHECK (bound_result_digest ~ '^[a-f0-9]{64}$'),
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_case_chat_run ON operations.case_chat_messages(org_id, review_run_id);

CREATE TABLE IF NOT EXISTS operations.product_audit_log (
  audit_id text PRIMARY KEY,
  org_id text NOT NULL,
  case_id text,
  created_at timestamptz NOT NULL,
  record_json jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_audit_case ON operations.product_audit_log(org_id, case_id, created_at);

CREATE TABLE IF NOT EXISTS operations.product_outbox (
  outbox_id text PRIMARY KEY,
  org_id text NOT NULL,
  aggregate_id text NOT NULL,
  topic text NOT NULL,
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  lease_id text,
  lease_expires_at timestamptz,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_product_outbox_aggregate ON operations.product_outbox(org_id, aggregate_id);

CREATE TABLE IF NOT EXISTS operations.review_stage_events (
  event_id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES operations.product_jobs(job_id) ON DELETE RESTRICT,
  org_id text NOT NULL,
  sequence integer NOT NULL CHECK (sequence > 0),
  record_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_stage_events_job ON operations.review_stage_events(org_id, job_id, sequence);
