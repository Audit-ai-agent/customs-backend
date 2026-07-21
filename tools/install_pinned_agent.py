"""Verify and install the exact AI Agent wheel declared by this repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil

# Fixed argument vectors only; shell execution is never used.
import subprocess  # nosec B404
import sys
import zipfile
from email.parser import Parser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "src" / "integration" / "agent_contract.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one .dist-info/METADATA file")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    return str(metadata["Name"]), str(metadata["Version"])


def verify_wheel(path: Path, config: dict[str, object]) -> tuple[str, str, str]:
    actual = file_sha256(path)
    expected = str(config["wheel_sha256"]).lower()
    if actual != expected:
        raise ValueError(f"AI wheel SHA-256 mismatch: expected {expected}, got {actual}")
    name, version = wheel_identity(path)
    if normalize_distribution(name) != normalize_distribution(str(config["distribution"])):
        raise ValueError(f"AI wheel distribution mismatch: got {name}")
    if version != str(config["version"]):
        raise ValueError(f"AI wheel version mismatch: expected {config['version']}, got {version}")
    return name, version, actual


def resolve_uv() -> str | None:
    candidate = shutil.which("uv") or os.environ.get("UV")
    return str(candidate) if candidate else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    config = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    try:
        wheel = args.wheel.expanduser().resolve(strict=True)
        _, version, actual = verify_wheel(wheel, config)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))

    if args.verify_only:
        print(
            "verified AI Agent wheel "
            f"version={version} declared_source_commit={config['source_commit']} sha256={actual}"
        )
        return 0

    uv = resolve_uv()
    if not uv:
        parser.error("uv executable is required to install the verified wheel")
    # The wheel bytes and identity were verified above; argv is fixed and shell=False.
    subprocess.run(  # nosec B603
        [uv, "pip", "install", "--no-deps", "--python", sys.executable, str(wheel)],
        cwd=ROOT,
        check=True,
    )
    # The executable was resolved explicitly and the argv has no caller-controlled option.
    subprocess.run(  # nosec B603
        [uv, "pip", "check", "--python", sys.executable], cwd=ROOT, check=True
    )
    print(
        "installed verified AI Agent wheel "
        f"version={config['version']} declared_source_commit={config['source_commit']} "
        f"sha256={actual}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
