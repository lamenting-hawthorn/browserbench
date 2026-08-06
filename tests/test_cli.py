"""CLI status and failure-summary behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_pilot


def test_failed_run_returns_nonzero_and_prints_failure_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        run_pilot.engine,
        "run_managed",
        lambda **_: {
            "status": "failure",
            "task": "msg_read_01",
            "outcome": None,
            "error": {
                "type": "RuntimeError",
                "message": "baseline failed",
                "stage": "baseline",
            },
            "receipt_path": str(tmp_path / "failed.json"),
        },
    )
    status = run_pilot.main(
        [
            "--task",
            "msg_read_01",
            "--baseline",
            "playwright-exact",
            "--receipt-dir",
            str(tmp_path),
        ]
    )
    assert status == 1
    output = capsys.readouterr().out
    assert '"status": "failure"' in output
    assert '"outcome": null' in output
    assert "baseline failed" in output


def test_summary_never_shapes_failure_as_success() -> None:
    summary = run_pilot._summarize(
        {
            "status": "failure",
            "task": "task",
            "outcome": None,
            "error": {"type": "ValueError", "message": "bad", "stage": "setup"},
            "receipt_path": None,
        },
        "run",
        "playwright-exact",
    )
    assert summary["status"] == "failure"
    assert summary["outcome"] is None
    assert summary["error"]["stage"] == "setup"
    json.dumps(summary)


def test_unknown_task_is_rejected_by_cli() -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_pilot.main(["--task", "../outside"])
    assert exc_info.value.code == 2


def test_browser_use_full_is_an_accepted_cli_baseline_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run_managed(*, baseline, **kwargs):
        del kwargs
        captured["baseline"] = baseline
        return {
            "status": "success",
            "task": "msg_read_01",
            "outcome": "read_only_ok",
            "claimed_send": False,
            "receipt_path": str(tmp_path / "ok.json"),
        }

    monkeypatch.setattr(run_pilot.engine, "run_managed", fake_run_managed)
    status = run_pilot.main(
        [
            "--task",
            "msg_read_01",
            "--baseline",
            "browser-use-full",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-chat",
            "--receipt-dir",
            str(tmp_path),
        ]
    )
    assert status == 0
    assert captured["baseline"] == "browser-use-full"


def test_browser_use_full_routes_through_external_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_external(*, baseline, **kwargs):
        del kwargs
        captured["baseline"] = baseline
        return {
            "status": "success",
            "task": "msg_read_01",
            "outcome": "read_only_ok",
            "claimed_send": False,
            "receipt_path": str(tmp_path / "external.json"),
        }

    def fake_verify_external_server(**_kwargs):
        return None

    monkeypatch.setattr(run_pilot.engine, "run_external", fake_run_external)
    monkeypatch.setattr(run_pilot.runtime, "verify_fixture_identity", fake_verify_external_server)
    status = run_pilot.main(
        [
            "--task",
            "msg_read_01",
            "--baseline",
            "browser-use-full",
            "--external-server",
            "--receipt-dir",
            str(tmp_path),
        ]
    )
    assert status == 0
    assert captured["baseline"] == "browser-use-full"
