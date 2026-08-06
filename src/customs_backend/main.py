from __future__ import annotations

from customs_backend.api import create_app
from customs_backend.settings import get_settings

app = create_app()


def main() -> None:
    import uvicorn

    # Both supported profiles are a same-machine development/UAT boundary.
    # Never widen this listener through an environment selector.
    get_settings()
    uvicorn.run(
        "customs_backend.main:app",
        host="127.0.0.1",
        # Windows commonly reserves portions of the 80xx range.  Keep the
        # supported loopback topology outside that range so a fresh clone can
        # start without reclaiming a machine-level port reservation.
        port=18081,
        reload=False,
    )
