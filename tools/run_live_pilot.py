"""Run the real Agent -> Evidence HTTP API -> Operational PostgreSQL pilot."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from integration.review_service import ReviewCommand, execute_review


DEFAULT_EVIDENCE_URL = "http://127.0.0.1:8010"
DEFAULT_REVIEW_AS_OF = "2026-07-10"
DEFAULT_OPS_DSN = "postgresql://trade:change_me@127.0.0.1:5432/trade_doc"


def health(base_url: str, kind: str) -> tuple[int, dict]:
    url = f"{base_url.rstrip('/')}/internal/rag/v1/health/{kind}"
    try:
        with urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"detail": "non-JSON health response"}
        return exc.code, parsed
    except (URLError, TimeoutError) as exc:
        return 0, {"detail": f"Evidence Service unavailable: {type(exc).__name__}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rag-url", default=os.environ.get("CUSTOMS_EVIDENCE_URL", DEFAULT_EVIDENCE_URL)
    )
    parser.add_argument(
        "--review-as-of",
        default=os.environ.get("CUSTOMS_REVIEW_AS_OF", DEFAULT_REVIEW_AS_OF),
    )
    parser.add_argument(
        "--ops-dsn", default=os.environ.get("CUSTOMS_OPS_DSN", DEFAULT_OPS_DSN)
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--allow-incomplete-corpus",
        action="store_true",
        help="Targeted diagnostic only: continue when global Data readiness is 503.",
    )
    args = parser.parse_args(argv)

    live_code, live = health(args.rag_url, "live")
    ready_code, ready = health(args.rag_url, "ready")
    report = {
        "evidence_url": args.rag_url,
        "live_http": live_code,
        "ready_http": ready_code,
        "live": live,
        "ready": ready,
        "mode": "TARGETED_DIAGNOSTIC" if args.allow_incomplete_corpus else "RELEASE_GATE",
    }
    if live_code != 200:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    if ready_code != 200 and not args.allow_incomplete_corpus:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 3
    if args.preflight_only:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"pilot-{stamp}-{uuid.uuid4().hex[:8]}"
    scenario = (
        "low_evidence_needs_review"
        if args.allow_incomplete_corpus
        else "exact_hsk_no_requirements"
    )
    command = ReviewCommand.model_validate(
        {
            "scenario_id": scenario,
            "review_run_id": run_id,
            "review_as_of": args.review_as_of,
            "rag_url": args.rag_url,
            "ops_dsn": args.ops_dsn,
            "store": True,
        }
    )
    first = execute_review(command)
    second = execute_review(command)
    snapshot = first.snapshot
    evidence_ids = {
        str(evidence.get("evidence_id"))
        for evidence in snapshot.evidence
        if evidence.get("evidence_id")
    }
    common_checks = {
        "knowledge_snapshot_present": bool(snapshot.knowledge_snapshot_id),
        "evidence_present": bool(snapshot.evidence),
        "stored": first.stored,
        "canonical_readback_verified": first.persistence_verified,
        "idempotent_reuse": second.reused and second.persistence_verified,
    }
    if args.allow_incomplete_corpus:
        policy_checks = {
            "incomplete_retrieval_not_reported_ok": snapshot.retrieval_status
            in {"partial", "failed"},
            "incomplete_retrieval_not_auto_completed": snapshot.final_status == "needs_review"
            and snapshot.requires_human_review,
            "findings_evidence_bound": bool(snapshot.regulatory_findings)
            and all(
                finding.get("evidence_ids")
                and set(map(str, finding["evidence_ids"])).issubset(evidence_ids)
                for finding in snapshot.regulatory_findings
            ),
        }
    else:
        policy_checks = {
            "retrieval_ok": snapshot.retrieval_status == "ok",
            "coverage_complete": snapshot.retrieval_coverage == "complete",
            "automatic_completion_safe": snapshot.final_status == "completed"
            and not snapshot.requires_human_review,
        }
    checks = common_checks | policy_checks
    report.update(
        {
            "review_run_id": run_id,
            "scenario": scenario,
            "review_as_of": args.review_as_of,
            "knowledge_snapshot_id": snapshot.knowledge_snapshot_id,
            "evidence_count": len(snapshot.evidence),
            "finding_count": len(snapshot.regulatory_findings),
            "status": snapshot.final_status,
            "checks": checks,
            "passed": all(checks.values()),
        }
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
