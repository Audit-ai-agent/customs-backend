from __future__ import annotations

import json
from pathlib import Path

from customs_backend.adapters import CallableAgentClient, InMemoryObjectStore
from customs_backend.api import Container, create_app
from customs_backend.auth import StaticTokenVerifier
from customs_backend.repository import InMemoryWorkbenchRepository
from customs_backend.service import WorkbenchService
from customs_backend.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
# The browser-facing Backend API is /api/v1.  Keep one generated public
# snapshot for it; there is no second Browser API surface.
DEFAULT_DESTINATION = ROOT / "docs" / "openapi-v1.json"


def _schema_app():
    """Build an inert app solely to render the public API schema.

    Contract generation needs no env file or running sibling service. No
    request is sent through this container.
    """

    settings = Settings(_env_file=None, environment="test")
    repository = InMemoryWorkbenchRepository()
    object_store = InMemoryObjectStore()
    agent = CallableAgentClient(lambda _request: {})
    service = WorkbenchService(
        repository=repository,
        object_store=object_store,
        agent=agent,
    )
    return create_app(
        settings=settings,
        container=Container(
            settings=settings,
            repository=repository,
            object_store=object_store,
            agent=agent,
            token_verifier=StaticTokenVerifier({}),
            service=service,
        ),
    )


def render_product_openapi() -> bytes:
    """Return the canonical, reproducible current public Backend OpenAPI bytes."""

    encoded = json.dumps(
        _schema_app().openapi(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (encoded + "\n").encode("utf-8")


def export_product_openapi(destination: Path = DEFAULT_DESTINATION) -> None:
    destination.write_bytes(render_product_openapi())


def main() -> None:
    export_product_openapi()


if __name__ == "__main__":
    main()
