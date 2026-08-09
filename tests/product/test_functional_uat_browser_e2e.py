from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import run_functional_uat_browser_e2e as browser_e2e


RUN_IDS = (
    "run_" + "a" * 32,
    "run_" + "b" * 32,
    "run_" + "c" * 32,
)
PROFILE = {
    "CUSTOMS_FUNCTIONAL_REVIEWER_USERNAME": "local-functional-reviewer",
    "CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD": "reviewer-local-value",
    "CUSTOMS_FUNCTIONAL_APPROVER_USERNAME": "local-functional-approver",
    "CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD": "approver-local-value",
}


def _receipt() -> dict[str, object]:
    return {
        "schema_version": "functional-uat-browser-e2e/v1",
        "status": "passed",
        "profile": "functional-uat",
        "base_url": "http://127.0.0.1:5173",
        "checks": sorted(browser_e2e._REQUIRED_CHECKS),
        "review_run_ids": list(RUN_IDS),
        "browser_api_origins": ["http://127.0.0.1:5173"],
    }


def _failure_receipt(*, first_failure: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": "functional-uat-browser-failure/v1",
        "status": "failed",
        "failure_count": 1,
        "first_failure": first_failure,
    }


def _prepare_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    profile_path = tmp_path / "backend.env"
    profile_path.write_text("ignored", encoding="utf-8")
    receipt_root = tmp_path / "e2e-runs"
    monkeypatch.setattr(browser_e2e, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(browser_e2e, "_validate_workspace_main", lambda _workspace: None)
    monkeypatch.setattr(browser_e2e, "_validate_frontend_command", lambda _frontend: None)
    monkeypatch.setattr(
        browser_e2e,
        "_validate_readiness",
        lambda *, backend_api_url, frontend_url: None,
    )
    monkeypatch.setattr(browser_e2e, "_profile_path", lambda _value: profile_path)
    monkeypatch.setattr(browser_e2e, "_receipt_root", lambda _value: receipt_root)
    monkeypatch.setattr(browser_e2e, "_load_profile", lambda _path: PROFILE)
    return profile_path, receipt_root


def test_receipt_accepts_only_the_safe_browser_summary_shape(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(_receipt()), encoding="utf-8")

    receipt = browser_e2e._load_receipt(path)

    assert receipt.checks == tuple(sorted(browser_e2e._REQUIRED_CHECKS))
    assert receipt.review_run_ids == RUN_IDS


def test_failure_receipt_accepts_only_the_strict_safe_failure_shape(tmp_path: Path) -> None:
    path = tmp_path / "failure.json"
    path.write_text(
        json.dumps(
            _failure_receipt(
                first_failure={
                    "file": "functional-uat-browser.spec.ts",
                    "line": 71,
                    "column": 9,
                }
            )
        ),
        encoding="utf-8",
    )

    receipt = browser_e2e._load_failure_receipt(path)

    assert receipt.failure_count == 1
    assert receipt.first_failure == browser_e2e.BrowserFailureLocation(
        file="functional-uat-browser.spec.ts", line=71, column=9
    )


def test_failure_receipt_allows_a_null_first_failure_without_detail(tmp_path: Path) -> None:
    path = tmp_path / "failure.json"
    path.write_text(json.dumps(_failure_receipt()), encoding="utf-8")

    receipt = browser_e2e._load_failure_receipt(path)

    assert receipt.failure_count == 1
    assert receipt.first_failure is None


def test_corepack_command_is_executable_on_windows_and_posix() -> None:
    assert browser_e2e._corepack_command("nt") == "corepack.cmd"
    assert browser_e2e._corepack_command("posix") == "corepack"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"raw_body": "not allowed"}),
        lambda payload: payload.update({"knowledge_conversation_id": "knowledge_conv_not_allowed"}),
        lambda payload: payload.update({"status": "failed"}),
        lambda payload: payload.update({"review_run_ids": [RUN_IDS[0], RUN_IDS[1]]}),
        lambda payload: payload.update({"review_run_ids": ["not-a-run", RUN_IDS[1], RUN_IDS[2]]}),
        lambda payload: payload.update({"browser_api_origins": ["http://example.test"]}),
        lambda payload: payload.update({"checks": ["case_created"]}),
    ],
)
def test_receipt_rejects_raw_or_incomplete_values(tmp_path: Path, mutate) -> None:
    payload = _receipt()
    mutate(payload)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(browser_e2e.BrowserE2eError, match="receipt_invalid"):
        browser_e2e._load_receipt(path)


