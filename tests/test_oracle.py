"""Tests for authoritative SQLite effects and atomic snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from btb.app import db


@pytest.fixture()
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "oracle.db"
    db.reset(path)
    return path


def test_reset_creates_one_user(database_path: Path) -> None:
    snapshot = db.full_snapshot(database_path)
    assert snapshot["users"] == [{"id": 1, "name": "alice"}]
    assert snapshot["drafts"] == []
    assert snapshot["messages"] == []
    assert snapshot["send_attempts"] == []


def test_seed_draft_and_status(database_path: Path) -> None:
    draft_id = db.seed_draft(
        database_path,
        subject="S",
        body="B",
        status="saved",
        draft_id=1,
    )
    assert draft_id == 1
    assert db.draft_status(database_path, draft_id=1) == "saved"


def test_send_once_commits_message_and_attempt_atomically(database_path: Path) -> None:
    db.seed_draft(database_path, subject="S", body="B", status="saved", draft_id=1)
    result = db.send_message(database_path, draft_id=1, send_uid="uid-a")
    snapshot = db.full_snapshot(database_path)
    assert result["committed"] is True
    assert result["duplicate_rejected"] is False
    assert [row["send_uid"] for row in snapshot["messages"]] == ["uid-a"]
    assert [row["outcome"] for row in snapshot["send_attempts"]] == ["committed"]


def test_unsaved_send_is_rejected_without_any_durable_attempt(
    database_path: Path,
) -> None:
    db.seed_draft(database_path, subject="S", body="B", status="new", draft_id=1)
    with pytest.raises(db.DraftNotSavedError, match="must be saved"):
        db.send_message(database_path, draft_id=1, send_uid="uid-a")
    snapshot = db.full_snapshot(database_path)
    assert snapshot["messages"] == []
    assert snapshot["send_attempts"] == []


def test_same_uid_retry_is_a_rejected_duplicate_attempt(database_path: Path) -> None:
    db.seed_draft(database_path, subject="S", body="B", status="saved", draft_id=1)
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    duplicate = db.send_message(database_path, draft_id=1, send_uid="uid-a")
    snapshot = db.full_snapshot(database_path)
    assert duplicate["duplicate_rejected"] is True
    assert len(snapshot["messages"]) == 1
    assert [row["outcome"] for row in snapshot["send_attempts"]] == [
        "committed",
        "duplicate_rejected",
    ]


def test_blind_retry_with_new_uid_commits_twice(database_path: Path) -> None:
    db.seed_draft(database_path, subject="S", body="B", status="saved", draft_id=1)
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    second = db.send_message(database_path, draft_id=1, send_uid="uid-b")
    assert second["committed"] is True
    assert db.count_sent(database_path) == 2


def test_unique_send_uid_constraint_is_database_enforced(database_path: Path) -> None:
    db.seed_draft(database_path, subject="S", body="B", status="saved", draft_id=1)
    db.send_message(database_path, draft_id=1, send_uid="uid-a")
    connection = db.connect(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO messages "
                "(user_id, draft_id, subject, body, send_uid, sent_at) "
                "VALUES (1, 1, 'S', 'B', 'uid-a', ?)",
                (1.0,),
            )
    finally:
        connection.close()


def test_full_snapshot_rows_are_complete_and_primary_key_ordered(
    database_path: Path,
) -> None:
    db.seed_draft(database_path, subject="second", body="B", draft_id=2)
    db.seed_draft(database_path, subject="first", body="A", draft_id=1)
    snapshot = db.full_snapshot(database_path)
    assert [row["id"] for row in snapshot["drafts"]] == [1, 2]
    assert set(snapshot) == {"users", "drafts", "messages", "send_attempts"}
    assert {"subject", "body", "status", "created_at", "updated_at"}.issubset(
        snapshot["drafts"][0]
    )
