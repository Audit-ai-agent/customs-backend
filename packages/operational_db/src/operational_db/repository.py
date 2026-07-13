from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from typing import Any, Protocol

from pydantic_core import to_jsonable_python

from operational_db.models import ReviewRunSnapshot


class OperationalIntegrityError(RuntimeError):
    pass


class OperationalRepository(Protocol):
    def save_run(self, snapshot: ReviewRunSnapshot | dict[str, Any]) -> ReviewRunSnapshot: ...

    def get_run(self, review_run_id: str) -> ReviewRunSnapshot | None: ...


class InMemoryOperationalRepository:
    def __init__(self) -> None:
        self._runs: dict[str, tuple[str, dict[str, Any]]] = {}

    def save_run(self, snapshot: ReviewRunSnapshot | dict[str, Any]) -> ReviewRunSnapshot:
        parsed = snapshot if isinstance(snapshot, ReviewRunSnapshot) else ReviewRunSnapshot.model_validate(snapshot)
        digest = parsed.canonical_hash()
        previous = self._runs.get(parsed.review_run_id)
        if previous:
            if previous[0] != digest:
                raise OperationalIntegrityError(
                    f"review_run_id {parsed.review_run_id!r} already contains a different snapshot"
                )
            return ReviewRunSnapshot.model_validate(deepcopy(previous[1]))
        if parsed.parent_run_id and parsed.parent_run_id not in self._runs:
            raise OperationalIntegrityError("parent_run_id must reference an existing immutable run")
        payload = parsed.model_dump(mode="json")
        self._runs[parsed.review_run_id] = (digest, deepcopy(payload))
        return ReviewRunSnapshot.model_validate(payload)

    def get_run(self, review_run_id: str) -> ReviewRunSnapshot | None:
        stored = self._runs.get(review_run_id)
        return ReviewRunSnapshot.model_validate(deepcopy(stored[1])) if stored else None


class PostgresOperationalRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            to_jsonable_python(value),
            ensure_ascii=False,
            sort_keys=True,
        )

    def save_run(self, snapshot: ReviewRunSnapshot | dict[str, Any]) -> ReviewRunSnapshot:
        parsed = snapshot if isinstance(snapshot, ReviewRunSnapshot) else ReviewRunSnapshot.model_validate(snapshot)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - installation error path
            raise RuntimeError("psycopg is required for PostgresOperationalRepository") from exc

        with psycopg.connect(self._dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT result_json FROM operations.review_runs WHERE review_run_id=%s",
                (parsed.review_run_id,),
            )
            existing = cur.fetchone()
            if existing:
                stored = ReviewRunSnapshot.model_validate(existing["result_json"])
                if stored.canonical_hash() != parsed.canonical_hash():
                    raise OperationalIntegrityError("review_run_id already contains a different snapshot")
                return stored

            if parsed.parent_run_id:
                cur.execute(
                    "SELECT 1 FROM operations.review_runs WHERE review_run_id=%s",
                    (parsed.parent_run_id,),
                )
                if cur.fetchone() is None:
                    raise OperationalIntegrityError("parent_run_id does not exist")

            cur.execute(
                "INSERT INTO operations.cases(case_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (parsed.case_id,),
            )
            result_json = parsed.model_dump(mode="json")
            cur.execute(
                """
                INSERT INTO operations.review_runs
                  (review_run_id, case_id, parent_run_id, schema_version, final_status,
                   requires_human_review, knowledge_snapshot_id, request_json, result_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                """,
                (
                    parsed.review_run_id,
                    parsed.case_id,
                    parsed.parent_run_id,
                    parsed.schema_version,
                    parsed.final_status,
                    parsed.requires_human_review,
                    parsed.knowledge_snapshot_id,
                    self._json(parsed.request),
                    self._json(result_json),
                ),
            )

            for document in parsed.documents:
                content = str(document.get("content_text") or "")
                cur.execute(
                    """
                    INSERT INTO operations.documents
                      (review_run_id,document_id,filename,content_sha256,document_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        document["document_id"],
                        document.get("filename") or document["document_id"],
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        self._json(document),
                    ),
                )

            for document in parsed.documents:
                document_id = document["document_id"]
                classified = parsed.classified_documents.get(document_id, {})
                extracted = parsed.extracted_documents.get(document_id, {})
                normalized = parsed.normalized_documents.get(document_id, {})
                cur.execute(
                    """
                    INSERT INTO operations.document_processing_snapshots
                      (review_run_id,document_id,classified_json,extracted_json,normalized_json)
                    VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        document_id,
                        self._json(classified),
                        self._json(extracted),
                        self._json(normalized),
                    ),
                )
                for field_path, field_value in self._extracted_fields(extracted):
                    cur.execute(
                        """
                        INSERT INTO operations.extracted_fields
                          (review_run_id,document_id,field_path,field_json)
                        VALUES (%s,%s,%s,%s::jsonb)
                        """,
                        (
                            parsed.review_run_id,
                            document_id,
                            field_path,
                            self._json(field_value),
                        ),
                    )

            cur.execute(
                "INSERT INTO operations.pairing_snapshots(review_run_id,pairing_json) "
                "VALUES (%s,%s::jsonb)",
                (parsed.review_run_id, self._json(parsed.pairing_result)),
            )

            for item in parsed.items:
                cur.execute(
                    """
                    INSERT INTO operations.review_run_items
                      (review_run_id,item_id,description,hs_code,item_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        item["item_id"],
                        item.get("description"),
                        item.get("hs_code"),
                        self._json(item),
                    ),
                )

            for index, issue in enumerate(parsed.validation_issues):
                cur.execute(
                    """
                    INSERT INTO operations.validation_issues
                      (review_run_id,issue_index,issue_code,severity,item_id,issue_json)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        index,
                        issue["issue_code"],
                        issue["severity"],
                        issue.get("item_id"),
                        self._json(issue),
                    ),
                )

            for evidence in parsed.evidence:
                cur.execute(
                    """
                    INSERT INTO operations.evidence_snapshots
                      (review_run_id,evidence_id,item_id,source_record_id,version_hash,source_uri,evidence_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        evidence["evidence_id"],
                        evidence["item_id"],
                        evidence["source_record_id"],
                        evidence["version_hash"],
                        evidence["source_uri"],
                        self._json(evidence),
                    ),
                )

            for index, finding in enumerate(parsed.regulatory_findings):
                cur.execute(
                    """
                    INSERT INTO operations.regulatory_findings
                      (review_run_id,finding_index,finding_code,severity,item_id,finding_json)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        index,
                        finding["finding_code"],
                        finding["severity"],
                        finding["item_id"],
                        self._json(finding),
                    ),
                )
                for evidence_id in finding["evidence_ids"]:
                    cur.execute(
                        """
                        INSERT INTO operations.finding_evidence
                          (review_run_id,finding_index,evidence_id,item_id)
                        VALUES (%s,%s,%s,%s)
                        """,
                        (parsed.review_run_id, index, evidence_id, finding["item_id"]),
                    )

            cur.execute(
                "INSERT INTO operations.reports(review_run_id,report_json) VALUES (%s,%s::jsonb)",
                (parsed.review_run_id, self._json(parsed.report)),
            )
            for index, event in enumerate(parsed.audit_events):
                cur.execute(
                    """
                    INSERT INTO operations.audit_events
                      (review_run_id,event_index,stage,event,event_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        parsed.review_run_id,
                        index,
                        event.get("stage", "unknown"),
                        event.get("event", "unknown"),
                        self._json(event),
                    ),
                )
        return parsed

    @staticmethod
    def _extracted_fields(extracted: dict[str, Any]) -> list[tuple[str, Any]]:
        rows = [
            (f"fields.{name}", value)
            for name, value in (extracted.get("fields") or {}).items()
        ]
        for index, item in enumerate(extracted.get("line_items") or []):
            item_key = item.get("item_id") or str(index)
            rows.extend(
                (f"line_items.{item_key}.{name}", value)
                for name, value in item.items()
                if name != "item_id"
            )
        return rows

    def get_run(self, review_run_id: str) -> ReviewRunSnapshot | None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for PostgresOperationalRepository") from exc
        with psycopg.connect(self._dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT result_json FROM operations.review_runs WHERE review_run_id=%s",
                (review_run_id,),
            )
            row = cur.fetchone()
            return ReviewRunSnapshot.model_validate(row["result_json"]) if row else None
