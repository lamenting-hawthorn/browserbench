"""Independent schema-v2 receipt validation and mutation tests."""

from __future__ import annotations

import builtins
import copy
import hashlib
import importlib
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from btb.app import db
from btb.harness import browser_use_policy
from btb.harness import engine
from btb.harness import manifest
from btb.harness import validate_manifest
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner


_BROWSER_USE_SCHEMA_FIXTURE = (
    Path(__file__).parent / "fixtures" / "browser_use_0136_semantic_policy.json"
)


def _baseline(task: dict) -> manifest.BaselineProvenance:
    baseline = engine._playwright_provenance(
        "exact",
        action_timeout_ms=float(task["budget"]["wall_s"]) * 1_000,
    )
    return replace(baseline, framework_version="1")


def _fixture_schema_condition(
    config: engine.BrowserUseConfig,
) -> dict[str, object]:
    payload = json.loads(_BROWSER_USE_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    condition = browser_use_policy.schema_condition(
        config.name,
        config.provider,
        config.model,
    )
    conditions = payload.get("conditions")
    if not isinstance(conditions, dict):
        raise AssertionError("Browser Use schema fixture has no conditions")
    fixture = conditions.get(condition)
    if not isinstance(fixture, dict):
        raise AssertionError(f"Browser Use schema fixture lacks {condition}")
    if {
        "name": fixture.get("name"),
        "provider": fixture.get("provider"),
        "model": fixture.get("model"),
    } != {
        "name": config.name,
        "provider": config.provider,
        "model": config.model,
    }:
        raise AssertionError(f"Browser Use schema fixture metadata drifted for {condition}")
    return copy.deepcopy(fixture)


def _observed_browser_policy(config: engine.BrowserUseConfig) -> dict[str, object]:
    fixture = _fixture_schema_condition(config)
    fixture_actions = fixture.get("actions")
    action_model_schema = fixture.get("action_model_schema")
    if not isinstance(fixture_actions, list) or not isinstance(action_model_schema, dict):
        raise AssertionError("Browser Use schema fixture condition is malformed")
    names = tuple(config.allowed_actions)
    actions: list[dict[str, object]] = []
    for fixture_action in fixture_actions:
        if not isinstance(fixture_action, dict):
            raise AssertionError("Browser Use schema fixture action is malformed")
        name = fixture_action.get("name")
        parameter_schema = fixture_action.get("parameter_schema")
        if not isinstance(name, str) or not isinstance(parameter_schema, dict):
            raise AssertionError("Browser Use schema fixture action lacks schema data")
        action: dict[str, object] = {
            "name": name,
            "parameter_schema": parameter_schema,
            "parameter_schema_sha256": manifest.canonical_json_sha256(parameter_schema),
        }
        callback = browser_use_policy.FIXTURE_ACTION_CALLBACKS.get(name)
        if callback is not None:
            action["callback_identity"] = callback["identity"]
            action["callback_behavior"] = callback["behavior"]
        actions.append(action)
    if tuple(action["name"] for action in actions) != names:
        raise AssertionError("Browser Use schema fixture action names drifted")
    if config.name == "browser-use-full":
        screenshot = next(action for action in actions if action["name"] == "screenshot")
        screenshot["callback_identity"] = "btb.no_file_screenshot.v1"
        screenshot["callback_behavior"] = "request_next_observation_without_file_write"
    return {
        "status": "observed",
        "browser_use_version": "0.13.6",
        "settings": {
            "available_file_paths": [],
            "directly_open_url": True,
            "display_files_in_done_text": False,
            "generate_gif": False,
            "max_actions_per_step": (
                config.max_actions_per_step
                if config.max_actions_per_step is not None
                else 5
            ),
            "message_compaction": False,
            "save_conversation_path": None,
            "use_judge": False,
            "use_vision": config.use_vision,
        },
        "framework_paths": {
            "agent_directory": "sandbox_root_only",
            "file_system_base": "sandbox_root_only",
            "file_system_data": "sandbox_root_only",
            "screenshot_storage": "sandbox_root_only",
        },
        "llm_roles": {
            "primary": {"provider": config.provider, "model": config.model},
            "page_extraction": {
                "provider": config.provider,
                "model": config.model,
            },
            "judge": {
                "active": False,
                "provider": config.provider,
                "model": config.model,
            },
            "fallback": None,
        },
        "action_model_names": list(names),
        "action_model_schema": action_model_schema,
        "action_model_schema_sha256": manifest.canonical_json_sha256(
            action_model_schema
        ),
        "schema_condition": browser_use_policy.schema_condition(
            config.name,
            config.provider,
            config.model,
        ),
        "actions": actions,
    }


def _browser_baseline(task: dict) -> manifest.BaselineProvenance:
    config = engine.resolve_browser_use_config(
        task,
        provider="deepseek",
        model="deepseek-chat",
        max_steps=task["budget"]["steps"],
    )
    baseline = engine._browser_use_provenance(
        config,
        effective_policy=_observed_browser_policy(config),
    )
    return replace(baseline, framework_version="0.13.6")


def _browser_full_baseline(
    task: dict,
    *,
    provider: str = "openai",
    model: str = "gpt-4.1-mini",
) -> manifest.BaselineProvenance:
    config = engine.resolve_browser_use_config(
        task,
        provider=provider,
        model=model,
        max_steps=task["budget"]["steps"],
        name="browser-use-full",
    )
    baseline = engine._browser_use_provenance(
        config,
        effective_policy=_observed_browser_policy(config),
    )
    return replace(baseline, framework_version="0.13.6")


def _manifest_claim(claim: claim_mod.Claim) -> dict:
    result = claim.to_dict()
    result.pop("raw")
    result.update(
        {
            "detail": claim.raw,
            "detail_sha256": manifest.prompt_sha256(claim.raw),
            "detail_redacted": False,
        }
    )
    return result


def _send_report(message: dict) -> dict:
    request = {
        "sequence": 1,
        "method": "POST",
        "path": "/api/messages/send",
        "started_at": 1.0,
        "upstream_completed_at": 1.1,
        "upstream_status": 200,
        "treatment": "connection_dropped_after_commit",
        "finished_at": 1.2,
    }
    attempt = {
        "sequence": 1,
        "request_sequence": 1,
        "committed_sequence": 1,
        "method": "POST",
        "path": "/api/messages/send",
        "started_at": 1.0,
        "request_body_sha256": "a" * 64,
        "request_draft_id": message["draft_id"],
        "request_send_uid": message["send_uid"],
        "upstream_completed_at": 1.1,
        "upstream_status": 200,
        "upstream_classification": "committed",
        "committed_message_id": message["id"],
        "committed_send_uid": message["send_uid"],
        "treatment": "connection_dropped_after_commit",
        "finished_at": 1.2,
    }
    return {
        "injection": "disconnect_after_possible_send",
        "injection_enabled": True,
        "inject_after_committed": 1,
        "forwarded": True,
        "forwarded_status": 200,
        "upstream_statuses": [200],
        "aborted_response": True,
        "treatment_delivered": True,
        "send_forwards": 1,
        "in_flight": 0,
        "treatment_summary": {
            "none": 0,
            "response_forwarded": 0,
            "downstream_disconnected_before_response": 0,
            "connection_dropped_after_commit": 1,
        },
        "attempts": [attempt],
        "requests": [request],
    }


def _write_valid_receipt(
    tmp_path: Path,
    task_id: str,
    *,
    browser_use: bool = False,
    browser_use_full: bool = False,
    browser_use_full_provider: str = "openai",
    browser_use_full_model: str = "gpt-4.1-mini",
) -> Path:
    task = task_runner.load_definition(task_id)
    database_path = tmp_path / f"{task_id}.sqlite3"
    task_runner.prepare_initial_state(database_path, task=task)
    before = score_mod.snapshot(database_path)

    if task["effect_class"] == "read":
        expected = task["expected_report"]
        claim = claim_mod.claim_from_mapping(
            {
                "believes": "not_sent",
                "subject": expected["subject"],
                "body": expected["body"],
            }
        )
        report = {"injection": "none", "treatment_delivered": False}
    elif task["effect_class"] == "save":
        draft = db.create_draft(
            database_path,
            user_id=1,
            subject="Launch Notes",
            body="Ship tomorrow at 09:00.",
        )
        db.save_draft(database_path, draft_id=draft["id"])
        claim = claim_mod.claim_from_mapping({"believes": "not_sent"})
        report = {"injection": "none", "treatment_delivered": False}
    else:
        send = db.send_message(database_path, draft_id=1, send_uid="validator-uid")
        claim = claim_mod.claim_from_mapping({"believes": "sent"})
        report = _send_report(send["message"])

    after = score_mod.snapshot(database_path)
    evaluation = score_mod.evaluate(
        task,
        before,
        after,
        claim,
        injection_report=report,
    )
    builder = manifest.ReceiptBuilder(
        run_id=f"valid-{task_id}",
        freeze=task["freeze"],
        baseline=(
            _browser_full_baseline(
                task,
                provider=browser_use_full_provider,
                model=browser_use_full_model,
            )
            if browser_use_full
            else _browser_baseline(task)
            if browser_use
            else _baseline(task)
        ),
        configured_steps=task["budget"]["steps"] if (browser_use or browser_use_full) else None,
        configured_wall_s=task["budget"]["wall_s"],
        canonical_requested=False,
        task_definition=task,
        prompt_text=f"exact procedure for {task_id}",
        source=manifest.SourceProvenance(
            git_commit="a" * 40,
            git_dirty=True,
            source_tree_sha256="b" * 64,
        ),
        out_dir=tmp_path,
    )
    builder.before_snapshot = before.to_dict()
    builder.after_snapshot = after.to_dict()
    builder.agent_claim = _manifest_claim(claim)
    builder.injection_report = report
    builder.evaluation = evaluation.to_dict()
    builder.outcome = evaluation.headline_outcome
    if browser_use or browser_use_full:
        builder.write_json_trace({"steps": []}, kind="browser-use-history")
        builder.bind_framework_filesystem(
            state="cleaned",
            inventory={
                "version": 1,
                "entries": [],
                "entry_count": 0,
                "file_count": 0,
                "total_bytes": 0,
                "inventory_sha256": manifest.canonical_json_sha256({"entries": []}),
            },
        )
    else:
        artifact_directory = tmp_path / "artifacts"
        artifact_directory.mkdir(exist_ok=True)
        trace_path = artifact_directory / f"valid-{task_id}.playwright-trace.zip"
        with zipfile.ZipFile(trace_path, "w") as archive:
            archive.writestr("trace.trace", "{}")
        builder.bind_binary_trace(
            trace_path,
            kind="playwright",
            format_name="playwright-trace-zip",
            redacted=True,
        )
    return builder.write_success()


def _browser_use_receipt_for_schema_condition(
    tmp_path: Path,
    condition: str,
) -> Path:
    name, provider_model = condition.split(":", 1)
    provider, model = provider_model.split("/", 1)
    if name == "browser-use":
        assert (provider, model) == ("deepseek", "deepseek-chat")
        return _write_valid_receipt(tmp_path, "msg_send_01", browser_use=True)
    assert name == "browser-use-full"
    return _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
        browser_use_full_provider=provider,
        browser_use_full_model=model,
    )


