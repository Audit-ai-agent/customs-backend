from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

import tools.run_live_pilot as pilot


def degraded_health(
    _base_url: str, kind: str, *, service_token: str | None = None
) -> tuple[int, dict]:
    del service_token
    if kind == "live":
        return 200, {"status": "alive"}
    return 503, {"detail": "law corpus incomplete"}


def complete_health(
    _base_url: str, kind: str, *, service_token: str | None = None
) -> tuple[int, dict]:
    del service_token
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
        lambda _base_url, kind, **_kwargs: (
            (200, {"status": "alive"}) if kind == "live" else (ready_code, ready_body)
        ),
    )

    assert pilot.main(["--preflight-only", "--allow-incomplete-corpus"]) == 3
    capsys.readouterr()


def test_preflight_rejects_wrong_liveness_body(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        pilot,
        "health",
        lambda _base_url, kind, **_kwargs: (
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

    def unexpected_health(_base_url, _kind, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("health must not be called")

    monkeypatch.setattr(pilot, "health", unexpected_health)
    assert pilot.main(["--preflight-only", "--rag-url", url]) == 4
    assert not called
    assert "password" not in capsys.readouterr().out


def test_pilot_uses_configurable_local_postgres_host_port(monkeypatch) -> None:
    monkeypatch.delenv("CUSTOMS_OPS_DSN", raising=False)
    monkeypatch.setenv("CUSTOMS_OPS_HOST_PORT", "55432")

    args = pilot.build_parser().parse_args([])

    assert args.ops_host_port == 55432
    assert args.ops_dsn is None
    assert pilot.local_ops_dsn(args.ops_host_port).endswith("@127.0.0.1:55432/trade_doc")


def test_pilot_uses_data_postgres_port_and_safe_default(monkeypatch) -> None:
    monkeypatch.delenv("CUSTOMS_OPS_DSN", raising=False)
    monkeypatch.delenv("CUSTOMS_OPS_HOST_PORT", raising=False)
    monkeypatch.setenv("POSTGRES_PORT", "60001")

    configured = pilot.build_parser().parse_args([])
    assert configured.ops_host_port == 60001

    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    defaulted = pilot.build_parser().parse_args([])
    assert defaulted.ops_host_port == 55432


def test_explicit_ops_dsn_takes_precedence_over_generated_local_dsn(monkeypatch) -> None:
    configured = "postgresql://user:secret@127.0.0.1:60000/operations"
    monkeypatch.setenv("CUSTOMS_OPS_DSN", configured)
    monkeypatch.setenv("CUSTOMS_OPS_HOST_PORT", "55432")

    args = pilot.build_parser().parse_args([])

    assert args.ops_dsn == configured
    assert args.ops_host_port == 55432


def test_review_date_prefers_explicit_value_then_readiness_snapshot_date() -> None:
    ready = {"supported_review_as_of": "2026-07-10"}

    assert pilot.resolve_review_as_of("2026-07-09", ready) == "2026-07-09"
    assert pilot.resolve_review_as_of(None, ready) == "2026-07-10"


def test_review_date_fails_when_neither_configuration_nor_readiness_provides_it() -> None:
    with pytest.raises(ValueError, match="review_as_of is required"):
        pilot.resolve_review_as_of(None, {"detail": "law corpus incomplete"})


def test_service_token_is_passed_to_health_but_never_printed(monkeypatch, capsys) -> None:
    secret = "never-print-this-token"
    observed: list[str | None] = []

    def token_aware_health(_base_url, kind, *, service_token=None):
        observed.append(service_token)
        return complete_health(_base_url, kind)

    monkeypatch.setenv("EVIDENCE_SERVICE_TOKEN", secret)
    monkeypatch.setattr(pilot, "health", token_aware_health)

    assert pilot.main(["--preflight-only"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert observed == [secret, secret]
    assert report["evidence_auth_configured"] is True
    assert report["llm_mode"] == "mock"
    assert secret not in output


def test_health_sends_token_in_header_not_url(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"alive"}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(pilot, "urlopen", fake_urlopen)

    code, payload = pilot.health("http://127.0.0.1:8010", "live", service_token="header-only-token")

    request = captured["request"]
    assert code == 200
    assert payload == {"status": "alive"}
    assert request.full_url == "http://127.0.0.1:8010/internal/rag/v1/health/live"
    assert request.get_header("X-demo-token") == "header-only-token"
    assert "header-only-token" not in request.full_url


def test_health_classifies_error_without_echoing_body_or_token(monkeypatch) -> None:
    token = "never-echo-this-token"

    def failed_urlopen(request, timeout):
        del request, timeout
        raise HTTPError(
            "http://127.0.0.1:8010/internal/rag/v1/health/ready",
            401,
            "Unauthorized",
            {},
            BytesIO(f'{{"detail":"{token}"}}'.encode()),
        )

    monkeypatch.setattr(pilot, "urlopen", failed_urlopen)

    code, payload = pilot.health(
        "http://127.0.0.1:8010",
        "ready",
        service_token=token,
    )

    assert code == 401
    assert payload == {"detail": "Evidence health HTTP 401"}
    assert token not in json.dumps(payload)
