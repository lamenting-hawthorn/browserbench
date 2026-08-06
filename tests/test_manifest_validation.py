"""Independent schema-v2 receipt validation and mutation tests."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from btb.app import db
from btb.harness import engine
from btb.harness import manifest
from btb.harness import validate_manifest
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner


def _baseline(task: dict) -> manifest.BaselineProvenance:
    baseline = engine._playwright_provenance(
        "exact",
        action_timeout_ms=float(task["budget"]["wall_s"]) * 1_000,
    )
    return replace(baseline, framework_version="1")


def _browser_baseline(task: dict) -> manifest.BaselineProvenance:
    baseline = engine._browser_use_provenance(
        engine.BrowserUseConfig(
            provider="deepseek",
            model="deepseek-chat",
            max_steps=task["budget"]["steps"],
            wall_s=float(task["budget"]["wall_s"]),
        )
    )
    return replace(baseline, framework_version="0.13.6")


def _browser_full_baseline(task: dict) -> manifest.BaselineProvenance:
    baseline = engine._browser_use_provenance(
        engine.BrowserUseConfig(
            provider="deepseek",
            model="deepseek-chat",
            max_steps=task["budget"]["steps"],
            wall_s=float(task["budget"]["wall_s"]),
            name="browser-use-full",
            excluded_actions=engine._BROWSER_USE_FULL_EXCLUDED_ACTIONS,
            allowed_actions=engine._BROWSER_USE_FULL_ALLOWED_ACTIONS,
            use_vision=True,
            use_judge=False,
            max_actions_per_step=8,
        )
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
            _browser_full_baseline(task)
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
