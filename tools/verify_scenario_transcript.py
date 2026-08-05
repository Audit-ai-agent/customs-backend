"""Verify one local-only functional-UAT transcript without printing bodies."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from customs_backend.scenario_transcript import (
    ScenarioTranscriptError,
    verify_run,
    workspace_transcript_root,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="review-run ID to verify")
    parser.add_argument(
        "--transcript-root",
        type=Path,
        default=workspace_transcript_root(),
        help="workspace-local _local/state/scenario-runs only",
    )
    args = parser.parse_args(argv)
    try:
        result = verify_run(args.transcript_root, args.run_id)
    except ScenarioTranscriptError as exc:
        print(f"Transcript verification failed: {exc}")
        return 1
    streams = ", ".join(f"{name}={count}" for name, count in sorted(result["streams"].items()))
    print(
        "Transcript verified: "
        f"run={result['review_run_id']} profile={result['profile']} "
        f"status={result['status']} {streams}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
