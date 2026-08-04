from __future__ import annotations

from customs_backend.api import create_app

app = create_app()


def main() -> None:
    import uvicorn

    # Local UAT may run this process in an isolated private network. Public
    # exposure and TLS are intentionally outside this source repository.
    uvicorn.run(
        "customs_backend.main:app",
        # The caller is responsible for keeping this listener private.
        host="0.0.0.0",  # nosec B104
        port=8080,
        reload=False,
    )
