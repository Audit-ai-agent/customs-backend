-- Add queryable chat-reservation provenance for atomic per-run and per-actor
-- quota enforcement. Migration 006 remains immutable for already-deployed DBs.

ALTER TABLE operations.case_chat_interactions
  ADD COLUMN IF NOT EXISTS created_by text;

UPDATE operations.case_chat_interactions
SET created_by = NULLIF(record_json->>'created_by', '')
WHERE created_by IS NULL;

DO $block$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM operations.case_chat_interactions
    WHERE created_by IS NULL OR created_by = ''
  ) THEN
    RAISE EXCEPTION
      'case_chat_interactions contains rows without created_by provenance';
  END IF;
END
$block$;

ALTER TABLE operations.case_chat_interactions
  ALTER COLUMN created_by SET NOT NULL,
  ADD CONSTRAINT case_chat_interactions_created_by_nonempty
    CHECK (created_by <> '');

CREATE INDEX IF NOT EXISTS idx_case_chat_interactions_actor_window
  ON operations.case_chat_interactions(
    org_id,
    review_run_id,
    created_by,
    created_at DESC
  );
