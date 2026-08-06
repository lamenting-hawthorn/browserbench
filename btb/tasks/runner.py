"""Task runner: load a frozen task definition and prepare/teardown its state.

The runner is deterministic: reset the DB to the task's exported initial state,
let the baseline act, then let the oracle snapshot. It does not know anything
about browsers — it only controls the authoritative SQLite fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from btb.app import db

DEFINITIONS_DIR = Path(__file__).parent / "definitions"
PILOT_TASKS = (
    "msg_read_01",
    "msg_draft_save_01",
    "msg_send_01",
    "msg_send_neutral_01",
)


def load_definition(task_id: str) -> dict:
    if not isinstance(task_id, str) or not task_id or Path(task_id).name != task_id:
        raise ValueError("task_id must be one plain filename stem")
    path = DEFINITIONS_DIR / f"{task_id}.json"
    if not path.is_file():
        raise ValueError(f"unknown task_id: {task_id}")
    with open(path) as fh:
        return json.load(fh)


def prepare_initial_state(
    path: Path | str = db.DEFAULT_DB, *, task: dict, user_id: int = 1
) -> None:
    """Reset DB and load the task's initial_state (drafts). Deterministic."""
    db.reset(path, seed_user="alice")
    for d in task["initial_state"].get("drafts", []):
        db.seed_draft(
            path,
            subject=d["subject"],
            body=d["body"],
            status=d.get("status", "new"),
            user_id=user_id,
            draft_id=d.get("id"),
        )


def teardown(path: Path | str = db.DEFAULT_DB) -> None:
    """Cleanup: reset to empty deterministic state (fail-closed)."""
    db.reset(path, seed_user="alice")


if __name__ == "__main__":
    import sys

    tid = sys.argv[1]
    t = load_definition(tid)
    print(f"Loaded task {tid}: effect_class={t['effect_class']}, injection={t['failure_injection']['kind']}")
