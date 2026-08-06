"""Receipt writer ownership, atomicity, and sanitization tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from btb.harness import manifest


def _baseline() -> manifest.BaselineProvenance:
    return manifest.BaselineProvenance(
        name="test-control",
        framework_name="pytest",
        framework_version="1",
        provider=None,
        model="deterministic",
        parameters={},
        modality_policy={"dom": True},
        capability_policy={"visible_page_controls_only": True},
    )


def _builder(tmp_path: Path, *, run_id: str = "receipt-test") -> manifest.ReceiptBuilder:
    return manifest.ReceiptBuilder(
        run_id=run_id,
        freeze="test-freeze",
        baseline=_baseline(),
        configured_steps=1,
        configured_wall_s=1,
        canonical_requested=False,
        task_definition={"id": "test-task"},
        prompt_text="exact prompt",
        source=manifest.SourceProvenance(
            git_commit="a" * 40,
            git_dirty=True,
            source_tree_sha256="b" * 64,
        ),
        out_dir=tmp_path,
    )


def test_failure_receipt_is_finalized_exactly_once(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    path = builder.write_failure(ValueError("failed"), stage="baseline")
    assert path == tmp_path / "receipt-test.json"
    assert builder.finalized_path == path
    with pytest.raises(RuntimeError, match="already been finalized"):
        builder.write_failure(ValueError("second"), stage="baseline")
    assert [item.name for item in tmp_path.iterdir()] == ["receipt-test.json"]


def test_atomic_receipt_and_trace_leave_no_temporary_files(tmp_path: Path) -> None:
    builder = _builder(tmp_path, run_id="atomic")
    builder.write_json_trace({"steps": ["one"]}, kind="test-trace")
    builder.write_failure(ValueError("failed"), stage="baseline")
    assert not list(tmp_path.glob(".*.tmp"))
    assert (tmp_path / "artifacts" / "atomic.test-trace.json").is_file()
    assert (tmp_path / "atomic.json").is_file()


def test_receipt_free_text_redacts_environment_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    builder = _builder(tmp_path, run_id="redacted")
    path = builder.write_failure(
        RuntimeError("Authorization: Bearer sk-test-secret-value"),
        stage="baseline",
    )
    payload = path.read_text(encoding="utf-8")
    assert "sk-test-secret-value" not in payload
    receipt = json.loads(payload)
    assert "<redacted" in receipt["execution"]["failure"]["message"]


def test_trace_redaction_is_bound_in_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret-value")
    builder = _builder(tmp_path, run_id="trace-redacted")
    metadata = builder.write_json_trace(
        {"error": "api_key=deepseek-secret-value"},
        kind="history",
    )
    assert metadata["redacted"] is True
    trace_text = (tmp_path / str(metadata["path"])).read_text(encoding="utf-8")
    assert "deepseek-secret-value" not in trace_text


def test_registered_runtime_secret_is_redacted_from_trace_and_failure(
    tmp_path: Path,
) -> None:
    token = "fixture-runtime-capability-token"
    builder = _builder(tmp_path, run_id="runtime-redacted")
    builder.register_sensitive_value(token)
    metadata = builder.write_json_trace({"token": token}, kind="history")
    receipt_path = builder.write_failure(
        RuntimeError(f"request used {token}"),
        stage="baseline",
    )

    assert metadata["redacted"] is True
    persisted = receipt_path.read_bytes() + (
        tmp_path / str(metadata["path"])
    ).read_bytes()
    assert token.encode() not in persisted
    assert b"<redacted:RUNTIME_SECRET>" in persisted


def test_receipt_free_text_redacts_local_home_path(tmp_path: Path) -> None:
    builder = _builder(tmp_path, run_id="path-redacted")
    path = builder.write_failure(
        RuntimeError(f"browser profile at {Path.home() / 'private-profile'}"),
        stage="baseline",
    )
    payload = path.read_text(encoding="utf-8")
    assert str(Path.home()) not in payload
    assert "$HOME/private-profile" in payload


def test_success_requires_complete_trace(tmp_path: Path) -> None:
    builder = _builder(tmp_path, run_id="missing-trace")
    builder.before_snapshot = {}
    builder.after_snapshot = {}
    builder.agent_claim = {}
    builder.injection_report = {"injection": "none"}
    builder.evaluation = {"headline_outcome": "test"}
    builder.outcome = "test"
    with pytest.raises(ValueError, match="complete execution trace"):
        builder.write_success()


@pytest.mark.parametrize("wall_s", [0, -1, True, "90"])
def test_receipt_builder_rejects_invalid_wall_budget(
    tmp_path: Path,
    wall_s: object,
) -> None:
    with pytest.raises(ValueError, match="configured_wall_s must be positive"):
        manifest.ReceiptBuilder(
            run_id="invalid-wall",
            freeze="test-freeze",
            baseline=_baseline(),
            configured_steps=1,
            configured_wall_s=wall_s,  # type: ignore[arg-type]
            canonical_requested=False,
            task_definition={"id": "test-task"},
            prompt_text="exact prompt",
            out_dir=tmp_path,
        )


@pytest.mark.parametrize("run_id", ["../escape", "nested/name", "", "bad name"])
def test_receipt_builder_rejects_unsafe_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id must contain only"):
        _builder(tmp_path, run_id=run_id)


def test_second_builder_cannot_overwrite_existing_receipt(tmp_path: Path) -> None:
    first = _builder(tmp_path, run_id="same-run")
    path = first.write_failure(ValueError("first"), stage="baseline")
    original = path.read_bytes()

    second = _builder(tmp_path, run_id="same-run")
    with pytest.raises(FileExistsError):
        second.write_failure(ValueError("second"), stage="baseline")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_playwright_trace_redacts_ui_token_everywhere(tmp_path: Path) -> None:
    token = "fixture-capability-token-value"
    path = tmp_path / "trace.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "trace.network",
            '{"name":"X-BTB-UI-Token","value":"' + token + '"}',
        )
        archive.writestr(
            "resources/page.html",
            '<script>const btbUiToken = "' + token + '";</script>',
        )
        archive.writestr("resources/image.bin", b"\x00\x01")

    assert manifest.redact_playwright_trace(path) is True
    with zipfile.ZipFile(path) as archive:
        contents = b"".join(archive.read(name) for name in archive.namelist())
    assert token.encode() not in contents
    assert b"<redacted:BTB_UI_TOKEN>" in contents


def test_source_hash_ignores_generated_schema_cache(tmp_path: Path) -> None:
    (tmp_path / "btb" / "schemas" / "__pycache__").mkdir(parents=True)
    (tmp_path / "btb" / "tasks" / "definitions").mkdir(parents=True)
    (tmp_path / "btb" / "app" / "templates").mkdir(parents=True)
    (tmp_path / "btb" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "btb" / "schemas" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "btb" / "schemas" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "btb" / "tasks" / "definitions" / "task.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (tmp_path / "btb" / "app" / "templates" / "index.html").write_text(
        "<html></html>\n", encoding="utf-8"
    )
    (tmp_path / "run_pilot.py").write_text("", encoding="utf-8")

    before = manifest.source_tree_sha256(tmp_path)
    (tmp_path / "btb" / "schemas" / "__pycache__" / "schema.pyc").write_bytes(
        b"generated"
    )
    assert manifest.source_tree_sha256(tmp_path) == before
