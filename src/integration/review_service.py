"""Shared application service for CLI and visual Agent demonstrations."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from inspect import signature
from ipaddress import ip_address
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, SecretStr, field_validator

from integration.ai_distribution import require_compatible_ai_agent

require_compatible_ai_agent()

# These imports intentionally follow the distribution guard: importing sibling
# source or an unpinned Agent build must fail before application code is loaded.
from customs_ai_agent.adapters.retrieval import (  # noqa: E402
    HttpRegulationRetriever,
    RegulationRetriever,
)
from customs_ai_agent.runner import run_review  # noqa: E402
from customs_ai_agent.schemas.models import ReviewRunRequest  # noqa: E402
from operational_db import (  # noqa: E402
    PostgresOperationalRepository,
    ReviewRunSnapshot,
    snapshot_from_agent_state,
)
from operational_db.repository import OperationalRepository  # noqa: E402


DEFAULT_RAG_URL = "http://127.0.0.1:8010"
DEFAULT_OPS_DSN = "postgresql://trade:change_me@localhost:55432/trade_doc"


class ReadinessError(RuntimeError):
    """The Evidence Service cannot provide a usable readiness document."""


class ReviewCommand(BaseModel):
    scenario_id: str = Field(min_length=1)
    review_run_id: str = Field(
        default_factory=lambda: f"run_{uuid.uuid4().hex}", min_length=1, max_length=128
    )
    review_as_of: date | None = None
    llm_mode: Literal["mock", "llm"] = "mock"
    trade_type: Literal["import", "export"] = "import"
    submitted_document_codes: list[str] = Field(default_factory=list)
    rag_url: str = DEFAULT_RAG_URL
    evidence_service_token: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    store: bool = True
    ops_dsn: str = Field(default=DEFAULT_OPS_DSN, exclude=True, repr=False)

    @field_validator("submitted_document_codes")
    @classmethod
    def _normalize_codes(cls, value: list[str]) -> list[str]:
        return sorted({code.strip() for code in value if code.strip()})

    @field_validator("rag_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("rag_url must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("rag_url must not contain credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("rag_url must not contain an application path")
        if not _is_loopback_host(parsed.hostname):
            raise ValueError(
                "rag_url must target localhost; this transitional tool does not proxy arbitrary hosts"
            )
        return value

    @field_validator("evidence_service_token", mode="before")
    @classmethod
    def _empty_token_is_unset(cls, value: SecretStr | str | None) -> SecretStr | str | None:
        if isinstance(value, SecretStr):
            return value if value.get_secret_value() else None
        return value if value else None


def _is_loopback_host(host: str) -> bool:
    """Return true only for an explicit loopback hostname or IP address."""

    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ReviewExecution:
    snapshot: ReviewRunSnapshot
    state: dict[str, Any] | None
    readback: ReviewRunSnapshot | None
    reused: bool
    stored: bool
    persistence_verified: bool | None
    hash_before: str
    hash_after: str | None

    def to_api_dict(self) -> dict[str, Any]:
        snapshot = self.snapshot
        counts = {
            "documents": len(snapshot.documents),
            "extracted_fields": _extracted_field_count(snapshot.extracted_documents),
            "items": len(snapshot.items),
            "validation_issues": len(snapshot.validation_issues),
            "evidence": len(snapshot.evidence),
            "findings": len(snapshot.regulatory_findings),
            "finding_evidence_links": sum(
                len(finding.get("evidence_ids") or [])
                for finding in snapshot.regulatory_findings
            ),
            "audit_events": len(snapshot.audit_events),
        }
        summary = {
            "case_id": snapshot.case_id,
            "review_run_id": snapshot.review_run_id,
            "final_status": snapshot.final_status,
            "requires_human_review": snapshot.requires_human_review,
            "retrieval_status": snapshot.retrieval_status,
            "retrieval_coverage": snapshot.retrieval_coverage,
            "knowledge_snapshot_id": snapshot.knowledge_snapshot_id,
        }
        if self.stored and self.persistence_verified is not True:
            system_status = "FAIL"
        elif (
            snapshot.retrieval_status == "ok"
            and snapshot.retrieval_coverage == "complete"
            and snapshot.knowledge_snapshot_id
        ):
            system_status = "PASS"
        else:
            system_status = "WARN"
        return {
            "summary": summary,
            "snapshot": snapshot.model_dump(mode="json"),
            "reused": self.reused,
            "stored": self.stored,
            "persistence_verified": self.persistence_verified,
            "hashes": {"before": self.hash_before, "after": self.hash_after},
            "counts": counts,
            "system_status": system_status,
        }


def fetch_readiness(
    rag_url: str,
    timeout: float = 10.0,
    *,
    service_token: str | None = None,
) -> dict[str, Any]:
    url = f"{rag_url.rstrip('/')}/internal/rag/v1/health/ready"
    headers = {"Accept": "application/json"}
    if service_token:
        # The local Data service accepts this header. Never put the token in a URL,
        # exception message, snapshot, or report.
        headers["X-Demo-Token"] = service_token
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ReadinessError(f"Evidence readiness HTTP {exc.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ReadinessError(
            f"Evidence readiness failed: {type(exc).__name__}"
        ) from None
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise ReadinessError("Evidence Service did not return status=ready")
    if not payload.get("supported_review_as_of"):
        raise ReadinessError("Evidence readiness omitted supported_review_as_of")
    return payload


def _http_retriever(rag_url: str, service_token: str | None) -> RegulationRetriever:
    """Construct the pinned Agent retriever without coupling to sibling source.

    The currently published Agent wheel predates optional service-token support.
    It remains compatible when authentication is disabled. If a token is required,
    silently dropping it would turn a configuration error into a confusing 401, so
    an old wheel fails before any review is run. A newer pinned wheel can expose the
    additive ``service_token`` keyword without another Backend source change.
    """

    if not service_token:
        return HttpRegulationRetriever(rag_url)
    if "service_token" not in signature(HttpRegulationRetriever).parameters:
        raise RuntimeError(
            "The installed customs-ai-agent wheel does not support authenticated "
            "Evidence requests; install and pin a compatible Agent wheel"
        )
    return HttpRegulationRetriever(rag_url, service_token=service_token)


def execute_review(
    command: ReviewCommand | dict[str, Any],
    *,
    repository: OperationalRepository | None = None,
    retriever: RegulationRetriever | None = None,
) -> ReviewExecution:
    command = (
        command if isinstance(command, ReviewCommand) else ReviewCommand.model_validate(command)
    )
    review_as_of = command.review_as_of
    service_token = (
        command.evidence_service_token.get_secret_value()
        if command.evidence_service_token is not None
        else None
    )
    if review_as_of is None:
        readiness = fetch_readiness(command.rag_url, service_token=service_token)
        review_as_of = date.fromisoformat(str(readiness["supported_review_as_of"]))

    request = ReviewRunRequest(
        scenario_id=command.scenario_id,
        review_run_id=command.review_run_id,
        review_as_of=review_as_of,
        llm_mode=command.llm_mode,
        trade_type=command.trade_type,
        uploaded_requirement_document_codes=command.submitted_document_codes,
    )
    request_json = request.model_dump(mode="json")

    if command.store and repository is None:
        repository = PostgresOperationalRepository(command.ops_dsn)
    if repository is not None:
        existing = repository.get_run(command.review_run_id)
        if existing is not None:
            if existing.request != request_json:
                raise ValueError(
                    f"review_run_id {command.review_run_id!r} already exists with a different request"
                )
            digest = existing.canonical_hash()
            return ReviewExecution(
                snapshot=existing,
                state=None,
                readback=existing,
                reused=True,
                stored=True,
                persistence_verified=True,
                hash_before=digest,
                hash_after=digest,
            )

    actual_retriever = retriever or _http_retriever(command.rag_url, service_token)
    state = run_review(request, retriever=actual_retriever)
    snapshot = snapshot_from_agent_state(state, request=request_json)
    before = snapshot.canonical_hash()

    readback: ReviewRunSnapshot | None = None
    after: str | None = None
    persistence_verified: bool | None = None
    if repository is not None:
        repository.save_run(snapshot)
        readback = repository.get_run(snapshot.review_run_id)
        if readback is None:
            persistence_verified = False
        else:
            after = readback.canonical_hash()
            persistence_verified = before == after
        if persistence_verified is not True:
            raise RuntimeError("Operational DB readback did not match the saved snapshot")

    return ReviewExecution(
        snapshot=snapshot,
        state=state,
        readback=readback,
        reused=False,
        stored=repository is not None,
        persistence_verified=persistence_verified,
        hash_before=before,
        hash_after=after,
    )


def _extracted_field_count(documents: dict[str, dict[str, Any]]) -> int:
    total = 0
    for document in documents.values():
        total += len(document.get("fields") or {})
        for item in document.get("line_items") or []:
            total += sum(1 for key in item if key != "item_id")
    return total
