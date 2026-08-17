"""Offline-only tests for the immutable repetition-study layer."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from functools import lru_cache
from pathlib import Path

import pytest

from btb.harness import (
    browser_use_sandbox,
    engine,
    manifest,
    repetition,
    repetition_summary,
    validate_manifest,
)
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner


def _condition(baseline: str = "playwright-exact") -> dict[str, object]:
    return {
        "baseline": baseline,
        "provider": None,
        "model": "deterministic-playwright",
        "max_steps": None,
    }


def _plan(*, repetitions: int = 2, study_wall_s: float = 300.0) -> repetition.StudyPlan:
    return repetition.build_plan(
        study_id="offline-repetition-test",
        seed="stable-test-seed",
        task_ids=["msg_read_01"],
        conditions=[_condition()],
        repetitions=repetitions,
        study_wall_s=study_wall_s,
        source_provenance=_canonical_source(),
    )


@lru_cache
def _canonical_source() -> manifest.SourceProvenance:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        cwd=manifest.REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = validate_manifest._commit_source_sha256(manifest.REPO_ROOT, commit)
    assert digest is not None
    return manifest.SourceProvenance(
        git_commit=commit,
        git_dirty=False,
        source_tree_sha256=digest,
    )


def _build_plan(**kwargs: object) -> repetition.StudyPlan:
    return repetition.build_plan(source_provenance=_canonical_source(), **kwargs)  # type: ignore[arg-type]


def _execute_plan(
    plan: repetition.StudyPlan | dict[str, object] | Path | str,
    **kwargs: object,
) -> repetition.ExecutionReport:
    return repetition.execute_plan(
        plan,
        source_provenance=_canonical_source(),
        **kwargs,
    )  # type: ignore[arg-type]


def _manifest_claim(claim: claim_mod.Claim) -> dict[str, object]:
    value = claim.to_dict()
    value.pop("raw")
    value.update(
        {
            "detail": claim.raw,
            "detail_sha256": manifest.prompt_sha256(claim.raw),
            "detail_redacted": False,
        }
    )
    return value


def _write_fixture_receipt(
    receipt_dir: Path,
    *,
    run_id: str,
    failure: bool = False,
    failure_status: str | None = None,
    prompt_suffix: str = "",
    baseline_name: str = "playwright-exact",
    canonical: bool = True,
    port: int = 43123,
) -> Path:
    """Write a generated valid schema-v2 fixture, not a benchmark run."""

    receipt_dir.mkdir(parents=True, exist_ok=True)
    task = task_runner.load_definition("msg_read_01")
    database_path = receipt_dir / f"{run_id}.sqlite3"
    task_runner.prepare_initial_state(database_path, task=task)
    before = score_mod.snapshot(database_path)
    expected = task["expected_report"]
    claim = claim_mod.claim_from_mapping(
        {
            "believes": "not_sent",
            "subject": expected["subject"],
            "body": expected["body"],
        }
    )
    after = score_mod.snapshot(database_path)
    database_path.unlink()
    report = {"injection": "none", "treatment_delivered": False}
    evaluation = score_mod.evaluate(task, before, after, claim, injection_report=report)
    behavior = "exact" if baseline_name == "playwright-exact" else "naive_retry"
    prompt = engine._augment_instruction(task, f"http://127.0.0.1:{port}") + prompt_suffix
    builder = manifest.ReceiptBuilder(
        run_id=run_id,
        freeze=task["freeze"],
        baseline=engine._playwright_provenance(
            behavior,
            action_timeout_ms=float(task["budget"]["wall_s"]) * 1_000,
        ),
        configured_steps=None,
        configured_wall_s=task["budget"]["wall_s"],
        canonical_requested=canonical,
        task_definition=task,
        prompt_text=prompt,
        source=(
            _canonical_source()
            if canonical
            else manifest.SourceProvenance(
                git_commit="a" * 40,
                git_dirty=True,
                source_tree_sha256="b" * 64,
            )
        ),
        out_dir=receipt_dir,
    )
    if failure:
        return builder.write_failure(
            RuntimeError("offline fixture baseline failure"),
            stage="baseline",
            status=failure_status,  # type: ignore[arg-type]
        )
    builder.before_snapshot = before.to_dict()
    builder.after_snapshot = after.to_dict()
    builder.agent_claim = _manifest_claim(claim)
    builder.injection_report = report
    builder.evaluation = evaluation.to_dict()
    builder.outcome = evaluation.headline_outcome
    artifacts = receipt_dir / "artifacts"
    artifacts.mkdir(exist_ok=True)
    trace_path = artifacts / f"{run_id}.playwright-trace.zip"
    with zipfile.ZipFile(trace_path, "w") as archive:
        archive.writestr("trace.trace", "{}")
    builder.bind_binary_trace(
        trace_path,
        kind="playwright",
        format_name="playwright-trace-zip",
        redacted=True,
    )
    return builder.write_success()


def _fixture_executor(
    *,
    failure_on_call: int | None = None,
    failure_status_on_call: str | None = None,
    prompt_suffix_on_call: int | None = None,
) -> tuple[object, list[str]]:
    calls: list[str] = []

    def execute(**kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["run_id"]))
        receipt_options = kwargs["receipt_options"]
        assert isinstance(receipt_options, engine.ReceiptOptions)
        assert receipt_options.out_dir is not None
        call_number = len(calls)
        _write_fixture_receipt(
            receipt_options.out_dir,
            run_id=str(kwargs["run_id"]),
            failure=call_number == failure_on_call,
            failure_status=failure_status_on_call,
            prompt_suffix=" drift" if call_number == prompt_suffix_on_call else "",
            port=43000 + call_number,
        )
        return {"status": "success"}

    return execute, calls


def test_plan_is_balanced_and_stable_across_input_order() -> None:
    first = _build_plan(
        study_id="matrix",
        seed=42,
        task_ids=["msg_send_01", "msg_read_01"],
        conditions=[_condition("playwright-naive"), _condition()],
        repetitions=3,
        study_wall_s=120,
    )
    second = _build_plan(
        study_id="matrix",
        seed=42,
        task_ids=["msg_read_01", "msg_send_01"],
        conditions=[_condition(), _condition("playwright-naive")],
        repetitions=3,
        study_wall_s=120.0,
    )
    assert first.to_dict() == second.to_dict()
    assert len(first.runs) == 2 * 2 * 3
    assert {run.ordinal for run in first.runs} == set(range(1, 13))
    assert len({run.run_id for run in first.runs}) == 12
    assert all(len(run.run_id) <= 192 for run in first.runs)
    assert all(run.run_id.startswith("btb-r1-") for run in first.runs)


def test_plan_persists_derived_framework_identity(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    condition = next(iter(plan.conditions.values()))
    path = tmp_path / "framework-plan.json"
    repetition.write_plan(path, plan)
    payload = path.read_bytes()
    assert condition.framework == "playwright"
    assert condition.framework_version
    assert f'"framework":"{condition.framework}"'.encode() in payload
    assert f'"framework_version":"{condition.framework_version}"'.encode() in payload
    assert repetition.load_plan(path).conditions[condition.key] == condition


def test_source_identity_changes_plan_and_run_ids() -> None:
    first = _plan(repetitions=1)
    source = _canonical_source()
    second = repetition.build_plan(
        study_id="offline-repetition-test",
        seed="stable-test-seed",
        task_ids=["msg_read_01"],
        conditions=[_condition()],
        repetitions=1,
        study_wall_s=300,
        source_provenance=manifest.SourceProvenance(
            git_commit="a" * 40,
            git_dirty=False,
            source_tree_sha256=source.source_tree_sha256,
        ),
    )
    assert first.source != second.source
    assert first.plan_sha256 != second.plan_sha256
    assert first.runs[0].run_id != second.runs[0].run_id


def test_runtime_identity_changes_plan_and_run_ids() -> None:
    first = _plan(repetitions=1)
    runtime = first.runtime
    second = repetition.build_plan(
        study_id="offline-repetition-test",
        seed="stable-test-seed",
        task_ids=["msg_read_01"],
        conditions=[_condition()],
        repetitions=1,
        study_wall_s=300,
        source_provenance=_canonical_source(),
        runtime_identity=repetition.RuntimeIdentity(
            algorithm=runtime.algorithm,
            package=runtime.package,
            tree_sha256="c" * 64,
        ),
    )
    assert first.runtime != second.runtime
    assert first.plan_sha256 != second.plan_sha256
    assert first.runs[0].run_id != second.runs[0].run_id


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.__setitem__("source", {}),
        lambda payload: payload["source"].__setitem__("git_dirty", True),
    ),
)
def test_plan_rejects_missing_or_noncanonical_source_identity(
    mutate: object,
) -> None:
    payload = _plan(repetitions=1).to_dict()
    assert callable(mutate)
    mutate(payload)
    with pytest.raises(repetition.PlanError, match="source"):
        repetition._validated_plan(payload)


def test_plan_rejects_tampered_runtime_identity() -> None:
    payload = _plan(repetitions=1).to_dict()
    payload["runtime"]["algorithm"] = "unknown"
    with pytest.raises(repetition.PlanError, match="runtime"):
        repetition._validated_plan(payload)


def test_plan_creation_refuses_dirty_default_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _canonical_source()
    monkeypatch.setattr(
        manifest,
        "source_provenance",
        lambda _root: manifest.SourceProvenance(
            git_commit=source.git_commit,
            git_dirty=True,
            source_tree_sha256=source.source_tree_sha256,
        ),
    )
    with pytest.raises(repetition.PlanError, match="source tree is dirty"):
        repetition.build_plan(
            study_id="dirty-source",
            seed="s",
            task_ids=["msg_read_01"],
            conditions=[_condition()],
            repetitions=1,
            study_wall_s=100,
        )


def test_framework_version_changes_condition_and_plan_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repetition, "_framework_identity", lambda _baseline: ("playwright", "1.2.3")
    )
    first = _plan(repetitions=1)
    monkeypatch.setattr(
        repetition, "_framework_identity", lambda _baseline: ("playwright", "1.2.4")
    )
    second = _plan(repetitions=1)
    assert first.plan_sha256 != second.plan_sha256
    assert first.runs[0].run_id != second.runs[0].run_id
    assert next(iter(first.conditions)) != next(iter(second.conditions))


def test_persisted_plan_requires_expanded_framework_fields(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    repetition.write_plan(path, _plan(repetitions=1))
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["conditions"][0]["framework_version"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(repetition.PlanError, match="plan condition is invalid"):
        repetition.load_plan(path)


def test_seed_changes_the_recorded_order_and_complete_run_identity() -> None:
    first = _plan(repetitions=4)
    second = _build_plan(
        study_id="offline-repetition-test",
        seed="another-seed",
        task_ids=["msg_read_01"],
        conditions=[_condition()],
        repetitions=4,
        study_wall_s=300,
    )
    assert first.plan_sha256 != second.plan_sha256
    assert [run.run_id for run in first.runs] != [run.run_id for run in second.runs]
    assert [run.random_order_sha256 for run in first.runs] != [
        run.random_order_sha256 for run in second.runs
    ]


@pytest.mark.parametrize(
    ("task_ids", "conditions"),
    [
        (["msg_read_01", "msg_read_01"], [_condition()]),
        (["msg_read_01"], [_condition(), _condition()]),
    ],
)
def test_plan_rejects_duplicate_matrix_members(
    task_ids: list[str], conditions: list[dict[str, object]]
) -> None:
    with pytest.raises(repetition.PlanError, match="unique"):
        _build_plan(
            study_id="duplicate-matrix",
            seed="s",
            task_ids=task_ids,
            conditions=conditions,
            repetitions=1,
            study_wall_s=100,
        )


@pytest.mark.parametrize(
    "condition",
    [
        {"baseline": "browser-use", "provider": None, "model": None, "max_steps": 5},
        {
            "baseline": "browser-use",
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "max_steps": 5,
        },
        {
            "baseline": "browser-use-full",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "max_steps": 5,
        },
        {
            "baseline": "playwright-exact",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "max_steps": 5,
        },
    ],
)
def test_plan_rejects_ambiguous_or_unapproved_condition(condition: dict[str, object]) -> None:
    with pytest.raises(repetition.PlanError):
        _build_plan(
            study_id="bad-condition",
            seed="s",
            task_ids=["msg_read_01"],
            conditions=[condition],
            repetitions=1,
            study_wall_s=10,
        )


def test_plan_write_is_atomic_idempotent_and_refuses_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "study.json"
    plan = _plan()
    assert repetition.write_plan(path, plan).to_dict() == plan.to_dict()
    assert repetition.write_plan(path, plan).to_dict() == plan.to_dict()
    different = _build_plan(
        study_id="offline-repetition-test",
        seed="different",
        task_ids=["msg_read_01"],
        conditions=[_condition()],
        repetitions=2,
        study_wall_s=30,
    )
    with pytest.raises(repetition.PlanError, match="refusing to overwrite"):
        repetition.write_plan(path, different)
    assert not list(tmp_path.glob(".*.tmp"))


def test_tampered_plan_fails_before_execution(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    repetition.write_plan(path, _plan())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["ordinal"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(repetition.PlanError, match="content/hash/order"):
        repetition.load_plan(path)


def test_execute_validates_receipts_and_resumes_without_duplicate_attempts(
    tmp_path: Path,
) -> None:
    plan = _plan()
    executor, calls = _fixture_executor()
    report = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=executor,
    )
    assert report.stopped_reason is None
    assert report.attempted_run_ids == tuple(calls)
    assert report.completed_run_ids == tuple(calls)
    assert not report.skipped_run_ids
    assert len(list(tmp_path.glob("*.json"))) == 2

    def should_not_execute(**_kwargs: object) -> None:
        raise AssertionError("completed receipt must be resumed, not retried")

    resumed = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=should_not_execute,
    )
    assert not resumed.attempted_run_ids
    assert resumed.skipped_run_ids == tuple(run.run_id for run in plan.runs)


def test_resume_rejects_wrong_id_and_duplicate_expected_receipts(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    path = tmp_path / f"{plan.runs[0].run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["run_id"] = "wrong-run-id"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(repetition.ReceiptError, match="filename/run_id mismatch"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=executor)

    clean = tmp_path / "clean"
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=clean, executor=executor)
    original = clean / f"{plan.runs[0].run_id}.json"
    (clean / "duplicate.json").write_bytes(original.read_bytes())
    with pytest.raises(repetition.ReceiptError, match="filename/run_id mismatch"):
        _execute_plan(plan, receipt_dir=clean, executor=executor)


def test_foreign_receipt_is_rejected_before_any_executor_call(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    (tmp_path / "foreign.json").write_text(
        json.dumps({"run_id": "foreign"}), encoding="utf-8"
    )
    executor, calls = _fixture_executor()
    with pytest.raises(repetition.ReceiptError, match="foreign receipt"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    assert not calls


def test_resume_rejects_receipt_framework_version_drift(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    path = tmp_path / f"{plan.runs[0].run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["baseline"]["framework"]["installed_version"] = "drift"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(repetition.ReceiptError, match="condition/budget differs"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=executor)


def test_execution_rejects_source_drift_before_resume_or_launch(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    source = _canonical_source()
    drifted = manifest.SourceProvenance(
        git_commit="b" * 40,
        git_dirty=False,
        source_tree_sha256=source.source_tree_sha256,
    )

    def should_not_execute(**_kwargs: object) -> None:
        raise AssertionError("source drift must fail before a resume can launch")

    with pytest.raises(repetition.StudyExecutionError, match="differs from the frozen plan"):
        repetition.execute_plan(
            plan,
            receipt_dir=tmp_path,
            executor=should_not_execute,
            source_provenance=drifted,
        )


def test_execution_rejects_runtime_drift_before_resume_or_launch(tmp_path: Path) -> None:
    current = _plan(repetitions=1)
    plan = repetition.build_plan(
        study_id="runtime-drift",
        seed="s",
        task_ids=["msg_read_01"],
        conditions=[_condition()],
        repetitions=1,
        study_wall_s=100,
        source_provenance=_canonical_source(),
        runtime_identity=repetition.RuntimeIdentity(
            algorithm=current.runtime.algorithm,
            package=current.runtime.package,
            tree_sha256="d" * 64,
        ),
    )
    with pytest.raises(repetition.StudyExecutionError, match="runtime differs"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])


def test_catastrophic_executor_creates_one_failure_receipt_then_fails_visibly(
    tmp_path: Path,
) -> None:
    plan = _plan(repetitions=1)

    def catastrophic(**_kwargs: object) -> None:
        raise RuntimeError("synthetic executor crash")

    report = _execute_plan(plan, receipt_dir=tmp_path, executor=catastrophic)
    assert report.completed_run_ids == (plan.runs[0].run_id,)
    receipts = list(tmp_path.glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["canonical"] is True
    assert receipt["status"] == "setup_error"
    assert receipt["execution"]["failure"]["stage"] == "repetition_executor_unhandled"


def test_outer_deadline_before_start_leaves_every_cell_unattempted(
    tmp_path: Path,
) -> None:
    plan = _plan(study_wall_s=1)
    executor, calls = _fixture_executor()
    report = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=executor,
        clock=lambda: 0.0,
    )
    assert report.stopped_reason == "study_wall_deadline_exhausted"
    assert not report.attempted_run_ids
    assert not calls
    assert len(report.unstarted_run_ids) == 2
    assert not list(tmp_path.glob("*.json"))


def test_outer_deadline_stops_mid_plan_when_next_task_budget_cannot_fit(tmp_path: Path) -> None:
    plan = _plan(study_wall_s=200)
    executor, calls = _fixture_executor()
    ticks = iter((0.0, 0.0, 0.0, 0.0, 150.0))

    def clock() -> float:
        return next(ticks, 150.0)

    report = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=executor,
        clock=clock,
    )
    assert report.stopped_reason == "study_wall_deadline_exhausted"
    assert report.attempted_run_ids == tuple(calls)
    assert len(calls) == 1
    assert len(report.unstarted_run_ids) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_outer_deadline_rechecks_immediately_before_engine_start(tmp_path: Path) -> None:
    plan = _plan(repetitions=1, study_wall_s=100)
    executor, calls = _fixture_executor()
    # Initial deadline, pre-setup check, then the exact pre-invocation check.
    ticks = iter((0.0, 0.0, 11.0))
    report = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=executor,
        clock=lambda: next(ticks),
    )
    assert report.stopped_reason == "study_wall_deadline_exhausted"
    assert not calls
    assert report.unstarted_run_ids == (plan.runs[0].run_id,)
    assert not list(tmp_path.glob("*.json"))


def test_resume_debits_validated_prior_receipt_duration_from_study_budget(
    tmp_path: Path,
) -> None:
    plan = _plan(study_wall_s=180)
    _write_fixture_receipt(tmp_path, run_id=plan.runs[0].run_id)
    first_path = tmp_path / f"{plan.runs[0].run_id}.json"
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    payload["duration_s"] = 100.0
    first_path.write_text(json.dumps(payload), encoding="utf-8")
    executor, calls = _fixture_executor()
    report = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=executor,
        clock=lambda: 0.0,
    )
    assert report.stopped_reason == "study_wall_deadline_exhausted"
    assert report.skipped_run_ids == (plan.runs[0].run_id,)
    assert report.unstarted_run_ids == (plan.runs[1].run_id,)
    assert not calls


def test_resume_rejects_nonfinite_receipt_duration(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    _write_fixture_receipt(tmp_path, run_id=plan.runs[0].run_id)
    path = tmp_path / f"{plan.runs[0].run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["duration_s"] = float("nan")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(repetition.ReceiptError, match="invalid duration_s"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])


def test_worker_environment_keeps_only_the_selected_provider_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    environment = repetition._worker_environment("deepseek")
    assert environment["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_parent_worker_timeout_kills_the_entire_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repetition,
        "_worker_command",
        lambda: [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "time.sleep(30)"
            ),
        ],
    )
    started = time.monotonic()
    result = repetition._run_worker_process({}, timeout_s=0.1, provider=None)
    assert result.timed_out is True
    assert result.process_group is not None
    assert time.monotonic() - started < 5
    assert browser_use_sandbox._process_group_exists(result.process_group) is False


def test_parent_timeout_receipts_once_and_never_starts_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(study_wall_s=200)
    monkeypatch.setattr(
        repetition,
        "_worker_command",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    observed: list[repetition._WorkerProcessResult] = []
    original = repetition._run_worker_process

    def capture(*args: object, **kwargs: object) -> repetition._WorkerProcessResult:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        observed.append(result)
        return result

    monkeypatch.setattr(repetition, "_run_worker_process", capture)
    report = repetition.execute_plan(
        plan,
        receipt_dir=tmp_path,
        source_provenance=_canonical_source(),
        _worker_timeout_cap_s=0.1,
    )
    assert report.stopped_reason == "study_wall_deadline_exhausted"
    assert report.attempted_run_ids == (plan.runs[0].run_id,)
    assert report.completed_run_ids == (plan.runs[0].run_id,)
    assert report.unstarted_run_ids == (plan.runs[1].run_id,)
    receipt = json.loads(
        (tmp_path / f"{plan.runs[0].run_id}.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "timeout"
    assert receipt["canonical"] is True
    assert observed and observed[0].process_group is not None
    assert browser_use_sandbox._process_group_exists(observed[0].process_group) is False


def test_worker_group_teardown_failure_is_receipted_as_setup_error_not_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(study_wall_s=200)
    monkeypatch.setattr(
        repetition,
        "_run_worker_process",
        lambda *_args, **_kwargs: repetition._WorkerProcessResult(
            timed_out=True,
            return_code=None,
            teardown_error=True,
            process_group=12345,
        ),
    )
    report = repetition.execute_plan(
        plan,
        receipt_dir=tmp_path,
        source_provenance=_canonical_source(),
    )
    assert report.stopped_reason == "worker_process_group_teardown_failed"
    assert report.attempted_run_ids == (plan.runs[0].run_id,)
    assert report.completed_run_ids == (plan.runs[0].run_id,)
    assert report.unstarted_run_ids == (plan.runs[1].run_id,)
    receipt = json.loads(
        (tmp_path / f"{plan.runs[0].run_id}.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "setup_error"
    assert receipt["execution"]["failure"]["stage"] == "worker_process_group_teardown"
    assert list(tmp_path.glob("*.json")) == [tmp_path / f"{plan.runs[0].run_id}.json"]


def test_staged_injected_success_is_discarded_when_the_outer_clock_overruns(
    tmp_path: Path,
) -> None:
    plan = _plan(repetitions=1, study_wall_s=100)
    staged_roots: list[Path] = []

    def executor(**kwargs: object) -> None:
        options = kwargs["receipt_options"]
        assert isinstance(options, engine.ReceiptOptions) and options.out_dir is not None
        assert options.out_dir != tmp_path
        assert not (tmp_path / f"{kwargs['run_id']}.json").exists()
        staged_roots.append(options.out_dir.parent)
        _write_fixture_receipt(options.out_dir, run_id=str(kwargs["run_id"]))

    ticks = iter((0.0, 0.0, 0.0, 0.0, 101.0))
    report = _execute_plan(
        plan,
        receipt_dir=tmp_path,
        executor=executor,
        clock=lambda: next(ticks, 101.0),
    )
    receipt = json.loads((tmp_path / f"{plan.runs[0].run_id}.json").read_text())
    assert report.stopped_reason == "study_wall_deadline_exhausted"
    assert receipt["status"] == "timeout"
    assert staged_roots and all(not root.exists() for root in staged_roots)


def test_staged_success_is_not_resumable_after_group_teardown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(study_wall_s=200)

    def staged_teardown(request: dict[str, object], **_kwargs: object) -> repetition._WorkerProcessResult:
        _write_fixture_receipt(Path(str(request["receipt_dir"])), run_id=str(request["run_id"]))
        return repetition._WorkerProcessResult(False, 0, True, 12345)

    monkeypatch.setattr(repetition, "_run_worker_process", staged_teardown)
    report = repetition.execute_plan(
        plan, receipt_dir=tmp_path, source_provenance=_canonical_source()
    )
    receipt = json.loads((tmp_path / f"{plan.runs[0].run_id}.json").read_text())
    assert report.stopped_reason == "worker_process_group_teardown_failed"
    assert receipt["status"] == "setup_error"
    assert receipt["execution"]["failure"]["stage"] == "worker_process_group_teardown"
    resumed = repetition.execute_plan(
        plan, receipt_dir=tmp_path, source_provenance=_canonical_source()
    )
    assert resumed.stopped_reason == "worker_process_group_teardown_failed"
    assert not resumed.attempted_run_ids
    assert resumed.unstarted_run_ids == (plan.runs[1].run_id,)


def test_parent_work_root_inventory_is_bounded_and_removed_without_a_provider() -> None:
    root = repetition._new_work_root(prefix="btb-repeat-test-")
    (root / "synthetic.txt").write_text("offline", encoding="utf-8")
    inventory, inventory_error, cleanup_error = repetition._cleanup_root(root)
    assert inventory_error is None
    assert cleanup_error is None
    assert inventory is not None and inventory["file_count"] == 1
    assert next(entry for entry in inventory["entries"] if entry["type"] == "regular_file")["sha256"]
    assert not root.exists()


def test_synthetic_learned_timeout_receipts_parent_work_root_cleanup(tmp_path: Path) -> None:
    task = task_runner.load_definition("msg_read_01")
    config = engine.BrowserUseConfig(
        provider="deepseek", model="deepseek-chat", max_steps=1, wall_s=task["budget"]["wall_s"]
    )
    builder = manifest.ReceiptBuilder(
        run_id="synthetic-learned-timeout",
        freeze=task["freeze"],
        baseline=engine._browser_use_provenance(config),
        configured_steps=1,
        configured_wall_s=task["budget"]["wall_s"],
        canonical_requested=True,
        task_definition=task,
        prompt_text=None,
        source=_canonical_source(),
        out_dir=tmp_path,
    )
    root = repetition._new_work_root(prefix="btb-repeat-learned-")
    (root / "synthetic-timeout.txt").write_text("offline", encoding="utf-8")
    inventory, inventory_error, cleanup_error = repetition._cleanup_root(root)
    assert inventory_error is None
    condition = repetition.Condition(
        "browser-use", "deepseek", "deepseek-chat", 1, "browser-use", "0.13.6"
    )
    repetition._write_parent_failure(
        builder,
        condition,
        directory=tmp_path,
        exc=TimeoutError("synthetic timeout"),
        stage="outer_study_deadline",
        status="timeout",
        inventory=inventory,
        inventory_error=inventory_error,
        cleanup_error=cleanup_error,
    )
    path = tmp_path / f"{builder.run_id}.json"
    receipt = json.loads(path.read_text())
    filesystem = receipt["framework_filesystem"]
    assert filesystem["state"] == "cleaned"
    assert filesystem["cleanup_verified"] is True
    assert filesystem["inventory"]["inventory_sha256"] == inventory["inventory_sha256"]
    assert (tmp_path / filesystem["inventory"]["path"]).is_file()
    assert not root.exists()


@pytest.mark.parametrize("name", ("foreign.txt", ".partial.tmp", "nested"))
def test_receipt_layout_rejects_noncanonical_top_level_entries(
    tmp_path: Path, name: str
) -> None:
    plan = _plan(repetitions=1)
    path = tmp_path / name
    if name == "nested":
        path.mkdir()
    else:
        path.write_text("blocked", encoding="utf-8")
    with pytest.raises(repetition.ReceiptError, match="unexpected receipt-directory entry"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])


def test_receipt_layout_rejects_symlink_receipt_and_artifacts(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    outside = tmp_path.parent / "outside-receipt.json"
    outside.write_text("{}", encoding="utf-8")
    os.symlink(outside, tmp_path / f"{plan.runs[0].run_id}.json")
    with pytest.raises(repetition.ReceiptError, match="unsafe"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])

    clean = tmp_path / "clean"
    _execute_plan(plan, receipt_dir=clean, executor=_fixture_executor()[0])
    artifact = next((clean / "artifacts").iterdir())
    artifact.unlink()
    os.symlink(outside, artifact)
    with pytest.raises(repetition.ReceiptError, match="artifact"):
        _execute_plan(plan, receipt_dir=clean, executor=_fixture_executor()[0])


def test_receipt_layout_rejects_orphan_artifact_and_summary_reuses_the_check(
    tmp_path: Path,
) -> None:
    plan = _plan(repetitions=1)
    _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])
    (tmp_path / "artifacts" / "orphan.bin").write_bytes(b"orphan")
    with pytest.raises(repetition.ReceiptError, match="orphaned"):
        _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])
    with pytest.raises(repetition.ReceiptError, match="orphaned"):
        repetition.aggregate_receipts(plan, receipt_dir=tmp_path)


def test_valid_partial_receipt_and_its_artifacts_resume_safely(tmp_path: Path) -> None:
    plan = _plan()
    _write_fixture_receipt(tmp_path, run_id=plan.runs[0].run_id)
    executor, calls = _fixture_executor()
    report = _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    assert report.skipped_run_ids == (plan.runs[0].run_id,)
    assert calls == [plan.runs[1].run_id]


def test_parent_publishes_a_first_success_into_a_new_nested_receipt_directory(
    tmp_path: Path,
) -> None:
    plan = _plan(repetitions=1)
    receipt_dir = tmp_path / "new" / "nested" / "receipts"
    assert not receipt_dir.exists()
    _execute_plan(plan, receipt_dir=receipt_dir, executor=_fixture_executor()[0])
    assert (receipt_dir / f"{plan.runs[0].run_id}.json").is_file()
    assert not [path for path in receipt_dir.rglob("*") if path.name.startswith(".")]


def test_receipt_directory_swap_during_publication_fails_closed_without_outside_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    receipt_dir = tmp_path / "receipts"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = repetition._prepare_publication

    def swap_after_prepare(
        stage: Path,
        directory: Path,
        *,
        run_id: str,
        receipt: dict[str, object],
        expected_directory: os.stat_result | None = None,
    ) -> repetition._Publication:
        publication = original(
            stage,
            directory,
            run_id=run_id,
            receipt=receipt,
            expected_directory=expected_directory,
        )
        moved = tmp_path / "validated-receipts"
        directory.rename(moved)
        os.symlink(outside, directory, target_is_directory=True)
        return publication

    monkeypatch.setattr(repetition, "_prepare_publication", swap_after_prepare)
    with pytest.raises(repetition.StudyExecutionError, match="sole terminal receipt"):
        _execute_plan(plan, receipt_dir=receipt_dir, executor=_fixture_executor()[0])
    assert receipt_dir.is_symlink()
    assert list(outside.iterdir()) == []
    moved = tmp_path / "validated-receipts"
    assert moved.is_dir()
    assert list(moved.rglob("*")) == [moved / "artifacts"]


def test_receipt_directory_replacement_during_publication_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    receipt_dir = tmp_path / "receipts"
    original = repetition._prepare_publication

    def replace_after_prepare(
        stage: Path,
        directory: Path,
        *,
        run_id: str,
        receipt: dict[str, object],
        expected_directory: os.stat_result | None = None,
    ) -> repetition._Publication:
        publication = original(
            stage,
            directory,
            run_id=run_id,
            receipt=receipt,
            expected_directory=expected_directory,
        )
        directory.rename(tmp_path / "validated-receipts")
        directory.mkdir()
        return publication

    monkeypatch.setattr(repetition, "_prepare_publication", replace_after_prepare)
    with pytest.raises(repetition.StudyExecutionError, match="sole terminal receipt"):
        _execute_plan(plan, receipt_dir=receipt_dir, executor=_fixture_executor()[0])
    assert list(receipt_dir.iterdir()) == []
    assert list((tmp_path / "validated-receipts").rglob("*")) == [
        tmp_path / "validated-receipts" / "artifacts"
    ]


def test_receipt_directory_replacement_before_publication_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    receipt_dir = tmp_path / "receipts"
    original = repetition._prepare_publication

    def replace_before_prepare(
        stage: Path,
        directory: Path,
        **kwargs: object,
    ) -> repetition._Publication:
        directory.rename(tmp_path / "validated-receipts")
        directory.mkdir()
        return original(stage, directory, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repetition, "_prepare_publication", replace_before_prepare)
    with pytest.raises(repetition.StudyExecutionError, match="sole terminal receipt"):
        _execute_plan(plan, receipt_dir=receipt_dir, executor=_fixture_executor()[0])
    assert list(receipt_dir.iterdir()) == []
    assert list((tmp_path / "validated-receipts").rglob("*")) == []


def test_parent_failure_publication_swap_stays_descriptor_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    receipt_dir = tmp_path / "receipts"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = repetition._bind_parent_filesystem_descriptor

    def swap_after_parent_open(
        builder: manifest.ReceiptBuilder,
        condition: repetition.Condition,
        directory: repetition._ReceiptDirectory,
        **kwargs: object,
    ) -> list[tuple[str, str, os.stat_result]]:
        result = original(builder, condition, directory, **kwargs)  # type: ignore[arg-type]
        directory.path.rename(tmp_path / "validated-receipts")
        os.symlink(outside, directory.path, target_is_directory=True)
        return result

    def failing_executor(**_kwargs: object) -> None:
        raise RuntimeError("synthetic executor failure")

    monkeypatch.setattr(
        repetition, "_bind_parent_filesystem_descriptor", swap_after_parent_open
    )
    with pytest.raises(repetition.StudyExecutionError, match="sole terminal receipt"):
        _execute_plan(plan, receipt_dir=receipt_dir, executor=failing_executor)
    assert receipt_dir.is_symlink()
    assert list(outside.iterdir()) == []
    assert list((tmp_path / "validated-receipts").rglob("*")) == []


def test_publication_rollback_does_not_remove_a_replaced_target_inode(
    tmp_path: Path,
) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    artifacts = held.artifacts(create=True)
    assert artifacts is not None
    target = receipt_dir / "artifacts" / "run.trace.zip"
    target.write_bytes(b"owned")
    expected = os.stat(target, follow_symlinks=False)
    target.unlink()
    target.write_bytes(b"foreign")
    repetition._unlink_owned(artifacts, target.name, expected)
    held.close()
    assert target.read_bytes() == b"foreign"


def test_payload_temp_rejects_replacement_before_identity_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    artifacts = held.artifacts(create=True)
    assert artifacts is not None
    original_stat = os.stat
    temporary_name: str | None = None
    swapped = False

    def replace_before_stat(path: str, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal temporary_name, swapped
        if not swapped and isinstance(path, str) and path.startswith(".btb-repeat-"):
            temporary_name = path
            descriptor = int(kwargs["dir_fd"])
            os.unlink(path, dir_fd=descriptor)
            foreign = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(repetition.os, "stat", replace_before_stat)
    try:
        with pytest.raises(repetition.ReceiptError, match="publication temporary changed"):
            repetition._write_bytes_to_temp(held, name="inventory.json", payload=b"owned")
    finally:
        held.close()
    assert swapped
    assert temporary_name is not None
    assert (receipt_dir / "artifacts" / temporary_name).read_bytes() == b"foreign"


def test_artifact_copy_rejects_replacement_before_identity_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "stage.bin"
    source.write_bytes(b"artifact")
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    original_stat = os.stat
    temporary_name: str | None = None
    swapped = False

    def replace_before_stat(path: str, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal temporary_name, swapped
        if not swapped and isinstance(path, str) and path.startswith(".btb-repeat-"):
            temporary_name = path
            descriptor = int(kwargs["dir_fd"])
            os.unlink(path, dir_fd=descriptor)
            foreign = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(repetition.os, "stat", replace_before_stat)
    try:
        with pytest.raises(repetition.ReceiptError, match="publication temporary changed"):
            repetition._copy_artifact_to_temp(
                source,
                held,
                name="stage.bin",
                digest=hashlib.sha256(b"artifact").hexdigest(),
                size=len(b"artifact"),
            )
    finally:
        held.close()
    assert swapped
    assert temporary_name is not None
    assert (receipt_dir / "artifacts" / temporary_name).read_bytes() == b"foreign"


def test_receipt_temp_rejects_replacement_before_identity_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    original_stat = os.stat
    temporary_name: str | None = None
    swapped = False

    def replace_before_stat(path: str, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal temporary_name, swapped
        if not swapped and isinstance(path, str) and path.startswith(".btb-repeat-"):
            temporary_name = path
            descriptor = int(kwargs["dir_fd"])
            os.unlink(path, dir_fd=descriptor)
            foreign = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(repetition.os, "stat", replace_before_stat)
    try:
        with pytest.raises(repetition.ReceiptError, match="publication temporary changed"):
            repetition._commit_publication(
                repetition._Publication(held, "run", b"{}\n", [])
            )
    finally:
        if not held.closed:
            held.close()
    assert swapped
    assert temporary_name is not None
    assert (receipt_dir / temporary_name).read_bytes() == b"foreign"


def test_receipt_target_replacement_after_link_preserves_foreign_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    original_link = os.link
    original_unlink = os.unlink
    swapped = False

    def link_then_replace(
        source: str, destination: str, *args: object, **kwargs: object
    ) -> None:
        nonlocal swapped
        original_link(source, destination, *args, **kwargs)
        if source.startswith(".btb-repeat-") and destination == "run.json":
            descriptor = int(kwargs["dst_dir_fd"])
            original_unlink(destination, dir_fd=descriptor)
            foreign = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True

    monkeypatch.setattr(repetition.os, "link", link_then_replace)
    try:
        with pytest.raises(repetition.ReceiptError, match="receipt target changed"):
            repetition._commit_publication(
                repetition._Publication(held, "run", b"{}\n", [])
            )
    finally:
        if not held.closed:
            held.close()
    assert swapped
    assert (receipt_dir / "run.json").read_bytes() == b"foreign"
    assert not [
        path
        for path in receipt_dir.iterdir()
        if path.name.startswith(".btb-repeat-")
    ]


def test_publication_rollback_preserves_replacement_after_quarantine_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    artifacts = held.artifacts(create=True)
    assert artifacts is not None
    target = receipt_dir / "artifacts" / "run.trace.zip"
    target.write_bytes(b"owned")
    expected = os.stat(target, follow_symlinks=False)
    original_rename = os.rename
    swapped = False

    def rename_then_replace(
        source: str, destination: str, *args: object, **kwargs: object
    ) -> None:
        nonlocal swapped
        original_rename(source, destination, *args, **kwargs)
        if source == target.name and destination.startswith(".btb-repeat-cleanup-"):
            descriptor = int(kwargs["dst_dir_fd"])
            foreign = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True

    monkeypatch.setattr(repetition.os, "rename", rename_then_replace)
    try:
        assert repetition._unlink_owned(artifacts, target.name, expected) is True
    finally:
        held.close()
    assert swapped
    assert target.read_bytes() == b"foreign"
    assert not list((receipt_dir / "artifacts").glob(".btb-repeat-cleanup-*.tmp"))


def test_publication_rollback_refuses_quarantine_replacement_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real publication directory is parent-owned mode 0700 and one-writer;
    # this deterministic swap proves the portable fail-closed boundary.
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    receipt_dir.chmod(0o700)
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    artifacts = held.artifacts(create=True)
    assert artifacts is not None
    target = receipt_dir / "artifacts" / "run.trace.zip"
    target.write_bytes(b"owned")
    expected = os.stat(target, follow_symlinks=False)
    original_stat = os.stat
    quarantine_stats = 0
    swapped = False

    def replace_before_quarantine_recheck(
        path: str, *args: object, **kwargs: object
    ) -> os.stat_result:
        nonlocal quarantine_stats, swapped
        if (
            not swapped
            and isinstance(path, str)
            and path.startswith(".btb-repeat-cleanup-")
        ):
            quarantine_stats += 1
            if quarantine_stats != 2:
                return original_stat(path, *args, **kwargs)
            descriptor = int(kwargs["dir_fd"])
            os.unlink(path, dir_fd=descriptor)
            foreign = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(repetition.os, "stat", replace_before_quarantine_recheck)
    try:
        assert repetition._unlink_owned(artifacts, target.name, expected) is False
    finally:
        held.close()
    assert swapped
    assert quarantine_stats == 2
    residue = list((receipt_dir / "artifacts").glob(".btb-repeat-cleanup-*.tmp"))
    assert len(residue) == 1
    assert residue[0].read_bytes() == b"foreign"
    with pytest.raises(repetition.ReceiptError, match="orphaned"):
        repetition.validated_receipts(
            _plan(repetitions=1),
            receipt_dir,
            manifest.REPO_ROOT,
            require_complete=False,
        )


def test_publication_rollback_removes_parent_owned_entry(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    held = repetition._open_receipt_directory(receipt_dir, create=False)
    artifacts = held.artifacts(create=True)
    assert artifacts is not None
    target = receipt_dir / "artifacts" / "owned.trace.zip"
    target.write_bytes(b"owned")
    expected = os.stat(target, follow_symlinks=False)
    try:
        assert repetition._unlink_owned(artifacts, target.name, expected) is True
    finally:
        held.close()
    assert not target.exists()


def test_parent_cleanup_failure_writes_one_control_setup_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    original = repetition._cleanup_root

    def cleanup_with_failure(root: Path):
        inventory, inventory_error, _cleanup_error = original(root)
        return inventory, inventory_error, RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(repetition, "_cleanup_root", cleanup_with_failure)
    report = _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])
    receipt = json.loads((tmp_path / f"{plan.runs[0].run_id}.json").read_text())
    assert report.stopped_reason == "parent_work_root_cleanup_failed"
    assert receipt["status"] == "setup_error"
    assert receipt["framework_filesystem"] is None
    assert receipt["execution"]["evidence_failures"][0]["stage"] == "parent_work_root_cleanup"


def test_parent_inventory_failure_blocks_success_and_is_receipted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    original = repetition._cleanup_root

    def cleanup_with_inventory_failure(root: Path):
        inventory, _inventory_error, cleanup_error = original(root)
        return inventory, RuntimeError("synthetic inventory failure"), cleanup_error

    monkeypatch.setattr(repetition, "_cleanup_root", cleanup_with_inventory_failure)
    report = _execute_plan(plan, receipt_dir=tmp_path, executor=_fixture_executor()[0])
    receipt = json.loads((tmp_path / f"{plan.runs[0].run_id}.json").read_text())
    assert report.stopped_reason == "parent_work_root_inventory_failed"
    assert receipt["status"] == "setup_error"
    assert receipt["execution"]["evidence_failures"][0]["stage"] == "parent_work_root_inventory"


def test_layout_validation_uses_descriptor_read_without_a_second_receipt_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    _write_fixture_receipt(tmp_path, run_id=plan.runs[0].run_id)
    monkeypatch.setattr(
        validate_manifest,
        "validate_file",
        lambda *_args, **_kwargs: pytest.fail("layout must not reopen receipt JSON"),
    )
    assert repetition.validated_receipts(
        plan, tmp_path, manifest.REPO_ROOT, require_complete=True
    )


def test_layout_validation_rejects_a_receipt_swapped_during_loaded_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    _write_fixture_receipt(tmp_path, run_id=plan.runs[0].run_id)
    receipt_path = tmp_path / f"{plan.runs[0].run_id}.json"
    outside = tmp_path.parent / "swapped-receipt.json"
    outside.write_text("{}", encoding="utf-8")
    original = validate_manifest.validate_loaded_receipt

    def swap_after_validation(receipt: object, **kwargs: object):
        issues = original(receipt, **kwargs)  # type: ignore[arg-type]
        receipt_path.unlink()
        os.symlink(outside, receipt_path)
        return issues

    monkeypatch.setattr(validate_manifest, "validate_loaded_receipt", swap_after_validation)
    with pytest.raises(repetition.ReceiptError, match="changed during validation"):
        repetition.validated_receipts(plan, tmp_path, manifest.REPO_ROOT, require_complete=True)


def test_aggregate_retains_failure_in_denominator_and_writes_stable_outputs(
    tmp_path: Path,
) -> None:
    plan = _plan()
    executor, _calls = _fixture_executor(failure_on_call=2)
    _execute_plan(plan, receipt_dir=tmp_path / "receipts", executor=executor)
    first = repetition.aggregate_study(
        plan,
        receipt_dir=tmp_path / "receipts",
        csv_path=tmp_path / "one.csv",
        markdown_path=tmp_path / "one.md",
    )
    second = repetition.aggregate_study(
        plan,
        receipt_dir=tmp_path / "receipts",
        csv_path=tmp_path / "two.csv",
        markdown_path=tmp_path / "two.md",
    )
    assert (tmp_path / "one.csv").read_bytes() == (tmp_path / "two.csv").read_bytes()
    assert (tmp_path / "one.md").read_bytes() == (tmp_path / "two.md").read_bytes()
    cell = first.data["cells"][0]
    assert cell["denominator"] == 2
    assert cell["failure_count"] == 1
    assert cell["status_counts"]["baseline_error"] == 1
    assert cell["effect_state_counts"]["not_evaluated"] == 1
    assert cell["metrics"]["run_success"]["count"] == 1
    assert cell["metrics"]["functional_pass"]["count"] == 1
    assert cell["metrics"]["strict_all_safety_pass"]["count"] == 1
    assert cell["metrics"]["authorization_violation_present"]["count"] == 0
    assert cell["metrics"]["duplicate_attempt_present"]["count"] == 0
    assert cell["condition"] == {
        "baseline": "playwright-exact",
        "framework": "playwright",
        "framework_version": next(iter(plan.conditions.values())).framework_version,
        "provider": None,
        "model": "deterministic-playwright",
        "max_steps": None,
    }
    assert first.data["plan_source"] == plan.source.to_dict()
    assert first.data["plan_runtime"] == plan.runtime.to_dict()
    # The managed loopback port is allocated separately for each fixture run.
    # Literal hashes remain auditable even though compatibility normalization
    # permits that one approved runtime difference.
    assert len(cell["prompt_compatibility"]["literal_prompt_sha256_set"]) == 2
    assert cell["metrics"]["run_success"]["wilson"] == {
        "method": repetition.WILSON_METHOD,
        "confidence_level": "0.95",
        "z": "1.959963984540054",
        "continuity_correction": False,
        "rounding_decimal_places": 6,
        "lower": "0.094531",
        "upper": "0.905469",
    }
    assert second.data == first.data
    assert "Wilson score" in (tmp_path / "one.md").read_text(encoding="utf-8")
    assert "Plan source:" in (tmp_path / "one.md").read_text(encoding="utf-8")
    assert "Summary SHA-256:" in (tmp_path / "one.md").read_text(encoding="utf-8")
    assert repetition_summary.accepted_pair_sha256(
        tmp_path / "one.csv", tmp_path / "one.md"
    ) == first.data["summary_sha256"]
    assert "baseline,framework,framework_version,provider,model,max_steps" in (tmp_path / "one.csv").read_text(
        encoding="utf-8"
    )


def test_timeout_receipt_is_retained_as_a_denominator_bearing_failure(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor(
        failure_on_call=1,
        failure_status_on_call="timeout",
    )
    _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    cell = repetition.aggregate_receipts(plan, receipt_dir=tmp_path).data["cells"][0]
    assert cell["denominator"] == 1
    assert cell["timeout_count"] == 1
    assert cell["failure_count"] == 1
    assert all(cell["metrics"][name]["count"] == 0 for name in cell["metrics"])


def test_summary_second_publish_failure_leaves_no_fresh_accepted_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path / "receipts", executor=executor)
    result = repetition.aggregate_receipts(plan, receipt_dir=tmp_path / "receipts")
    csv_path, markdown_path = tmp_path / "summary.csv", tmp_path / "summary.md"
    original = repetition_summary._replace
    failed = False

    def fail_after_markdown(path: Path, payload: bytes) -> None:
        nonlocal failed
        original(path, payload)
        if path == markdown_path and not failed:
            failed = True
            raise OSError("synthetic second publish failure")

    monkeypatch.setattr(repetition_summary, "_replace", fail_after_markdown)
    with pytest.raises(OSError, match="second publish"):
        repetition_summary.write_summaries(
            result, csv_path=csv_path, markdown_path=markdown_path
        )
    assert not csv_path.exists()
    assert not markdown_path.exists()
    assert repetition_summary.accepted_pair_sha256(csv_path, markdown_path) is None


def test_summary_second_publish_failure_restores_the_existing_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path / "receipts", executor=executor)
    original_result = repetition.aggregate_receipts(plan, receipt_dir=tmp_path / "receipts")
    csv_path, markdown_path = tmp_path / "summary.csv", tmp_path / "summary.md"
    repetition_summary.write_summaries(
        original_result, csv_path=csv_path, markdown_path=markdown_path
    )
    prior_csv, prior_markdown = csv_path.read_bytes(), markdown_path.read_bytes()
    changed = copy.deepcopy(original_result.data)
    changed["study_id"] = "changed-summary"
    changed.pop("summary_sha256")
    changed["summary_sha256"] = repetition_summary._json_hash(changed)
    changed_result = repetition_summary.AggregationResult(changed)
    original = repetition_summary._replace
    failed = False

    def fail_after_markdown(path: Path, payload: bytes) -> None:
        nonlocal failed
        original(path, payload)
        if path == markdown_path and not failed:
            failed = True
            raise OSError("synthetic second publish failure")

    monkeypatch.setattr(repetition_summary, "_replace", fail_after_markdown)
    with pytest.raises(OSError, match="second publish"):
        repetition_summary.write_summaries(
            changed_result, csv_path=csv_path, markdown_path=markdown_path
        )
    assert csv_path.read_bytes() == prior_csv
    assert markdown_path.read_bytes() == prior_markdown
    assert repetition_summary.accepted_pair_sha256(csv_path, markdown_path) == original_result.data[
        "summary_sha256"
    ]


def test_summary_backup_failure_restores_the_first_renamed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path / "receipts", executor=executor)
    result = repetition.aggregate_receipts(plan, receipt_dir=tmp_path / "receipts")
    csv_path, markdown_path = tmp_path / "summary.csv", tmp_path / "summary.md"
    repetition_summary.write_summaries(result, csv_path=csv_path, markdown_path=markdown_path)
    prior_csv, prior_markdown = csv_path.read_bytes(), markdown_path.read_bytes()
    original = repetition_summary._move_to_backup

    def fail_before_markdown_backup(path: Path) -> Path | None:
        if path == markdown_path:
            raise OSError("synthetic second backup failure")
        return original(path)

    monkeypatch.setattr(repetition_summary, "_move_to_backup", fail_before_markdown_backup)
    with pytest.raises(OSError, match="second backup"):
        repetition_summary.write_summaries(result, csv_path=csv_path, markdown_path=markdown_path)
    assert csv_path.read_bytes() == prior_csv
    assert markdown_path.read_bytes() == prior_markdown
    assert repetition_summary.accepted_pair_sha256(csv_path, markdown_path) == result.data[
        "summary_sha256"
    ]


def test_summary_refuses_an_interrupted_backup_transaction(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path / "receipts", executor=executor)
    result = repetition.aggregate_receipts(plan, receipt_dir=tmp_path / "receipts")
    csv_path, markdown_path = tmp_path / "summary.csv", tmp_path / "summary.md"
    interrupted = tmp_path / ".summary.csv.btb-summary-interrupted.bak"
    interrupted.write_text("prior partial generation", encoding="utf-8")
    with pytest.raises(repetition.ReceiptError, match="interrupted summary publication"):
        repetition_summary.write_summaries(
            result, csv_path=csv_path, markdown_path=markdown_path
        )
    assert repetition_summary.accepted_pair_sha256(csv_path, markdown_path) is None


def test_aggregation_rejects_prompt_template_drift_and_extra_receipts(tmp_path: Path) -> None:
    plan = _plan()
    executor, _calls = _fixture_executor(prompt_suffix_on_call=2)
    _execute_plan(plan, receipt_dir=tmp_path / "receipts", executor=executor)
    with pytest.raises(repetition.ReceiptError, match="prompt.*drift"):
        repetition.aggregate_receipts(plan, receipt_dir=tmp_path / "receipts")

    clean = tmp_path / "clean"
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=clean, executor=executor)
    extra = clean / "foreign.json"
    extra.write_text(json.dumps({"run_id": "foreign"}), encoding="utf-8")
    with pytest.raises(repetition.ReceiptError, match="extras"):
        repetition.aggregate_receipts(plan, receipt_dir=clean)


@pytest.mark.parametrize(
    "suffix",
    [
        " unexpected text",
        " Open http://127.0.0.1:44444/ in your browser.",
    ],
)
def test_aggregation_rejects_any_prompt_drift_even_if_uniform(
    tmp_path: Path, suffix: str
) -> None:
    plan = _plan(repetitions=1)
    def execute(**kwargs: object) -> dict[str, object]:
        receipt_options = kwargs["receipt_options"]
        assert isinstance(receipt_options, engine.ReceiptOptions)
        assert receipt_options.out_dir is not None
        _write_fixture_receipt(
            receipt_options.out_dir,
            run_id=str(kwargs["run_id"]),
            prompt_suffix=suffix,
        )
        return {"status": "success"}

    _execute_plan(plan, receipt_dir=tmp_path, executor=execute)
    with pytest.raises(repetition.ReceiptError, match="prompt.*drift|managed-loopback"):
        repetition.aggregate_receipts(plan, receipt_dir=tmp_path)


def test_aggregation_rejects_missing_receipts(tmp_path: Path) -> None:
    plan = _plan()
    _write_fixture_receipt(tmp_path, run_id=plan.runs[0].run_id)
    with pytest.raises(repetition.ReceiptError, match="missing"):
        repetition.aggregate_receipts(plan, receipt_dir=tmp_path)


def test_aggregation_rejects_noncanonical_receipt_even_when_schema_valid(tmp_path: Path) -> None:
    plan = _plan(repetitions=1)
    run = plan.runs[0]
    _write_fixture_receipt(tmp_path, run_id=run.run_id, canonical=False)
    with pytest.raises(repetition.ReceiptError, match="exploratory/noncanonical"):
        repetition.aggregate_receipts(plan, receipt_dir=tmp_path)


def test_wilson_interval_is_declared_and_deterministically_rounded() -> None:
    assert repetition.wilson_interval(0, 10)["lower"] == "0.000000"
    assert repetition.wilson_interval(0, 10)["upper"] == "0.277533"
    assert repetition.wilson_interval(10, 10)["lower"] == "0.722467"
    assert repetition.wilson_interval(10, 10)["upper"] == "1.000000"
    with pytest.raises(ValueError, match="total > 0"):
        repetition.wilson_interval(0, 0)


@pytest.mark.parametrize(
    "path",
    [
        ("source", "source_tree_sha256"),
        ("task", "sha256"),
        ("baseline", "parameters"),
        ("execution", "configured_wall_s"),
        ("versions", "evaluator"),
    ],
)
def test_cell_refuses_each_provenance_axis_drift(
    tmp_path: Path, path: tuple[str, str]
) -> None:
    plan = _plan()
    executor, _calls = _fixture_executor()
    _execute_plan(plan, receipt_dir=tmp_path, executor=executor)
    receipts = [
        json.loads((tmp_path / f"{run.run_id}.json").read_text(encoding="utf-8"))
        for run in plan.runs
    ]
    changed = copy.deepcopy(receipts)
    parent, child = path
    if parent == "baseline":
        changed[1][parent][child]["drift"] = True
    elif parent == "execution":
        changed[1][parent][child] = 999
    else:
        changed[1][parent][child] = "f" * 64 if child.endswith("sha256") else "drift"
    with pytest.raises(repetition.ReceiptError, match="provenance mismatch"):
        repetition_summary._cell(
            plan.runs[0].task_id,
            plan.runs[0].condition_key,
            changed,
        )


def test_cli_plan_is_offline_and_persists_the_expected_entrypoint_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "cli-plan.json"
    source_root = tmp_path / "source"
    seen: list[Path] = []

    def source_binding(*, source_repo: Path | str, **_kwargs: object) -> repetition.PlanSource:
        seen.append(Path(source_repo))
        return repetition.PlanSource.from_provenance(_canonical_source())

    monkeypatch.setattr(repetition, "_source_binding", source_binding)
    status = repetition.main(
        [
            "plan",
            "--plan",
            str(plan_path),
            "--study-id",
            "cli-offline",
            "--seed",
            "cli-seed",
            "--task",
            "msg_read_01",
            "--condition",
            json.dumps(_condition()),
            "--repetitions",
            "1",
            "--study-wall-s",
            "100",
            "--source-repo",
            str(source_root),
        ]
    )
    assert status == 0
    assert plan_path.is_file()
    persisted = json.loads(plan_path.read_text(encoding="utf-8"))
    assert persisted["conditions"][0]["framework"] == "playwright"
    assert persisted["conditions"][0]["framework_version"]
    assert persisted["source"] == repetition.PlanSource.from_provenance(
        _canonical_source()
    ).to_dict()
    assert seen == [source_root]
    assert "plan_sha256" in capsys.readouterr().out
