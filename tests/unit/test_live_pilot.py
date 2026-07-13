from __future__ import annotations

import json

import tools.run_live_pilot as pilot


def degraded_health(_base_url: str, kind: str) -> tuple[int, dict]:
    if kind == "live":
        return 200, {"status": "alive"}
    return 503, {"detail": "law corpus incomplete"}


def test_release_preflight_rejects_incomplete_data_corpus(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pilot, "health", degraded_health)

    assert pilot.main(["--preflight-only"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "RELEASE_GATE"
    assert report["ready_http"] == 503


def test_targeted_diagnostic_is_explicitly_labeled_and_can_continue(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pilot, "health", degraded_health)

    assert pilot.main(["--preflight-only", "--allow-incomplete-corpus"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "TARGETED_DIAGNOSTIC"
    assert report["ready_http"] == 503


def test_release_preflight_accepts_ready_complete_data_platform(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        pilot,
        "health",
        lambda _base_url, kind: (200, {"status": "alive" if kind == "live" else "ready"}),
    )

    assert pilot.main(["--preflight-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "RELEASE_GATE"
    assert report["ready_http"] == 200
