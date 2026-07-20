"""Local-only visual demo for the Mock Agent -> live RAG -> Ops DB path.

This module is an unauthenticated engineering harness, not a product backend.
Both the listening socket and dependency URLs are deliberately loopback-only.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from customs_ai_agent.fixtures.scenarios import list_scenarios
from integration.review_service import (
    DEFAULT_OPS_DSN,
    DEFAULT_RAG_URL,
    ReadinessError,
    ReviewCommand,
    ReviewExecution,
    execute_review,
    fetch_readiness,
    _is_loopback_host,
)
from operational_db import OperationalIntegrityError, PostgresOperationalRepository


ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "agent_webui.html"
OPS_DSN = os.environ.get("CUSTOMS_OPS_DSN", os.environ.get("POSTGRES_DSN", DEFAULT_OPS_DSN))
RAG_URL = os.environ.get(
    "EVIDENCE_SERVICE_BASE_URL", os.environ.get("RAG_URL", DEFAULT_RAG_URL)
).rstrip("/")
EVIDENCE_SERVICE_TOKEN = os.environ.get("EVIDENCE_SERVICE_TOKEN") or None


def _readiness_fetcher(service_token: str | None):
    """Bind a token without exposing it through ``functools.partial`` repr."""

    def fetch(rag_url: str) -> dict[str, Any]:
        return fetch_readiness(rag_url, service_token=service_token)

    return fetch


class AgentReviewPayload(BaseModel):
    scenario_id: str = Field(min_length=1)
    review_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    review_as_of: date | None = None
    trade_type: Literal["import", "export"] = "import"
    submitted_document_codes: list[str] = Field(default_factory=list)
    store: bool = True
    rag_url: str | None = None

    @field_validator("submitted_document_codes")
    @classmethod
    def _codes(cls, value: list[str]) -> list[str]:
        return sorted({code.strip() for code in value if code.strip()})

    @field_validator("rag_url")
    @classmethod
    def _rag_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Keep one validation policy for CLI and UI payloads.
        return ReviewCommand(scenario_id="validation-only", rag_url=value, store=False).rag_url


def _ops_health() -> dict[str, Any]:
    try:
        import psycopg

        with psycopg.connect(OPS_DSN, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            ok = cur.fetchone() == (1,)
        return {"status": "ready" if ok else "failed"}
    except Exception:  # pragma: no cover - exercised by live health
        return {"status": "failed", "error": "Operational DB health check failed"}


def _execution_for_stored(snapshot) -> ReviewExecution:
    digest = snapshot.canonical_hash()
    return ReviewExecution(
        snapshot=snapshot,
        state=None,
        readback=snapshot,
        reused=True,
        stored=True,
        persistence_verified=True,
        hash_before=digest,
        hash_after=digest,
    )


def create_app() -> FastAPI:
    application = FastAPI(
        title="Customs AI Agent Visual Demo",
        description="Mock documents, live Agent workflow, live Evidence/RAG, immutable DB readback",
        version="1.0",
    )
    application.state.review_executor = execute_review
    application.state.repository_factory = lambda: PostgresOperationalRepository(OPS_DSN)
    application.state.readiness_fetcher = _readiness_fetcher(EVIDENCE_SERVICE_TOKEN)
    application.state.ops_health_checker = _ops_health
    application.state.run_lock = Lock()

    @application.middleware("http")
    async def localhost_only(request: Request, call_next):
        client_host = request.client.host if request.client else ""
        # Starlette's in-process TestClient uses this sentinel host.
        if client_host != "testclient" and not _is_loopback_host(client_host):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "This unauthenticated engineering demo is available from "
                        "the local machine only."
                    )
                },
            )
        return await call_next(request)

    @application.get("/", response_class=HTMLResponse)
    def index() -> str:
        if not HTML_PATH.exists():
            raise HTTPException(500, "agent_webui.html is missing")
        return HTML_PATH.read_text(encoding="utf-8")

    @application.get("/api/health")
    def health() -> dict[str, Any]:
        try:
            evidence = application.state.readiness_fetcher(RAG_URL)
        except Exception:
            evidence = {
                "status": "failed",
                "error": "Evidence Service readiness check failed",
            }
        operations = application.state.ops_health_checker()
        ready = evidence.get("status") == "ready" and operations.get("status") == "ready"
        return {
            "status": "ready" if ready else "degraded",
            "evidence": evidence,
            "operational_db": operations,
            "rag_url": RAG_URL,
            "modes": {
                "document_input": "MOCK",
                "extraction": "MOCK",
                "agent_workflow": "LIVE",
                "regulation_retriever": "LIVE_HTTP",
                "knowledge_db": "LIVE",
                "operational_db": "LIVE",
            },
        }

    @application.get("/api/scenarios")
    def scenarios() -> dict[str, list[str]]:
        return {"scenarios": list_scenarios()}

    @application.post("/api/reviews")
    def create_review(payload: AgentReviewPayload) -> dict[str, Any]:
        command_data = {
            "scenario_id": payload.scenario_id,
            "llm_mode": "mock",
            "review_as_of": payload.review_as_of,
            "trade_type": payload.trade_type,
            "submitted_document_codes": payload.submitted_document_codes,
            "rag_url": (payload.rag_url or RAG_URL),
            "evidence_service_token": EVIDENCE_SERVICE_TOKEN,
            "store": payload.store,
            "ops_dsn": OPS_DSN,
        }
        if payload.review_run_id:
            command_data["review_run_id"] = payload.review_run_id
        command = ReviewCommand.model_validate(command_data)
        repository = application.state.repository_factory() if payload.store else None
        try:
            with application.state.run_lock:
                execution = application.state.review_executor(
                    command,
                    repository=repository,
                )
            return execution.to_api_dict()
        except KeyError:
            raise HTTPException(400, "Unknown Agent scenario") from None
        except ReadinessError:
            raise HTTPException(503, "Evidence Service is not ready") from None
        except (OperationalIntegrityError, ValueError):
            raise HTTPException(409, "Review conflict or invalid state") from None
        except Exception:
            raise HTTPException(500, "Review execution failed") from None

    @application.get("/api/reviews/{review_run_id}")
    def get_review(review_run_id: str) -> dict[str, Any]:
        try:
            snapshot = application.state.repository_factory().get_run(review_run_id)
        except Exception:
            raise HTTPException(503, "Operational DB read failed") from None
        if snapshot is None:
            raise HTTPException(404, f"review run not found: {review_run_id}")
        return _execution_for_stored(snapshot).to_api_dict()

    return application


app = create_app()


def main() -> None:
    """Run the engineering UI on loopback only."""

    import uvicorn

    port = int(os.environ.get("CUSTOMS_AGENT_DEMO_PORT", "8020"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
