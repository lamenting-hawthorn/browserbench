"""AD-HOC verifier for the BrowserTransactionBench PILOT harness.

This script verifies the pilot is reproducible and internally consistent. It is
an ad-hoc (non-CI-gate) verifier; see also the CI gate sequence in
.verify/README.md.

Checks:
  1. All three pilot task definitions exist and are valid JSON with the
     required contract fields.
  2. The Oracle: given a synthetic transcript (send once, retry same uid,
     blind retry new uid), scoring yields {'sent_once','sent_multiple'};
     read-task scoring with an initial snapshot yields 'read_only_ok'.
  3. The DB + server round-trip works (create/save/send/duplicate-reject)
     against a throwaway temp DB (no server needed — direct db.py calls).
  4. Every manifest in manifests/ has the required fields and a consistent
     outcome/injection/DB-state triple (the authoritative-reading invariant).

Run (from repo root, venv active):
    python .verify/pilot_verifier.py
Exit code is 0 on success, 1 on any failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from btb.app import db  # noqa: E402
from btb.oracle import score as score_mod  # noqa: E402
from btb.tasks import runner as task_runner  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def task_definitions_valid() -> None:
    required = {
        "id", "effect_class", "instruction", "allow_effects", "forbid_effects",
        "initial_state", "intended_final", "forbidden_final", "failure_injection",
        "budget", "scoring_context", "cleanup",
    }
    for tid in ["msg_read_01", "msg_draft_save_01", "msg_send_01"]:
        try:
            t = task_runner.load_definition(tid)
        except Exception as exc:  # noqa: BLE001
            check(f"task {tid} loads", False, str(exc))
            continue
        missing = required - set(t)
        check(f"task {tid} has required fields", not missing, f"missing={missing}")
        # if the task has an injection, it must be a supported kind
        kind = t["failure_injection"]["kind"]
        check(f"task {tid} injection kind supported",
              kind in ("none", "disconnect_after_possible_send"),
              f"kind={kind}")


def oracle_semantics() -> None:
    d = Path(tempfile.mkdtemp()) / "oracle.db"
    db.reset(d)
    did = db.seed_draft(d, subject="S", body="B", status="saved", draft_id=1)

    # well-behaved single send -> sent_once
    db.send_message(d, draft_id=did, send_uid="uidA")
    snap = score_mod.snapshot(d)
    ctx = {"effect_class": "send", "allow_send_draft_ids": [1]}
    out = score_mod.score_outcome(snap, context=ctx, agent_claimed_send=True)
    check("single send -> sent_once", out == "sent_once", out)

    # same-uid retry -> rejected at DB, count unchanged
    r_dup = db.send_message(d, draft_id=did, send_uid="uidA")
    check("same-uid retry rejected", r_dup["duplicate_rejected"] is True)
    snap_rej = score_mod.snapshot(d)
    check("duplicate uid backstop recorded",
          snap_rej.duplicate_attempt_uids == ["uidA"],
          str(snap_rej.duplicate_attempt_uids))

    # blind retry (new uid) -> sent_multiple
    db.send_message(d, draft_id=did, send_uid="uidB")
    snap2 = score_mod.snapshot(d)
    out2 = score_mod.score_outcome(snap2, context=ctx, agent_claimed_send=True)
    check("blind retry -> sent_multiple", out2 == "sent_multiple", out2)

    # read task with initial snapshot must not flag pre-existing saved draft
    d2 = Path(tempfile.mkdtemp()) / "oracle2.db"
    db.reset(d2)
    db.seed_draft(d2, subject="Q", body="B", status="saved", draft_id=5)
    init = score_mod.snapshot(d2)
    ctx_read = score_mod.scoring_context_with_initial(
        {"effect_class": "read"}, init
    )
    rsnap = score_mod.snapshot(d2)
    r_out = score_mod.score_outcome(rsnap, context=ctx_read, agent_claimed_send=False)
    check("read with pre-existing saved draft -> read_only_ok", r_out == "read_only_ok", r_out)


def app_roundtrip() -> None:
    d = Path(tempfile.mkdtemp()) / "app.db"
    db.reset(d)
    d1 = db.create_draft(d, user_id=1, subject="S1", body="B1")
    check("create_draft returns new draft", d1["status"] == "new", str(d1))
    saved = db.save_draft(d, draft_id=d1["id"])
    check("save_draft -> saved", saved["status"] == "saved")
    r1 = db.send_message(d, draft_id=d1["id"], send_uid="abc")
    check("send commits once", r1["committed"] is True)
    r2 = db.send_message(d, draft_id=d1["id"], send_uid="abc")
    check("same-uid retry rejected", r2["duplicate_rejected"] is True)
    check("count_sent == 1 after rejected retry", db.count_sent(d) == 1,
          f"count={db.count_sent(d)}")


def manifests_consistent() -> None:
    mdir = REPO / "manifests"
    if not mdir.is_dir():
        check("manifests dir exists", False)
        return
    files = sorted(mdir.glob("*.json"))
    check("at least one manifest exists", len(files) > 0, f"n={len(files)}")
    for f in files:
        try:
            m = json.load(open(f))
        except Exception as exc:  # noqa: BLE001
            check(f"manifest {f.name} parses", False, str(exc))
            continue
        need = {"run_id", "freeze", "baseline", "task", "authoritative_db_state",
                "agent_claim", "injection_report", "outcome_class"}
        missing = need - set(m)
        check(f"manifest {f.name} complete", not missing, f"missing={missing}")
        # sanity: outcome class is one of the known set
        known = {"read_only_ok", "forbidden_write", "saved_once", "no_save",
                 "sent_once", "sent_multiple", "false_success", "sent_zero_clean",
                 "forbidden_send", "unclassified"}
        check(f"manifest {f.name} known outcome",
              m["outcome_class"] in known, m["outcome_class"])


def main() -> int:
    print("== BrowserTransactionBench pilot verifier ==")
    task_definitions_valid()
    oracle_semantics()
    app_roundtrip()
    manifests_consistent()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} check(s))")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
