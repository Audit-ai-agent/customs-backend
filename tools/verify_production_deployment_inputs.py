from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess  # nosec B404
from pathlib import Path
from urllib.parse import urlsplit

IMAGE_VARIABLES = (
    "AGENT_IMAGE",
    "BACKEND_IMAGE",
    "DATA_IMAGE",
    "EGRESS_PROXY_IMAGE",
    "EVIDENCE_PROXY_IMAGE",
    "FRONTEND_IMAGE",
    "KEYCLOAK_IMAGE",
    "MINIO_IMAGE",
    "POSTGRES_IMAGE",
    "REDIS_IMAGE",
    "REVERSE_PROXY_IMAGE",
)
EXACT_IMAGE = re.compile(r"^[A-Za-z0-9._-]+(?::[0-9]+)?(?:/[A-Za-z0-9._-]+)+@sha256:[a-f0-9]{64}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
SAFE_NETWORK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("Data environment file contains a non-assignment line")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ValueError("Data environment file contains an invalid or duplicate key")
        values[key] = value.strip().strip('"').strip("'")
    return values


def _require_exact_image(value: str, *, name: str) -> None:
    if not EXACT_IMAGE.fullmatch(value):
        raise ValueError(f"{name} must be an exact registry image @sha256 reference")


def validate_environment(environ: dict[str, str], data_env: dict[str, str]) -> None:
    for name in IMAGE_VARIABLES:
        _require_exact_image(environ.get(name, ""), name=name)
    network = environ.get("CUSTOMS_DATA_STORE_TLS_NETWORK", "")
    if not SAFE_NETWORK.fullmatch(network) or network in {"bridge", "host", "none"}:
        raise ValueError("CUSTOMS_DATA_STORE_TLS_NETWORK must name a dedicated external network")
    if environ.get("CUSTOMS_DATA_STORE_ACCESS_MODE") != "read_only":
        raise ValueError("Production Data-store access mode must be exactly read_only")
    if not SHA256.fullmatch(environ.get("CUSTOMS_DATA_STORE_PERMISSION_ATTESTATION_SHA256", "")):
        raise ValueError("a reviewed Data-store permission attestation SHA-256 is required")

    if data_env.get("POSTGRES_SSLMODE", "").casefold() not in {
        "require",
        "verify-ca",
        "verify-full",
    }:
        raise ValueError("Data PostgreSQL must require TLS")
    if urlsplit(data_env.get("QDRANT_URL", "")).scheme.casefold() != "https":
        raise ValueError("Data Qdrant endpoint must use HTTPS")
    if not (
        data_env.get("QDRANT_API_KEY")
        or data_env.get("EVIDENCE_QDRANT_API_KEY_FILE", "").startswith("/")
    ):
        raise ValueError("Data Qdrant requires an API key or absolute secret-file path")
    if urlsplit(data_env.get("NEO4J_URI", "")).scheme.casefold() not in {
        "bolt+s",
        "neo4j+s",
    }:
        raise ValueError("Data Neo4j endpoint must use an encrypted verified URI")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--data-env-file", type=Path, required=True)
    args = parser.parse_args()
    data_env = _parse_env_file(args.data_env_file)
    validate_environment(dict(os.environ), data_env)
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker executable is required")
    result = subprocess.run(  # nosec B603
        [docker, "compose", "-f", str(args.compose_file), "config", "--images"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    images = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not images:
        raise ValueError("rendered Compose configuration contains no images")
    for index, image in enumerate(images):
        _require_exact_image(image, name=f"rendered image {index}")
    print(f"production deployment inputs verified: {len(images)} exact images")


if __name__ == "__main__":
    main()
