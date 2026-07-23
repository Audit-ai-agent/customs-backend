from __future__ import annotations

import dramatiq

from customs_backend.api import build_container
from customs_backend.settings import get_settings


# Configure the production Redis broker before decorators register actors.
_settings = get_settings()
_container_instance = build_container(_settings)


def _container():
    return _container_instance


@dramatiq.actor(
    actor_name="customs_backend.execute_review_job",
    queue_name="customs-review",
    max_retries=2,
    time_limit=_settings.review_actor_time_limit_milliseconds,
)
def execute_review_job(org_id: str, job_id: str, _outbox_id: str) -> None:
    _container().service.execute_job(org_id=org_id, job_id=job_id)


@dramatiq.actor(
    actor_name="customs_backend.dispatch_pending_outbox",
    queue_name="customs-review",
    max_retries=0,
)
def dispatch_pending_outbox() -> int:
    """Invoke from the internal scheduler; duplicate delivery is job-idempotent."""

    return _container().service.dispatch_pending_outbox(limit=100)
