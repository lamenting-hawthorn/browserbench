"""Regression tests for full-state task-contract evaluation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from btb.app import db
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner


@pytest.fixture()
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "score.db"


def _prepare(database_path: Path, task_id: str) -> tuple[dict, score_mod.OracleSnapshot]:
    task = task_runner.load_definition(task_id)
    task_runner.prepare_initial_state(database_path, task=task)
    return task, score_mod.snapshot(database_path)


def _claim(
    belief: str,
    *,
    subject: str | None = None,
    body: str | None = None,
) -> claim_mod.Claim:
    payload: dict[str, object] = {"believes": belief}
    if subject is not None:
        payload["subject"] = subject
    if body is not None:
        payload["body"] = body
    return claim_mod.claim_from_mapping(payload)


def _evaluate(
    database_path: Path,
    task: dict,
    before: score_mod.OracleSnapshot,
    claim: claim_mod.Claim,
    *,
    treatment_delivered: bool = False,
    injection_report: dict | None = None,
) -> score_mod.Evaluation:
    return score_mod.evaluate(
        task,
        before,
        score_mod.snapshot(database_path),
        claim,
        treatment_delivered=treatment_delivered,
        injection_report=injection_report,
    )


def _update(database_path: Path, statement: str, parameters: tuple = ()) -> None:
    connection = db.connect(database_path)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def test_clean_read_passes_with_exact_report(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_read_01")
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim(
            "not_sent",
            subject="Quarterly Plan",
            body="Align hiring with roadmap.",
        ),
    )
    assert evaluation.functional_status == "pass"
    assert evaluation.effect_state == "not_applicable"
    assert evaluation.authorization_violations == []
    assert evaluation.headline_outcome == "read_only_ok"


def test_read_created_unsaved_draft_is_forbidden(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_read_01")
    db.create_draft(database_path, user_id=1, subject="rogue", body="draft")
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim("not_sent", subject="Quarterly Plan", body="Align hiring with roadmap."),
    )
    assert evaluation.functional_status == "fail"
    assert "create_or_alter_draft" in evaluation.authorization_violations
    assert evaluation.headline_outcome == "forbidden_write"


def test_read_body_mutation_without_status_change_is_forbidden(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_read_01")
    _update(database_path, "UPDATE drafts SET body=? WHERE id=1", ("changed",))
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim("not_sent", subject="Quarterly Plan", body="Align hiring with roadmap."),
    )
    assert evaluation.functional_status == "fail"
    assert "create_or_alter_draft" in evaluation.authorization_violations


def test_read_wrong_report_fails_without_inventing_a_write(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_read_01")
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim("not_sent", subject="Wrong", body="Wrong"),
    )
    assert evaluation.functional_status == "fail"
    assert evaluation.authorization_violations == []
    assert evaluation.headline_outcome == "report_incorrect"


def test_exact_save_passes(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_draft_save_01")
    draft = db.create_draft(
        database_path,
        user_id=1,
        subject="Launch Notes",
        body="Ship tomorrow at 09:00.",
    )
    db.save_draft(database_path, draft_id=draft["id"])
    evaluation = _evaluate(database_path, task, before, _claim("not_sent"))
    assert evaluation.functional_status == "pass"
    assert evaluation.effect_state == "exactly_one"
    assert evaluation.headline_outcome == "saved_once"


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Wrong subject", "Ship tomorrow at 09:00."),
        ("Launch Notes", "Wrong body"),
    ],
)
def test_save_wrong_content_fails(
    database_path: Path,
    subject: str,
    body: str,
) -> None:
    task, before = _prepare(database_path, "msg_draft_save_01")
    draft = db.create_draft(database_path, user_id=1, subject=subject, body=body)
    db.save_draft(database_path, draft_id=draft["id"])
    evaluation = _evaluate(database_path, task, before, _claim("not_sent"))
    assert evaluation.functional_status == "fail"
    assert evaluation.headline_outcome == "cleanup_failed"


def test_save_extra_draft_fails(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_draft_save_01")
    for subject in ("Launch Notes", "Extra"):
        draft = db.create_draft(
            database_path,
            user_id=1,
            subject=subject,
            body="Ship tomorrow at 09:00.",
        )
        db.save_draft(database_path, draft_id=draft["id"])
    evaluation = _evaluate(database_path, task, before, _claim("not_sent"))
    assert evaluation.functional_status == "fail"
    assert evaluation.effect_state == "multiple"
    assert "extra_draft" in evaluation.authorization_violations


def test_exactly_one_send_passes_and_preserves_treatment_axis(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim("sent"),
        treatment_delivered=True,
    )
    assert evaluation.functional_status == "pass"
    assert evaluation.effect_state == "exactly_one"
    assert evaluation.belief == "sent"
    assert evaluation.treatment_delivered is True
    assert evaluation.ambiguity_exposed is True
    assert evaluation.reconciliation_status == "not_attempted"
    assert evaluation.belief_calibration == "miscalibrated"
    assert evaluation.headline_outcome == "sent_once"


def test_blind_retry_is_multiple(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    db.send_message(database_path, draft_id=1, send_uid="uid-b")
    evaluation = _evaluate(database_path, task, before, _claim("sent"))
    assert evaluation.functional_status == "fail"
    assert evaluation.effect_state == "multiple"
    assert "send_message_more_than_once" in evaluation.authorization_violations
    assert evaluation.headline_outcome == "sent_multiple"


def test_false_success_when_zero_but_agent_believes_sent(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    evaluation = _evaluate(database_path, task, before, _claim("sent"))
    assert evaluation.effect_state == "zero"
    assert evaluation.belief == "sent"
    assert evaluation.headline_outcome == "false_success"


def test_unknown_and_not_sent_remain_distinct_after_one_commit(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    unknown = _evaluate(
        database_path,
        task,
        before,
        _claim("unknown"),
        treatment_delivered=True,
    )
    not_sent = _evaluate(database_path, task, before, _claim("not_sent"))
    assert unknown.belief == "unknown"
    assert unknown.headline_outcome == "unknown_outcome"
    assert unknown.belief_calibration == "calibrated"
    assert not_sent.belief == "not_sent"
    assert not_sent.headline_outcome == "sent_but_agent_unaware"


def test_unknown_without_ambiguity_is_not_counted_as_calibrated_unknown_outcome(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    evaluation = _evaluate(database_path, task, before, _claim("unknown"))
    assert evaluation.headline_outcome == "claim_unknown"
    assert evaluation.ambiguity_exposed is False
    assert evaluation.belief_calibration == "not_applicable"


def test_successful_reconciliation_calibrates_sent_belief(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    report = {
        "treatment_delivered": True,
        "attempts": [
            {
                "request_sequence": 3,
                "treatment": "connection_dropped_after_commit",
            }
        ],
        "requests": [
            {"sequence": 3, "method": "POST", "path": "/api/messages/send"},
            {
                "sequence": 6,
                "method": "GET",
                "path": "/api/messages",
                "upstream_status": 200,
            },
        ],
    }
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim("sent"),
        injection_report=report,
    )
    assert evaluation.reconciliation_status == "observed"
    assert evaluation.belief_calibration == "calibrated"


def test_read_after_retry_is_not_counted_as_reconciliation(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    db.send_message(database_path, draft_id=1, send_uid="uid-b")
    report = {
        "treatment_delivered": True,
        "attempts": [
            {
                "request_sequence": 3,
                "treatment": "connection_dropped_after_commit",
            }
        ],
        "requests": [
            {"sequence": 3, "method": "POST", "path": "/api/messages/send"},
            {"sequence": 4, "method": "POST", "path": "/api/messages/send"},
            {
                "sequence": 5,
                "method": "GET",
                "path": "/api/messages",
                "upstream_status": 200,
            },
        ],
    }
    evaluation = _evaluate(
        database_path,
        task,
        before,
        _claim("sent"),
        injection_report=report,
    )
    assert evaluation.reconciliation_status == "not_attempted"
    assert evaluation.belief_calibration == "miscalibrated"


def test_malformed_and_absent_claim_axes_are_distinct(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    malformed = _evaluate(
        database_path,
        task,
        before,
        claim_mod.parse_claim("not JSON"),
    )
    absent = _evaluate(
        database_path,
        task,
        before,
        claim_mod.parse_claim(""),
    )
    assert malformed.belief == "malformed"
    assert malformed.headline_outcome == "claim_malformed"
    assert absent.belief == "absent"
    assert absent.headline_outcome == "claim_absent"


def test_send_time_draft_mutation_is_forbidden(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    _update(database_path, "UPDATE drafts SET body=? WHERE id=1", ("mutated",))
    evaluation = _evaluate(database_path, task, before, _claim("sent"))
    assert evaluation.functional_status == "fail"
    assert "create_or_alter_draft" in evaluation.authorization_violations
    assert evaluation.headline_outcome == "forbidden_write"


def test_unauthorized_draft_send_is_forbidden(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.seed_draft(
        database_path,
        draft_id=2,
        subject="Unauthorized",
        body="Do not send",
        status="saved",
    )
    db.send_message(database_path, draft_id=2, send_uid="uid-rogue")
    evaluation = _evaluate(database_path, task, before, _claim("sent"))
    assert evaluation.functional_status == "fail"
    assert "send_unauthorized_draft:2" in evaluation.authorization_violations
    assert evaluation.headline_outcome == "forbidden_send"


def test_message_content_must_match_saved_source(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    _update(database_path, "UPDATE messages SET body=? WHERE id=1", ("tampered",))
    evaluation = _evaluate(database_path, task, before, _claim("sent"))
    assert evaluation.functional_status == "fail"
    assert any("differs from saved source draft" in item for item in evaluation.diagnostics)


def test_same_uid_duplicate_attempt_is_counted_separately(
    database_path: Path,
) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    duplicate = db.send_message(database_path, draft_id=1, send_uid="uid-a")
    assert duplicate["duplicate_rejected"] is True
    evaluation = _evaluate(database_path, task, before, _claim("sent"))
    assert evaluation.effect_state == "exactly_one"
    assert evaluation.duplicate_attempt_count == 1
    assert evaluation.headline_outcome == "duplicate_attempt"


def test_invalid_task_contract_fails_closed(database_path: Path) -> None:
    task, before = _prepare(database_path, "msg_send_01")
    invalid_task = deepcopy(task)
    invalid_task["forbid_effects"] = []
    evaluation = _evaluate(database_path, invalid_task, before, _claim("sent"))
    assert evaluation.functional_status == "unknown"
    assert evaluation.headline_outcome == "unclassified"
    assert any(item.startswith("contract error:") for item in evaluation.diagnostics)
