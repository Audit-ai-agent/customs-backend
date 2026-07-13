from __future__ import annotations

import json

import pytest

import tools.run_live_pilot as pilot


def degraded_health(_base_url: str, kind: str) -> tuple[int, dict]:
    if kind == "live":
        return 200, {"status": "alive"}
    return 503, {"detail": "law corpus incomplete"}


def complete_health(_base_url: str, kind: str) -> tuple[int, dict]:
    if kind == "live":
        return 200, {"status": "alive"}
    return 200, {
        "status": "ready",
        "knowledge_snapshot_id": "snapshot-1",
        "supported_review_as_of": "2026-07-10",
        "law_corpus": {"expected": 58, "loaded": 58},
        "law_index": {"expected": 1007, "postgres_indexed": 1007, "qdrant": 1007},
    }


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
    monkeypatch.setattr(pilot, "health", complete_health)

    assert pilot.main(["--preflight-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "RELEASE_GATE"
    assert report["ready_http"] == 200


@pytest.mark.parametrize(
    ("ready_code", "ready_body"),
    [
        (404, {"detail": "route missing"}),
        (500, {"detail": "database exception"}),
        (0, {"detail": "timeout"}),
        (503, {"detail": "unrecognized degraded state"}),
        (200, {"status": "degraded"}),
    ],
)
def test_diagnostic_rejects_unrelated_readiness_failures(
    monkeypatch, capsys, ready_code, ready_body
) -> None:
    monkeypatch.setattr(
        pilot,
        "health",
        lambda _base_url, kind: (
            (200, {"status": "alive"}) if kind == "live" else (ready_code, ready_body)
        ),
    )

    assert pilot.main(["--preflight-only", "--allow-incomplete-corpus"]) == 3
    capsys.readouterr()


def test_preflight_rejects_wrong_liveness_body(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        pilot,
        "health",
        lambda _base_url, kind: (
            (200, {"status": "wrong"}) if kind == "live" else complete_health("", kind)
        ),
    )

    assert pilot.main(["--preflight-only"]) == 2
    capsys.readouterr()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8088",
        "http://example.com:8088",
        "http://127.0.0.1:8088/path",
        "http://user:password@127.0.0.1:8088",
    ],
)
def test_preflight_rejects_non_loopback_or_credentialed_url(monkeypatch, capsys, url) -> None:
    called = False

    def unexpected_health(_base_url, _kind):
        nonlocal called
        called = True
        raise AssertionError("health must not be called")

    monkeypatch.setattr(pilot, "health", unexpected_health)
    assert pilot.main(["--preflight-only", "--rag-url", url]) == 4
    assert not called
    assert "password" not in capsys.readouterr().out
