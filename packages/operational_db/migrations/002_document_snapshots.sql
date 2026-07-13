-- Immutable document-processing inputs and intermediate Agent snapshots.
-- These rows make a headless run reproducible without coupling Agent nodes to DB code.

CREATE TABLE operations.documents (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  document_id text NOT NULL,
  filename text NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
  document_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, document_id)
);

CREATE TABLE operations.document_processing_snapshots (
  review_run_id text NOT NULL REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  document_id text NOT NULL,
  classified_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  extracted_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  normalized_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (review_run_id, document_id),
  FOREIGN KEY (review_run_id, document_id)
    REFERENCES operations.documents(review_run_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE operations.extracted_fields (
  review_run_id text NOT NULL,
  document_id text NOT NULL,
  field_path text NOT NULL,
  field_json jsonb NOT NULL,
  PRIMARY KEY (review_run_id, document_id, field_path),
  FOREIGN KEY (review_run_id, document_id)
    REFERENCES operations.documents(review_run_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE operations.pairing_snapshots (
  review_run_id text PRIMARY KEY REFERENCES operations.review_runs(review_run_id) ON DELETE RESTRICT,
  pairing_json jsonb NOT NULL
);

CREATE INDEX idx_documents_content_sha256
  ON operations.documents(content_sha256);
CREATE INDEX idx_extracted_fields_path
  ON operations.extracted_fields(field_path);