def test_child_environment_is_narrow_and_keeps_only_declared_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        browser_e2e.os,
        "environ",
        {"ANTHROPIC_API_KEY": "must-not-reach-node", "Path": "C:\\safe-node-bin"},
    )
    child = browser_e2e._child_environment(
        profile=PROFILE,
        frontend_url="http://127.0.0.1:5173",
        receipt_path=tmp_path / "receipt.json",
        failure_path=tmp_path / "failure.json",
    )

    assert child["Path"] == "C:\\safe-node-bin"
    assert child["CUSTOMS_FUNCTIONAL_UAT_BROWSER_E2E"] == "1"
    assert child["CUSTOMS_FUNCTIONAL_UAT_BROWSER_REVIEWER_USERNAME"] == (
        "local-functional-reviewer"
    )
    assert child["CUSTOMS_FUNCTIONAL_UAT_BROWSER_APPROVER_USERNAME"] == (
        "local-functional-approver"
    )
    assert child["CUSTOMS_FUNCTIONAL_UAT_BROWSER_REVIEWER_PASSWORD"] == ("reviewer-local-value")
    assert child["CUSTOMS_FUNCTIONAL_UAT_BROWSER_APPROVER_PASSWORD"] == ("approver-local-value")
    assert child["CUSTOMS_FUNCTIONAL_UAT_SAFE_FAILURE_FILE"] == str(tmp_path / "failure.json")
    assert "ANTHROPIC_API_KEY" not in child
    assert not any(
        key.startswith("CUSTOMS_")
        and not key.startswith("CUSTOMS_FUNCTIONAL_UAT_BROWSER_")
        and key != "CUSTOMS_FUNCTIONAL_UAT_SAFE_FAILURE_FILE"
        for key in child
    )


def test_workspace_validation_requires_clean_main_product_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in browser_e2e._REPOSITORIES:
        (tmp_path / name / ".git").mkdir(parents=True)

    def clean_git(repository: Path, *arguments: str) -> str:
        assert repository.name in browser_e2e._REPOSITORIES
        if arguments[:2] == ("branch", "--show-current"):
            return "main"
        if arguments[:2] == ("status", "--porcelain=v1"):
            return ""
        assert arguments[0] == "rev-parse"
        return "a" * 40

    monkeypatch.setattr(browser_e2e, "_git_output", clean_git)
    browser_e2e._validate_workspace_main(tmp_path)

    def dirty_git(_repository: Path, *arguments: str) -> str:
        return "main" if arguments[0] == "branch" else " M src/file.py"

    monkeypatch.setattr(browser_e2e, "_git_output", dirty_git)
    with pytest.raises(browser_e2e.BrowserE2eError, match="workspace_dirty"):
        browser_e2e._validate_workspace_main(tmp_path)


def test_workspace_validation_requires_the_checked_out_origin_main_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in browser_e2e._REPOSITORIES:
        (tmp_path / name / ".git").mkdir(parents=True)

    def divergent_git(_repository: Path, *arguments: str) -> str:
        if arguments[:2] == ("branch", "--show-current"):
            return "main"
        if arguments[:2] == ("status", "--porcelain=v1"):
            return ""
        assert arguments[0] == "rev-parse"
        return "a" * 40 if arguments[-1] == "HEAD" else "b" * 40

    monkeypatch.setattr(browser_e2e, "_git_output", divergent_git)
    with pytest.raises(browser_e2e.BrowserE2eError, match="workspace_not_origin_main"):
        browser_e2e._validate_workspace_main(tmp_path)


def test_run_invokes_frontend_without_forwarding_output_or_unrelated_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-node")
    profile_path, receipt_root = _prepare_run(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_e2e,
        "_load_receipt",
        lambda _path: browser_e2e.BrowserReceipt(
            checks=tuple(sorted(browser_e2e._REQUIRED_CHECKS)), review_run_ids=RUN_IDS
        ),
    )
    monkeypatch.setattr(browser_e2e, "_verify_transcripts", lambda _receipt: 3)

    def fake_runner(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0)

    result = browser_e2e.run(
        profile_file=profile_path,
        receipt_root=receipt_root,
        runner=fake_runner,
    )

    assert result == {
        "status": "passed",
        "profile": "functional-uat",
        "checks_verified": len(browser_e2e._REQUIRED_CHECKS),
        "transcripts_verified": 3,
    }
    assert observed["arguments"] == (
        browser_e2e._corepack_command(),
        "npm",
        "run",
        "functional-uat:browser",
    )
    kwargs = observed["kwargs"]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 900
    environment = kwargs["env"]
    assert environment["CUSTOMS_FUNCTIONAL_UAT_BROWSER_E2E"] == "1"
    assert "ANTHROPIC_API_KEY" not in environment
    receipt_path = Path(environment["CUSTOMS_FUNCTIONAL_UAT_BROWSER_RECEIPT_PATH"])
    failure_path = Path(environment["CUSTOMS_FUNCTIONAL_UAT_SAFE_FAILURE_FILE"])
    assert receipt_path.is_absolute()
    assert failure_path == receipt_path.with_name("failure.json")
    assert failure_path.parent.name == receipt_path.parent.name
    assert not failure_path.exists()


