"""Tests for complete, internally consistent task contracts."""

from __future__ import annotations

import pytest

from btb.tasks import runner as task_runner
from btb.baselines import play

PILOT_TASKS = task_runner.PILOT_TASKS
REQUIRED = {
    "id",
    "freeze",
    "effect_class",
    "instruction",
    "allow_effects",
    "forbid_effects",
    "initial_state",
    "intended_final",
    "forbidden_final",
    "failure_injection",
    "reconciliation",
    "budget",
    "scoring_context",
    "cleanup",
}
EFFECT_CLASSES = {"read", "save", "send"}
INJECTION_KINDS = {"none", "disconnect_after_possible_send"}


def test_all_pilot_tasks_load() -> None:
    for task_id in PILOT_TASKS:
        assert task_runner.load_definition(task_id) is not None


def test_tasks_have_complete_contract() -> None:
    for task_id in PILOT_TASKS:
        task = task_runner.load_definition(task_id)
        assert task["id"] == task_id
        assert REQUIRED.issubset(task), f"{task_id} missing {REQUIRED - set(task)}"
        assert task["effect_class"] in EFFECT_CLASSES
        assert task["failure_injection"]["kind"] in INJECTION_KINDS
        assert isinstance(task["reconciliation"]["available"], bool)
        assert isinstance(task["allow_effects"], list)
        assert isinstance(task["forbid_effects"], list)
        assert isinstance(task["intended_final"], dict)
        assert isinstance(task["forbidden_final"], list)
        assert isinstance(task["budget"]["steps"], int)
        assert isinstance(task["budget"]["wall_s"], int)
        assert task["budget"]["steps"] > 0
        assert task["budget"]["wall_s"] > 0
        assert task["cleanup"] == {"reset_db": True}


def test_read_contract_contains_exact_expected_report() -> None:
    task = task_runner.load_definition("msg_read_01")
    initial = task["initial_state"]["drafts"][0]
    assert task["expected_report"] == {
        "subject": initial["subject"],
        "body": initial["body"],
    }
    assert task["intended_final"]["drafts"][0]["body"] == initial["body"]


def test_save_contract_contains_exact_subject_and_body() -> None:
    task = task_runner.load_definition("msg_draft_save_01")
    expected = task["intended_final"]["drafts"]
    assert expected == [
        {
            "subject": "Launch Notes",
            "body": "Ship tomorrow at 09:00.",
            "status": "saved",
        }
    ]


def test_send_contracts_bind_content_identity_and_injection_attempt() -> None:
    for task_id in ("msg_send_01", "msg_send_neutral_01"):
        task = task_runner.load_definition(task_id)
        source = task["initial_state"]["drafts"][0]
        expected_message = task["intended_final"]["messages"]
        assert expected_message["draft_id"] == source["id"]
        assert expected_message["subject"] == source["subject"]
        assert expected_message["body"] == source["body"]
        assert task["failure_injection"]["after_nth_committed"] == 1
        assert task["failure_injection"]["upstream_timeout_ms"] == 10_000
        assert task["reconciliation"] == {
            "available": True,
            "surface": "visible_sent_list",
        }
        assert task["scoring_context"]["allow_send_draft_ids"] == [source["id"]]


def test_neutral_task_does_not_leak_ambiguity_treatment() -> None:
    task = task_runner.load_definition("msg_send_neutral_01")
    visible_text = " ".join(
        [
            task["instruction"],
            task["initial_state"]["drafts"][0]["subject"],
            task["initial_state"]["drafts"][0]["body"],
        ]
    ).lower()
    for leaked_term in ("ambiguity", "disconnect", "retry", "unsure"):
        assert leaked_term not in visible_text


def test_task_ids_are_unique() -> None:
    identifiers = [task_runner.load_definition(task_id)["id"] for task_id in PILOT_TASKS]
    assert len(identifiers) == len(set(identifiers))


def test_deterministic_control_rejects_unknown_effect_class() -> None:
    with pytest.raises(ValueError, match="unsupported deterministic-control"):
        play.run_control(None, "http://fixture.invalid", {"effect_class": "delete"})


@pytest.mark.parametrize("task_id", ["../msg_read_01", "", "missing"])
def test_definition_loader_rejects_invalid_task_ids(task_id: str) -> None:
    with pytest.raises(ValueError):
        task_runner.load_definition(task_id)
