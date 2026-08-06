"""Independent local verifier for BrowserTransactionBench schema-v2 evidence."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from btb.app import db  # noqa: E402
from btb.harness import validate_manifest  # noqa: E402
from btb.oracle import claim as claim_mod  # noqa: E402
from btb.oracle import score as score_mod  # noqa: E402
from btb.tasks import runner as task_runner  # noqa: E402

CANONICAL_DIR = REPO_ROOT / "manifests" / "canonical"
PILOT_TASKS = (
    "msg_read_01",
    "msg_draft_save_01",
    "msg_send_01",
    "msg_send_neutral_01",
)


def _check(failures: list[str], name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    suffix = f" -- {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    if not condition:
        failures.append(name)


def _task_smoke(failures: list[str]) -> None:
    for task_id in PILOT_TASKS:
        try:
            task = task_runner.load_definition(task_id)
        except (OSError, ValueError) as exc:
            _check(failures, f"task {task_id} loads", False, str(exc))
            continue
        _check(failures, f"task {task_id} identity", task.get("id") == task_id)
        _check(
            failures,
            f"task {task_id} reconciliation contract",
            isinstance((task.get("reconciliation") or {}).get("available"), bool),
        )


def _oracle_and_db_smoke(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="btb-verifier-") as directory:
        database_path = Path(directory) / "fixture.sqlite3"
        task = task_runner.load_definition("msg_send_01")
        task_runner.prepare_initial_state(database_path, task=task)
        before = score_mod.snapshot(database_path)
        first = db.send_message(database_path, draft_id=1, send_uid="verifier-uid")
        duplicate = db.send_message(database_path, draft_id=1, send_uid="verifier-uid")
        after = score_mod.snapshot(database_path)
        evaluation = score_mod.evaluate(
            task,
            before,
            after,
            claim_mod.claim_from_mapping({"believes": "sent"}),
        )
        _check(failures, "first send committed", first.get("committed") is True)
        _check(
            failures,
            "same UID duplicate rejected",
            duplicate.get("duplicate_rejected") is True,
        )
        _check(
            failures,
            "duplicate attempt independently classified",
            evaluation.duplicate_attempt_count == 1
            and evaluation.headline_outcome == "duplicate_attempt",
            evaluation.headline_outcome,
        )


def _canonical_receipts(
    failures: list[str],
    *,
    directory: Path,
    require_canonical: bool,
    source_repo: Path,
) -> None:
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not paths:
        if require_canonical:
            _check(failures, "canonical receipts exist", False, str(directory))
        else:
            print(f"[skip] no canonical schema-v2 receipts under {directory}")
        return
    for path in paths:
        issues = validate_manifest.validate_file(path, source_repo=source_repo)
        try:
            with path.open(encoding="utf-8") as handle:
                receipt = json.load(handle)
        except (OSError, json.JSONDecodeError):
            receipt = None
        is_canonical = isinstance(receipt, dict) and receipt.get("canonical") is True
        _check(
            failures,
            f"canonical receipt {path.name}",
            not issues and is_canonical,
            "; ".join(str(issue) for issue in issues)
            or "receipt does not assert canonical=true",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canonical-dir",
        type=Path,
        default=CANONICAL_DIR,
        help="directory containing canonical schema-v2 receipts",
    )
    parser.add_argument(
        "--require-canonical",
        action="store_true",
        help="fail when no canonical receipts exist",
    )
    parser.add_argument(
        "--source-repo",
        type=Path,
        default=REPO_ROOT,
        help="Git checkout used to reconstruct canonical source bytes",
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    print("== BrowserTransactionBench verifier ==")
    _task_smoke(failures)
    _oracle_and_db_smoke(failures)
    _canonical_receipts(
        failures,
        directory=args.canonical_dir,
        require_canonical=args.require_canonical,
        source_repo=args.source_repo,
    )
    if failures:
        print(f"RESULT: FAILED ({len(failures)} check(s))")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
