from __future__ import annotations

import json
from pathlib import Path

from customs_backend.api import create_app


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / "docs" / "openapi-v2.json"


def render_product_openapi() -> bytes:
    """Return the canonical, reproducible current public Backend OpenAPI bytes."""

    encoded = json.dumps(
        create_app().openapi(),
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
