"""Run the real Agent -> Evidence HTTP API -> Operational PostgreSQL pilot."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

DEFAULT_EVIDENCE_URL = "http://127.0.0.1:8010"
DEFAULT_REVIEW_AS_OF = "2026-07-10"
DEFAULT_OPS_DSN = "postgresql://trade:change_me@127.0.0.1:5432/trade_doc"
ALLOWED_INCOMPLETE_REASONS = ("law corpus incomplete", "law index incomplete")


def validate_loopback_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Evidence URL must be an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Evidence URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("Evidence URL must not contain an application path")
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        raise ValueError("Evidence URL must resolve syntactically to loopback")
    return value.rstrip("/")


def readiness_is_complete(payload: dict) -> bool:
    corpus = payload.get("law_corpus")
    index = payload.get("law_index")
    return bool(
        payload.get("status") == "ready"
        and payload.get("knowledge_snapshot_id")
        and payload.get("supported_review_as_of")
        and isinstance(corpus, dict)
        and corpus.get("expected")
        and corpus.get("expected") == corpus.get("loaded")
        and isinstance(index, dict)
        and index.get("expected")
        and index.get("expected") == index.get("postgres_indexed") == index.get("qdrant")
    )


def is_allowed_incomplete_readiness(code: int, payload: dict) -> bool:
    detail = str(payload.get("detail") or "").lower()
    return code == 503 and any(reason in detail for reason in ALLOWED_INCOMPLETE_REASONS)


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

    try:
        args.rag_url = validate_loopback_base_url(args.rag_url)
    except ValueError as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False))
        return 4

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
    if live_code != 200 or live.get("status") != "alive":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    ready_complete = ready_code == 200 and readiness_is_complete(ready)
    diagnostic_incomplete = args.allow_incomplete_corpus and is_allowed_incomplete_readiness(
        ready_code, ready
    )
    if not ready_complete and not diagnostic_incomplete:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 3
    if args.preflight_only:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0

    # Delay the Agent distribution guard until after dependency readiness is
    # established, so preflight can diagnose Data without an Agent install.
    from integration.review_service import ReviewCommand, execute_review

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
