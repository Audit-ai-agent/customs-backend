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
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

DEFAULT_EVIDENCE_URL = "http://127.0.0.1:8010"
DEFAULT_OPS_HOST_PORT = 55432
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


def health(
    base_url: str,
    kind: str,
    *,
    service_token: str | None = None,
) -> tuple[int, dict]:
    # Revalidate at the network boundary even when this helper is called directly.
    base_url = validate_loopback_base_url(base_url)
    url = f"{base_url.rstrip('/')}/internal/rag/v1/health/{kind}"
    headers = {"Accept": "application/json"}
    if service_token:
        headers["X-Demo-Token"] = service_token
    request = Request(url, headers=headers, method="GET")
    try:
        # validate_loopback_base_url above limits this request to loopback HTTP.
        with urlopen(request, timeout=10) as response:  # nosec B310
            payload = json.loads(response.read())
            if not isinstance(payload, dict):
                return 0, {"detail": "Evidence health returned a non-object response"}
            if service_token and service_token in json.dumps(payload, ensure_ascii=False):
                return 0, {"detail": "Evidence health response rejected"}
            return response.status, payload
    except HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
        detail = str(parsed.get("detail") or "").casefold()
        classified = next(
            (reason for reason in ALLOWED_INCOMPLETE_REASONS if reason in detail),
            None,
        )
        return exc.code, {"detail": classified or f"Evidence health HTTP {exc.code}"}
    except (URLError, TimeoutError) as exc:
        return 0, {"detail": f"Evidence Service unavailable: {type(exc).__name__}"}


def host_port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def local_ops_dsn(host_port_value: int) -> str:
    """Build a local-only DSN from the same env names used by Data compose."""

    user = quote(os.environ.get("POSTGRES_USER", "trade"), safe="")
    encoded_passphrase = quote(os.environ.get("POSTGRES_PASSWORD", "change_me"), safe="")
    database = quote(os.environ.get("POSTGRES_DB", "trade_doc"), safe="")
    return f"postgresql://{user}:{encoded_passphrase}@127.0.0.1:{host_port_value}/{database}"


def resolve_review_as_of(configured: str | None, readiness: dict) -> str:
    value = configured or readiness.get("supported_review_as_of")
    if not value:
        raise ValueError(
            "review_as_of is required when Evidence readiness does not publish "
            "supported_review_as_of"
        )
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rag-url",
        default=os.environ.get(
            "EVIDENCE_SERVICE_BASE_URL",
            os.environ.get("CUSTOMS_EVIDENCE_URL", DEFAULT_EVIDENCE_URL),
        ),
    )
    parser.add_argument(
        "--review-as-of",
        default=os.environ.get("CUSTOMS_REVIEW_AS_OF"),
        help=(
            "Review date (YYYY-MM-DD). If omitted, a release run uses the exact "
            "supported_review_as_of returned by Evidence readiness."
        ),
    )
    parser.add_argument(
        "--ops-dsn",
        default=os.environ.get("CUSTOMS_OPS_DSN"),
        help=(
            "Operational PostgreSQL DSN. Prefer CUSTOMS_OPS_DSN because a CLI "
            "value can be exposed by the operating-system process list."
        ),
    )
    parser.add_argument(
        "--ops-host-port",
        type=host_port,
        default=host_port(
            os.environ.get(
                "CUSTOMS_OPS_HOST_PORT",
                os.environ.get("POSTGRES_PORT", str(DEFAULT_OPS_HOST_PORT)),
            )
        ),
        help=(
            "Host port for the generated local Operational PostgreSQL DSN when "
            "--ops-dsn/CUSTOMS_OPS_DSN is absent (for example 55432)."
        ),
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--allow-incomplete-corpus",
        action="store_true",
        help="Targeted diagnostic only: continue when global Data readiness is 503.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ops_dsn_source = "configured_dsn" if args.ops_dsn else "generated_local_dsn"
    args.ops_dsn = args.ops_dsn or local_ops_dsn(args.ops_host_port)
    service_token = os.environ.get("EVIDENCE_SERVICE_TOKEN") or None

    try:
        args.rag_url = validate_loopback_base_url(args.rag_url)
    except ValueError as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False))
        return 4

    live_code, live = health(args.rag_url, "live", service_token=service_token)
    ready_code, ready = health(args.rag_url, "ready", service_token=service_token)
    report = {
        "evidence_url": args.rag_url,
        "evidence_auth_configured": service_token is not None,
        "live_http": live_code,
        "ready_http": ready_code,
        "live": live,
        "ready": ready,
        "llm_mode": "mock",
        "ops_dsn_source": ops_dsn_source,
        "ops_host_port": (args.ops_host_port if ops_dsn_source == "generated_local_dsn" else None),
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

    try:
        review_as_of = resolve_review_as_of(args.review_as_of, ready)
    except ValueError as exc:
        report["configuration_error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 4

    # Delay the Agent distribution guard until after dependency readiness is
    # established, so preflight can diagnose Data without an Agent install.
    from integration.review_service import ReviewCommand, execute_review

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"pilot-{stamp}-{uuid.uuid4().hex[:8]}"
    scenario = (
        "low_evidence_needs_review" if args.allow_incomplete_corpus else "exact_hsk_no_requirements"
    )
    command = ReviewCommand.model_validate(
        {
            "scenario_id": scenario,
            "review_run_id": run_id,
            "review_as_of": review_as_of,
            "llm_mode": "mock",
            "rag_url": args.rag_url,
            "evidence_service_token": service_token,
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
            "review_as_of": review_as_of,
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
