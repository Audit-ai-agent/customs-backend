from __future__ import annotations

import dramatiq

from customs_backend.api import build_container
from customs_backend.ports import (
    AgentPreExecutionRetriesExhaustedError,
    AgentPreExecutionRetryableError,
    JobExecutionNotRunnableError,
)
from customs_backend.settings import get_settings


# Configure the production Redis broker before decorators register actors.
_settings = get_settings()
_container_instance = build_container(_settings)


def _container():
    return _container_instance


@dramatiq.actor(
    actor_name="customs_backend.execute_review_job",
    queue_name="customs-review",
    # Dramatiq compares the pre-increment retry count with max_retries.
    # Two therefore permits exactly two delayed redeliveries after the initial
    # delivery (three service attempts), matching Settings.job_max_attempts.
    max_retries=2,
    time_limit=_settings.review_actor_time_limit_milliseconds,
)
def execute_review_job(org_id: str, job_id: str, _outbox_id: str) -> None:
    try:
        _container().service.execute_job(org_id=org_id, job_id=job_id)
    except AgentPreExecutionRetryableError as exc:
        raise dramatiq.Retry(
            "Agent admission dependency is not ready",
            delay=_settings.agent_pre_execution_retry_delay_seconds * 1_000,
        ) from exc
    except (AgentPreExecutionRetriesExhaustedError, JobExecutionNotRunnableError):
        # These states are already durable and terminal for automatic delivery.
        # Returning prevents Dramatiq from scheduling a meaningless retry.
        return


@dramatiq.actor(
    actor_name="customs_backend.dispatch_pending_outbox",
    queue_name="customs-review",
    max_retries=0,
)
def dispatch_pending_outbox() -> int:
    """Invoke from the internal scheduler; duplicate delivery is job-idempotent."""

    return _container().service.dispatch_pending_outbox(limit=100)
