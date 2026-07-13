CREATE SCHEMA IF NOT EXISTS operations;

CREATE TABLE IF NOT EXISTS operations.cases (
  case_id text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS operations.review_runs (
  review_run_id text PRIMARY KEY,
  case_id text NOT NULL REFERENCES operations.cases(case_id),
  parent_run_id text REFERENCES operations.review_runs(review_run_id),
  schema_version text NOT NULL,
  final_status text NOT NULL CHECK (final_status IN ('completed', 'needs_review', 'blocked')),
  requires_human_review boolean NOT NULL,
  knowledge_snapshot_id text,
  request_json jsonb NOT NULL,
  result_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS operations.review_run_items (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  item_id text NOT NULL,
  description text,
  hs_code text,
  item_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, item_id)
);

CREATE TABLE IF NOT EXISTS operations.validation_issues (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  issue_index integer NOT NULL,
  issue_code text NOT NULL,
  severity text NOT NULL,
  item_id text,
  issue_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, issue_index)
);

CREATE TABLE IF NOT EXISTS operations.evidence_snapshots (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  evidence_id text NOT NULL,
  item_id text NOT NULL,
  source_record_id text NOT NULL,
  version_hash text NOT NULL,
  source_uri text NOT NULL,
  evidence_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, evidence_id, item_id)
);

CREATE TABLE IF NOT EXISTS operations.regulatory_findings (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  finding_index integer NOT NULL,
  finding_code text NOT NULL,
  severity text NOT NULL,
  item_id text NOT NULL,
  finding_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, finding_index)
);

CREATE TABLE IF NOT EXISTS operations.finding_evidence (
  review_run_id text NOT NULL,
  finding_index integer NOT NULL,
  evidence_id text NOT NULL,
  item_id text NOT NULL,
  PRIMARY KEY (review_run_id, finding_index, evidence_id, item_id),
  FOREIGN KEY (review_run_id, finding_index)
    REFERENCES operations.regulatory_findings(review_run_id, finding_index) ON DELETE RESTRICT,
  FOREIGN KEY (review_run_id, evidence_id, item_id)
    REFERENCES operations.evidence_snapshots(review_run_id, evidence_id, item_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS operations.reports (
  review_run_id text PRIMARY KEY REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  report_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS operations.audit_events (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  event_index integer NOT NULL,
  stage text NOT NULL,
  event text NOT NULL,
  event_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, event_index)
);

CREATE TABLE IF NOT EXISTS operations.human_reviews (
  human_review_id text PRIMARY KEY,
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  reviewer_id text,
  corrections_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  decision text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_runs_case_created
  ON operations.review_runs(case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_code_severity
  ON operations.regulatory_findings(finding_code, severity);
CREATE INDEX IF NOT EXISTS idx_evidence_source_version
  ON operations.evidence_snapshots(source_record_id, version_hash);
