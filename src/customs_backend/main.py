from __future__ import annotations

from customs_backend.api import create_app

app = create_app()


def main() -> None:
    import uvicorn

    # The container listens on its private Docker network; TLS and public
    # exposure are owned by the reverse proxy in compose.production.yaml.
    uvicorn.run(
        "customs_backend.main:app",
        # This is reachable only on the private container network.
        host="0.0.0.0",  # nosec B104
        port=8080,
        reload=False,
    )
