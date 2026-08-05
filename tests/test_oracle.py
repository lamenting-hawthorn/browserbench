"""Tests for the authoritative SQLite oracle (btb.app.db).

These verify the exactly-once semantics that the whole benchmark depends on:
a single send commits once; a same-uid retry is rejected (idempotency backstop);
a blind retry with a *new* uid creates a duplicate (the very thing we measure).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btb.app import db


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    path = tmp_path / "oracle.db"
    db.reset(path)
    return path


def test_reset_creates_one_user(d: Path) -> None:
    conn = db.connect(d)
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert n == 1


def test_seed_draft_and_status(d: Path) -> None:
    did = db.seed_draft(d, subject="S", body="B", status="saved", draft_id=1)
    assert did == 1
    assert db.draft_status(d, draft_id=1) == "saved"


def test_send_once_commits_exactly_one(d: Path) -> None:
    db.seed_draft(d, subject="S", body="B", status="saved", draft_id=1)
    r = db.send_message(d, draft_id=1, send_uid="uidA")
    assert r["committed"] is True
    assert r["duplicate_rejected"] is False
    assert db.count_sent(d) == 1
    assert db.was_sent_once(d, send_uid="uidA") is True


def test_same_uid_retry_rejected_idempotent(d: Path) -> None:
    db.seed_draft(d, subject="S", body="B", status="saved", draft_id=1)
    db.send_message(d, draft_id=1, send_uid="uidA")
    r = db.send_message(d, draft_id=1, send_uid="uidA")
    assert r["duplicate_rejected"] is True
    # idempotency backstop: count unchanged
    assert db.count_sent(d) == 1
    assert [a["send_uid"] for a in db.duplicate_attempts(d)] == ["uidA"]


def test_blind_retry_new_uid_duplicates(d: Path) -> None:
    db.seed_draft(d, subject="S", body="B", status="saved", draft_id=1)
    db.send_message(d, draft_id=1, send_uid="uidA")
    r = db.send_message(d, draft_id=1, send_uid="uidB")
    assert r["committed"] is True  # new uid slips past the UNIQUE backstop
    assert db.count_sent(d) == 2


def test_unique_send_uid_constraint(d: Path) -> None:
    # The UNIQUE index on messages.send_uid is DB-enforced.
    db.seed_draft(d, subject="S", body="B", status="saved", draft_id=1)
    db.send_message(d, draft_id=1, send_uid="uidX")
    conn = db.connect(d)
    with pytest.raises(Exception):  # IntegrityError
        conn.execute(
            "INSERT INTO messages (user_id, draft_id, subject, body, send_uid, sent_at) "
            "VALUES (1, 1, 'S', 'B', 'uidX', ?)",
            (1.0,),
        )
    conn.close()
