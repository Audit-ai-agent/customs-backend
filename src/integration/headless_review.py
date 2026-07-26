"""Run the AI Agent against a loopback Evidence Service without product UI."""

from __future__ import annotations

import argparse
import json
import os
import sys

from integration.review_service import (
    DEFAULT_OPS_DSN,
    ReviewCommand,
    execute_review,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Headless Agent -> Evidence Service -> Operational DB review"
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument(
        "--rag-url",
        default=os.environ.get(
            "EVIDENCE_SERVICE_BASE_URL",
            os.environ.get("CUSTOMS_EVIDENCE_URL", "http://127.0.0.1:8010"),
        ),
    )
    parser.add_argument("--review-run-id")
    parser.add_argument("--review-as-of")
    parser.add_argument("--trade-type", choices=("import", "export"), default="import")
    parser.add_argument(
        "--submitted-document-code",
        action="append",
        default=[],
        help="Canonical requirement document code; may be supplied repeatedly",
    )
    parser.add_argument("--ops-dsn", default=os.environ.get("CUSTOMS_OPS_DSN", DEFAULT_OPS_DSN))
    parser.add_argument("--no-store", action="store_true")
    parser.add_argument(
        "--require-retrieval-ok",
        action="store_true",
        help=(
            "CI gate: exit 1 unless retrieval is ok/complete and a knowledge "
            "snapshot is present; needs_review remains a valid business status"
        ),
    )
    parser.add_argument("--compact", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict:
    payload = {
        "scenario_id": args.scenario,
        "llm_mode": "mock",
        "trade_type": args.trade_type,
        "submitted_document_codes": args.submitted_document_code,
        "rag_url": args.rag_url,
        "evidence_service_token": os.environ.get("EVIDENCE_SERVICE_TOKEN") or None,
        "store": not args.no_store,
        "ops_dsn": args.ops_dsn,
    }
    if args.review_run_id:
        payload["review_run_id"] = args.review_run_id
    if args.review_as_of:
        payload["review_as_of"] = args.review_as_of
    execution = execute_review(ReviewCommand.model_validate(payload))
    snapshot = execution.snapshot
    return {
        "case_id": snapshot.case_id,
        "review_run_id": snapshot.review_run_id,
        "status": snapshot.final_status,
        "requires_human_review": snapshot.requires_human_review,
        "retrieval_status": snapshot.retrieval_status,
        "retrieval_coverage": snapshot.retrieval_coverage,
        "knowledge_snapshot_id": snapshot.knowledge_snapshot_id,
        "evidence_count": len(snapshot.evidence),
        "finding_count": len(snapshot.regulatory_findings),
        "finding_codes": [finding.get("finding_code") for finding in snapshot.regulatory_findings],
        "stored": execution.stored,
        "reused": execution.reused,
        "persistence_verified": execution.persistence_verified,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.compact else 2,
        )
    )
    if args.require_retrieval_ok and not (
        result.get("retrieval_status") == "ok"
        and result.get("retrieval_coverage") == "complete"
        and result.get("knowledge_snapshot_id")
    ):
        return 1
    return 0 if result["status"] in {"completed", "needs_review", "blocked"} else 1


if __name__ == "__main__":
    sys.exit(main())
