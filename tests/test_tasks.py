"""Tests for the frozen task definitions (btb/tasks/definitions/*.json).

Guards against a malformed or non-contract task silently breaking the pilot.
"""

from __future__ import annotations

from btb.tasks import runner as task_runner

PILOT_TASKS = ["msg_read_01", "msg_draft_save_01", "msg_send_01", "msg_send_neutral_01"]
REQUIRED = {
    "id", "freeze", "effect_class", "instruction", "allow_effects", "forbid_effects",
    "initial_state", "intended_final", "forbidden_final", "failure_injection",
    "budget", "scoring_context", "cleanup",
}
EFFECT_CLASSES = {"read", "save", "send"}
INJECTION_KINDS = {"none", "disconnect_after_possible_send"}


def test_all_pilot_tasks_load() -> None:
    for tid in PILOT_TASKS:
        assert task_runner.load_definition(tid) is not None


def test_tasks_have_required_contract() -> None:
    for tid in PILOT_TASKS:
        t = task_runner.load_definition(tid)
        assert t["id"] == tid
        assert REQUIRED.issubset(set(t)), f"{tid} missing {REQUIRED - set(t)}"
        assert t["effect_class"] in EFFECT_CLASSES
        assert t["failure_injection"]["kind"] in INJECTION_KINDS
        assert isinstance(t["budget"]["steps"], int)
        assert isinstance(t["budget"]["wall_s"], int)


def test_task_ids_are_unique() -> None:
    ids = [task_runner.load_definition(t)["id"] for t in PILOT_TASKS]
    assert len(ids) == len(set(ids))