def test_run_nonzero_uses_only_the_validated_safe_failure_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path, receipt_root = _prepare_run(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    def failed_runner(arguments, **kwargs):
        observed["kwargs"] = kwargs
        failure_path = Path(kwargs["env"]["CUSTOMS_FUNCTIONAL_UAT_SAFE_FAILURE_FILE"])
        failure_path.write_text(
            json.dumps(
                _failure_receipt(
                    first_failure={
                        "file": "functional-uat-browser.spec.ts",
                        "line": 71,
                        "column": 9,
                    }
                )
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 1, stdout="untrusted", stderr="untrusted")

    with pytest.raises(browser_e2e.BrowserE2eError, match="command_failed") as captured:
        browser_e2e.run(
            profile_file=profile_path,
            receipt_root=receipt_root,
            runner=failed_runner,
        )

    assert captured.value.first_failure == browser_e2e.BrowserFailureLocation(
        file="functional-uat-browser.spec.ts", line=71, column=9
    )
    assert observed["kwargs"]["stdout"] is subprocess.DEVNULL
    assert observed["kwargs"]["stderr"] is subprocess.DEVNULL


def test_run_nonzero_without_a_failure_receipt_uses_a_safe_missing_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path, receipt_root = _prepare_run(monkeypatch, tmp_path)

    def failed_runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 1, stdout="untrusted", stderr="untrusted")

    with pytest.raises(browser_e2e.BrowserE2eError, match="failure_receipt_missing"):
        browser_e2e.run(
            profile_file=profile_path,
            receipt_root=receipt_root,
            runner=failed_runner,
        )


def test_run_nonzero_with_an_invalid_failure_receipt_uses_a_safe_invalid_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path, receipt_root = _prepare_run(monkeypatch, tmp_path)

    def failed_runner(arguments, **kwargs):
        failure_path = Path(kwargs["env"]["CUSTOMS_FUNCTIONAL_UAT_SAFE_FAILURE_FILE"])
        failure_path.write_text(json.dumps({"unexpected": "not allowed"}), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 1, stdout="untrusted", stderr="untrusted")

    with pytest.raises(browser_e2e.BrowserE2eError, match="failure_receipt_invalid"):
        browser_e2e.run(
            profile_file=profile_path,
            receipt_root=receipt_root,
            runner=failed_runner,
        )


def test_main_emits_only_the_validated_relative_failure_file_and_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_kwargs):
        raise browser_e2e.BrowserE2eError(
            "functional_uat_browser_command_failed",
            first_failure=browser_e2e.BrowserFailureLocation(
                file="functional-uat-browser.spec.ts", line=71, column=9
            ),
        )

    monkeypatch.setattr(browser_e2e, "run", fail)

    assert browser_e2e.main([]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "functional_uat_browser_e2e": "failed",
        "reason": "functional_uat_browser_command_failed",
        "first_failure": {"file": "functional-uat-browser.spec.ts", "line": 71},
    }


def test_browser_receipt_requires_current_workbench_correctness_checks() -> None:
    assert {
        "korean_user_prose",
        "raw_filename_hidden",
        "regulatory_requirement_deduplicated",
        "data_retrieval_semantics",
        "assistant_mixed_sections",
        "assistant_case_application",
        "case_document_paste_notice",
        "knowledge_conversation",
        "knowledge_recent_history",
        "knowledge_official_provenance",
        "knowledge_default_boundary",
        "knowledge_v2_scope_coverage",
        "knowledge_multiscope_clarification",
        "export_document_registered",
        "export_partial_coverage",
        "export_human_review_required",
    } <= browser_e2e._REQUIRED_CHECKS
