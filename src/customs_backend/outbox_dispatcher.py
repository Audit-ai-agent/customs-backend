from __future__ import annotations

import signal
import time

from customs_backend.adapters import DramatiqJobPublisher, InMemoryObjectStore
from customs_backend.api import _UnavailableAgentClient
from customs_backend.repository import PostgresWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import get_settings


def main() -> None:
    settings = get_settings()
    if (
        settings.environment != "production"
        or settings.process_role != "outbox"
        or settings.database_url is None
        or settings.redis_url is None
    ):
        raise RuntimeError("outbox dispatcher requires the minimal production outbox role")
    service = WorkbenchService(
        repository=PostgresWorkbenchRepository(settings.database_url.get_secret_value()),
        object_store=InMemoryObjectStore(),
        publisher=DramatiqJobPublisher(settings.redis_url.get_secret_value()),
        agent=_UnavailableAgentClient(),
    )
    running = True

    def stop(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        service.dispatch_pending_outbox(limit=100)
        time.sleep(5)