def _browser_use_schema_action_cases() -> list[tuple[str, str]]:
    payload = json.loads(_BROWSER_USE_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    conditions = payload.get("conditions")
    if not isinstance(conditions, dict):
        raise AssertionError("Browser Use schema fixture has no conditions")
    cases: list[tuple[str, str]] = []
    for condition, fixture in sorted(conditions.items()):
        if not isinstance(condition, str) or not isinstance(fixture, dict):
            raise AssertionError("Browser Use schema fixture condition is malformed")
        actions = fixture.get("actions")
        if not isinstance(actions, list):
            raise AssertionError("Browser Use schema fixture has no action list")
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("name"), str):
                raise AssertionError("Browser Use schema fixture action is malformed")
            cases.append((condition, action["name"]))
    return cases


_BROWSER_USE_ACTION_SCHEMA_CASES = _browser_use_schema_action_cases()
_BROWSER_USE_ACTION_MODEL_SCHEMA_CONDITIONS = sorted(
    {condition for condition, _action_name in _BROWSER_USE_ACTION_SCHEMA_CASES}
)


def test_validator_frozen_schema_lookup_does_not_import_browser_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def reject_optional_browser_use(name, *args, **kwargs):
        if name == "browser_use" or name.startswith("browser_use."):
            raise AssertionError("independent validator imported optional browser_use")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_optional_browser_use)
    importlib.reload(validate_manifest)

    frozen = validate_manifest._frozen_browser_use_schema_digests(
        "browser-use-full",
        "openai",
        "gpt-4.1-mini",
    )
    assert frozen is not None
    assert frozen["action_model"] == browser_use_policy.schema_digests_for(
        "browser-use-full",
        "openai",
        "gpt-4.1-mini",
    )["action_model"]


def _mutated(path: Path, name: str, mutation) -> Path:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    mutation(receipt)
    target = path.with_name(f"{name}.json")
    target.write_text(json.dumps(receipt), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "task_id",
    ["msg_read_01", "msg_draft_save_01", "msg_send_01"],
)
def test_valid_receipts_pass_schema_and_independent_invariants(
    tmp_path: Path,
    task_id: str,
) -> None:
    path = _write_valid_receipt(tmp_path, task_id)
    assert validate_manifest.validate_file(path, source_repo=None) == []


def test_validator_does_not_count_post_retry_read_as_reconciliation() -> None:
    report = {
        "attempts": [
            {
                "request_sequence": 1,
                "treatment": "connection_dropped_after_commit",
            }
        ],
        "requests": [
            {"sequence": 1, "method": "POST", "path": "/api/messages/send"},
            {"sequence": 2, "method": "POST", "path": "/api/messages/send"},
            {
                "sequence": 3,
                "method": "GET",
                "path": "/api/messages",
                "upstream_status": 200,
            },
        ],
    }
    assert (
        validate_manifest._reconciliation_status(report, ambiguity_exposed=True)
        == "not_attempted"
    )


def test_rejects_task_and_prompt_hash_tampering(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    task_path = _mutated(
        path,
        "bad-task-hash",
        lambda receipt: receipt["task"].__setitem__("sha256", "0" * 64),
    )
    prompt_path = _mutated(
        path,
        "bad-prompt-hash",
        lambda receipt: receipt["prompt"].__setitem__("sha256", "0" * 64),
    )
    assert any(
        issue.path == "$.task.sha256"
        for issue in validate_manifest.validate_file(task_path, source_repo=None)
    )
    assert any(
        issue.path == "$.prompt.sha256"
        for issue in validate_manifest.validate_file(prompt_path, source_repo=None)
    )


def test_rejects_release_and_component_version_drift(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    release = _mutated(
        path,
        "bad-release",
        lambda receipt: receipt.__setitem__("release", "9.9.9"),
    )
    parser = _mutated(
        path,
        "bad-parser-version",
        lambda receipt: receipt["versions"].__setitem__("parser", "other-parser"),
    )
    assert any(
        issue.path == "$.release"
        for issue in validate_manifest.validate_file(release, source_repo=None)
    )
    assert any(
        issue.path == "$.versions.parser"
        for issue in validate_manifest.validate_file(parser, source_repo=None)
    )


def test_rejects_missing_snapshot_and_headline_mismatch(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    missing = _mutated(
        path,
        "missing-after",
        lambda receipt: receipt.__setitem__("after_snapshot", None),
    )
    mismatched = _mutated(
        path,
        "headline-mismatch",
        lambda receipt: receipt.__setitem__("outcome", "fabricated"),
    )
    assert any(
        issue.path == "$.after_snapshot"
        for issue in validate_manifest.validate_file(missing, source_repo=None)
    )
    assert any(
        issue.path == "$.evaluation.headline_outcome"
        for issue in validate_manifest.validate_file(mismatched, source_repo=None)
    )


def test_read_and_save_evaluations_are_reconstructed_independently(
    tmp_path: Path,
) -> None:
    read = _write_valid_receipt(tmp_path, "msg_read_01")
    bad_read = _mutated(
        read,
        "bad-read-report",
        lambda receipt: receipt["agent_claim"].__setitem__("subject", "wrong"),
    )
    save = _write_valid_receipt(tmp_path, "msg_draft_save_01")
    bad_save = _mutated(
        save,
        "bad-save-state",
        lambda receipt: receipt["after_snapshot"]["drafts"][0].__setitem__(
            "body", "wrong"
        ),
    )
    assert any(
        issue.path == "$.evaluation.functional_status"
        for issue in validate_manifest.validate_file(bad_read, source_repo=None)
    )
    assert any(
        issue.path == "$.evaluation.functional_status"
        for issue in validate_manifest.validate_file(bad_save, source_repo=None)
    )


def test_rejects_fake_treatment_non_quiescence_and_duplicate_effect_mismatch(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01")
    fake = _mutated(
        path,
        "fake-treatment",
        lambda receipt: receipt["injection_report"].__setitem__(
            "treatment_delivered", False
        ),
    )
    in_flight = _mutated(
        path,
        "in-flight",
        lambda receipt: receipt["injection_report"].__setitem__("in_flight", 1),
    )

    def duplicate_message(receipt: dict) -> None:
        second = copy.deepcopy(receipt["after_snapshot"]["messages"][0])
        second["id"] = 2
        second["send_uid"] = "fabricated-second"
        receipt["after_snapshot"]["messages"].append(second)

    duplicate = _mutated(path, "duplicate-effect", duplicate_message)
    assert any(
        issue.path == "$.injection_report.treatment_delivered"
        for issue in validate_manifest.validate_file(fake, source_repo=None)
    )
    assert any(
        issue.path == "$.injection_report.in_flight"
        for issue in validate_manifest.validate_file(in_flight, source_repo=None)
    )
    assert any(
        issue.path == "$.injection_report.attempts"
        for issue in validate_manifest.validate_file(duplicate, source_repo=None)
    )


def test_rejects_dirty_or_commitless_canonical_claim(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")

    def dirty(receipt: dict) -> None:
        receipt["canonical"] = True
        receipt["execution"]["requested_canonical"] = True

    dirty_path = _mutated(path, "dirty-canonical", dirty)

    def commitless(receipt: dict) -> None:
        receipt["canonical"] = True
        receipt["execution"]["requested_canonical"] = True
        receipt["source"]["git_dirty"] = False
        receipt["source"]["git_commit"] = None

    commitless_path = _mutated(path, "commitless-canonical", commitless)
    assert any(
        issue.path == "$.canonical"
        for issue in validate_manifest.validate_file(dirty_path, source_repo=None)
    )
    assert any(
        issue.path in {"$.canonical", "$.source.git_commit"}
        for issue in validate_manifest.validate_file(commitless_path, source_repo=None)
    )


def test_rejects_trace_digest_tampering(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    artifact = tmp_path / "artifacts" / "valid-msg_read_01.playwright-trace.zip"
    artifact.write_text("tampered", encoding="utf-8")
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(issue.path == "$.trace.sha256" for issue in issues)


def test_trace_reader_rejects_symlink_and_oversize_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    artifact = tmp_path / "artifacts" / "valid-msg_read_01.playwright-trace.zip"
    outside = tmp_path / "outside-trace.zip"
    outside.write_text("must not be read", encoding="utf-8")
    artifact.unlink()
    artifact.symlink_to(outside)

    monkeypatch.setattr(
        validate_manifest.os,
        "read",
        lambda *_args, **_kwargs: pytest.fail("unsafe trace artifact reached os.read"),
    )
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(issue.path == "$.trace.path" for issue in issues)

    artifact.unlink()
    with artifact.open("wb") as handle:
        handle.truncate(validate_manifest._MAX_TRACE_ARTIFACT_BYTES + 1)
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(issue.path == "$.trace.path" for issue in issues)


def test_rejects_matching_trace_metadata_with_raw_ui_token(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    artifact = tmp_path / "artifacts" / "valid-msg_read_01.playwright-trace.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "trace.trace",
            '{"name":"X-BTB-UI-Token","value":"raw-capability-token"}',
        )
    content = artifact.read_bytes()
    receipt["trace"]["sha256"] = hashlib.sha256(content).hexdigest()
    receipt["trace"]["size_bytes"] = len(content)
    path.write_text(json.dumps(receipt), encoding="utf-8")

    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(issue.path == "$.trace.redacted" for issue in issues)


def test_rejects_unsafe_run_and_task_identifiers(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")

    def make_unsafe(receipt: dict) -> None:
        receipt["run_id"] = "../outside"
        receipt["task"]["definition"]["id"] = "../../outside"

    unsafe = _mutated(path, "unsafe-identifiers", make_unsafe)
    issues = validate_manifest.validate_file(unsafe, source_repo=None)
    assert any(issue.path == "$.run_id" for issue in issues)
    assert any(issue.path == "$.task.definition.id" for issue in issues)


def test_invalid_commit_never_reaches_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_read_01")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["canonical"] = True
    receipt["execution"]["requested_canonical"] = True
    receipt["source"]["git_dirty"] = False
    receipt["source"]["git_commit"] = "--help"
    source_repo = tmp_path / "source"
    (source_repo / ".git").mkdir(parents=True)

    def unexpected_git(*_args, **_kwargs):
        pytest.fail("invalid commit reached Git")

    monkeypatch.setattr(validate_manifest, "_git_bytes", unexpected_git)
    issues = validate_manifest.validate_receipt(
        receipt,
        source_repo=source_repo,
    )
    assert any(issue.path == "$.source.git_commit" for issue in issues)


def test_rejects_claim_shape_and_projection_tampering(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01")

    def tamper(receipt: dict) -> None:
        receipt["agent_claim"]["raw"] = "must never persist"
        receipt["agent_claim"]["claimed_send"] = False
        receipt["agent_claim"]["sourced_from"] = "absent"
        receipt["agent_claim"]["detail_redacted"] = True

    mutated = _mutated(path, "claim-shape", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    paths = {issue.path for issue in issues}
    assert "$.agent_claim" in paths
    assert "$.agent_claim.claimed_send" in paths
    assert "$.agent_claim.sourced_from" in paths
    assert "$.agent_claim.detail_redacted" in paths


def test_rejects_inconsistent_injection_summary_and_commit_event(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01")

    def tamper(receipt: dict) -> None:
        report = receipt["injection_report"]
        report["send_forwards"] = 9
        report["upstream_statuses"] = [503]
        report["forwarded_status"] = 503
        report["aborted_response"] = False
        report["treatment_summary"]["connection_dropped_after_commit"] = 0
        report["attempts"][0]["request_draft_id"] = 2
        report["attempts"][0]["upstream_status"] = 500

    mutated = _mutated(path, "injection-summary", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    paths = {issue.path for issue in issues}
    assert "$.injection_report.send_forwards" in paths
    assert "$.injection_report.upstream_statuses" in paths
    assert "$.injection_report.forwarded_status" in paths
    assert "$.injection_report.aborted_response" in paths
    assert "$.injection_report.treatment_summary" in paths
    assert "$.injection_report.attempts[0].request_draft_id" in paths
    assert "$.injection_report.attempts[0].upstream_status" in paths


def test_validator_reconstructs_message_user_identity(tmp_path: Path) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01")
    mutated = _mutated(
        path,
        "message-user",
        lambda receipt: receipt["after_snapshot"]["messages"][0].__setitem__(
            "user_id", 2
        ),
    )
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    assert any(issue.path == "$.evaluation.functional_status" for issue in issues)


def test_browser_use_baseline_contract_validates_and_rejects_policy_drift(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01", browser_use=True)
    assert validate_manifest.validate_file(path, source_repo=None) == []

    def tamper(receipt: dict) -> None:
        receipt["baseline"]["parameters"]["llm_generation"]["provider_retries"] = 2
        receipt["baseline"]["capability_policy"]["direct_api"] = True

    mutated = _mutated(path, "browser-policy-drift", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    paths = {issue.path for issue in issues}
    assert "$.baseline.parameters" in paths
    assert "$.baseline.capability_policy" in paths


def test_browser_use_full_baseline_contract_validates(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
    )
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert issues == []


def test_browser_use_full_rejects_non_allowlisted_provider_model(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
    )

    def tamper(receipt: dict) -> None:
        receipt["baseline"]["provider"] = "deepseek"
        receipt["baseline"]["model"] = "deepseek-chat"

    mutated = _mutated(path, "non-allowlisted-model", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    assert "$.baseline.provider" in {issue.path for issue in issues}
    assert any("statically allowlisted" in issue.message for issue in issues)


def test_browser_use_full_rejects_rehashed_screenshot_schema_drift(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
    )

    def tamper(receipt: dict) -> None:
        policy = receipt["baseline"]["parameters"]["effective_policy"]
        screenshot = next(
            action for action in policy["actions"] if action["name"] == "screenshot"
        )
        screenshot["parameter_schema"]["properties"] = {
            "file_name": {"type": "string"}
        }

    mutated = _mutated(path, "rehashed-executable-schema-drift", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    assert any(
        issue.path == "$.baseline.parameters.effective_policy.actions[10]"
        and "no-path" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("callback_identity", "browser_use.built_in_navigate"),
    ],
)
def test_browser_use_rejects_rehashed_fixture_navigate_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01", browser_use=True)

    def tamper(receipt: dict) -> None:
        policy = receipt["baseline"]["parameters"]["effective_policy"]
        navigate = next(
            action for action in policy["actions"] if action["name"] == "navigate"
        )
        navigate[field] = value

    mutated = _mutated(path, f"rehashed-navigate-{field}", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    assert any(
        issue.path == "$.baseline.parameters.effective_policy.actions[7]"
        and "fixture-only" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("callback_identity", "browser_use.built_in_search"),
        ("callback_behavior", "dispatch_external_search_url"),
    ],
)
def test_browser_use_full_rejects_rehashed_fixture_search_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
    )

    def tamper(receipt: dict) -> None:
        policy = receipt["baseline"]["parameters"]["effective_policy"]
        search = next(
            action for action in policy["actions"] if action["name"] == "search"
        )
        search[field] = value

    mutated = _mutated(path, f"rehashed-search-{field}", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    action_index = list(engine._BROWSER_USE_FULL_ALLOWED_ACTIONS).index("search")
    assert any(
        issue.path == f"$.baseline.parameters.effective_policy.actions[{action_index}]"
        and "fixture-only" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize(
    ("condition", "action_name"),
    _BROWSER_USE_ACTION_SCHEMA_CASES,
)
def test_browser_use_rejects_rehashed_frozen_action_schema_drift(
    tmp_path: Path,
    condition: str,
    action_name: str,
) -> None:
    path = _browser_use_receipt_for_schema_condition(tmp_path, condition)

    def tamper(receipt: dict) -> None:
        policy = receipt["baseline"]["parameters"]["effective_policy"]
        action = next(
            candidate
            for candidate in policy["actions"]
            if candidate["name"] == action_name
        )
        action["parameter_schema"]["x-btb-schema-drift"] = True
        action["parameter_schema_sha256"] = manifest.canonical_json_sha256(
            action["parameter_schema"]
        )

    mutated = _mutated(path, f"rehashed-{action_name}-schema-drift", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    action_index = list(
        engine._BROWSER_USE_FULL_ALLOWED_ACTIONS
        if condition.startswith("browser-use-full:")
        else engine._BROWSER_USE_ALLOWED_ACTIONS
    ).index(action_name)
    assert any(
        issue.path
        == "$.baseline.parameters.effective_policy.actions"
        f"[{action_index}].parameter_schema"
        and "frozen Browser Use action schema" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize("condition", _BROWSER_USE_ACTION_MODEL_SCHEMA_CONDITIONS)
def test_browser_use_rejects_rehashed_complete_action_model_schema_drift(
    tmp_path: Path,
    condition: str,
) -> None:
    path = _browser_use_receipt_for_schema_condition(tmp_path, condition)

    def tamper(receipt: dict) -> None:
        policy = receipt["baseline"]["parameters"]["effective_policy"]
        policy["action_model_schema"]["x-btb-schema-drift"] = True
        policy["action_model_schema_sha256"] = manifest.canonical_json_sha256(
            policy["action_model_schema"]
        )

    mutated = _mutated(path, "rehashed-action-model-schema-drift", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    assert any(
        issue.path
        == "$.baseline.parameters.effective_policy.action_model_schema"
        and "frozen Browser Use ActionModel schema" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        ("use_vision", "$.baseline.parameters"),
        ("max_actions_per_step", "$.baseline.parameters"),
        ("excluded_actions", "$.baseline.parameters"),
        ("agent_tools_allowed", "$.baseline.capability_policy"),
    ],
)
def test_browser_use_full_baseline_rejects_policy_drift(
    tmp_path: Path,
    mutation: str,
    expected_path: str,
) -> None:
    path = _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
    )

    def tamper(receipt: dict) -> None:
        if mutation == "use_vision":
            receipt["baseline"]["parameters"]["use_vision"] = False
        elif mutation == "max_actions_per_step":
            receipt["baseline"]["parameters"]["max_actions_per_step"] = 3
        elif mutation == "excluded_actions":
            receipt["baseline"]["parameters"]["excluded_actions"] = [
                "evaluate",
                "write_file",
            ]
        else:
            receipt["baseline"]["capability_policy"]["enforcement"][
                "agent_tools_allowed"
            ] = ["click", "done"]

    mutated = _mutated(path, f"browser-full-drift-{mutation}", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    assert expected_path in {issue.path for issue in issues}


def test_browser_use_full_rejects_restricted_policy_as_drift(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(
        tmp_path,
        "msg_send_01",
        browser_use_full=True,
    )

    def tamper(receipt: dict) -> None:
        receipt["baseline"]["parameters"]["use_vision"] = False
        receipt["baseline"]["parameters"]["excluded_actions"] = list(
            engine._BROWSER_USE_EXCLUDED_ACTIONS
        )
        receipt["baseline"]["modality_policy"] = {"dom": True, "vision": False}
        receipt["baseline"]["capability_policy"]["enforcement"][
            "agent_tools_allowed"
        ] = list(engine._BROWSER_USE_ALLOWED_ACTIONS)

    mutated = _mutated(path, "browser-full-restricted-drift", tamper)
    issues = validate_manifest.validate_file(mutated, source_repo=None)
    paths = {issue.path for issue in issues}
    assert "$.baseline.parameters" in paths
    assert "$.baseline.modality_policy" in paths
    assert "$.baseline.capability_policy" in paths


def test_browser_use_inventory_artifact_rejects_tampering_and_unsafe_paths(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01", browser_use=True)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    metadata = receipt["framework_filesystem"]["inventory"]
    artifact = tmp_path / metadata["path"]
    artifact.write_text('{"entries":[]}', encoding="utf-8")
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(
        issue.path == "$.framework_filesystem.inventory.sha256" for issue in issues
    )

    unsafe = _mutated(
        path,
        "unsafe-sandbox-inventory",
        lambda value: value["framework_filesystem"]["inventory"].__setitem__(
            "path", "/private/sandbox.json"
        ),
    )
    issues = validate_manifest.validate_file(unsafe, source_repo=None)
    assert any(
        issue.path == "$.framework_filesystem.inventory.path" for issue in issues
    )


def test_browser_use_inventory_rejects_traversal_entries_and_symlink_artifacts(
    tmp_path: Path,
) -> None:
    path = _write_valid_receipt(tmp_path, "msg_send_01", browser_use=True)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    artifact = tmp_path / receipt["framework_filesystem"]["inventory"]["path"]
    inventory = json.loads(artifact.read_text(encoding="utf-8"))
    inventory["entries"].append({"path": "../escape", "type": "directory"})
    artifact.write_text(json.dumps(inventory), encoding="utf-8")
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(
        issue.path == "$.framework_filesystem.inventory.entries[0].path"
        for issue in issues
    )

    path = _write_valid_receipt(tmp_path, "msg_read_01", browser_use=True)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    artifact = tmp_path / receipt["framework_filesystem"]["inventory"]["path"]
    outside = tmp_path / "outside-inventory.json"
    outside.write_text("{}", encoding="utf-8")
    artifact.unlink()
    artifact.symlink_to(outside)
    issues = validate_manifest.validate_file(path, source_repo=None)
    assert any(
        issue.path == "$.framework_filesystem.inventory.path" for issue in issues
    )


def test_bounded_inventory_reader_rejects_link_and_oversize_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    artifact = artifacts / "inventory.json"
    outside = tmp_path / "outside-inventory.json"
    outside.write_text('{"outside":"must not be read"}', encoding="utf-8")
    artifact.symlink_to(outside)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("unsafe inventory artifact reached os.read")

    monkeypatch.setattr(validate_manifest.os, "read", unexpected_read)
    with pytest.raises(OSError, match="safe path component"):
        validate_manifest._read_bounded_artifact(tmp_path, "../outside-inventory.json")
    with pytest.raises(OSError, match="regular file"):
        validate_manifest._read_bounded_artifact(tmp_path, artifact.name)

    artifact.unlink()
    artifact.write_bytes(
        b"x" * (validate_manifest._MAX_SANDBOX_INVENTORY_ARTIFACT_BYTES + 1)
    )
    with pytest.raises(OSError, match="bounded size"):
        validate_manifest._read_bounded_artifact(tmp_path, artifact.name)

    artifact.write_text('{"entries":[]}', encoding="utf-8")
    replacement = tmp_path / "replacement-inventory.json"
    replacement.write_text('{"replacement":"must not be read"}', encoding="utf-8")
    original_open = validate_manifest.os.open

    def swap_after_lstat(name, flags, *args, **kwargs):
        if name == artifact.name and kwargs.get("dir_fd") is not None:
            os.replace(replacement, artifact)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(validate_manifest.os, "open", swap_after_lstat)
    with pytest.raises(OSError, match="changed while opening"):
        validate_manifest._read_bounded_artifact(tmp_path, artifact.name)
