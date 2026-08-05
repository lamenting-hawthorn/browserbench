"""Authoritative SQLite layer for the BrowserTransactionBench message fixture.

The database is the single source of truth ("the oracle"). Every durable
effect (draft create, draft save, message send) writes here synchronously
before any HTTP response returns. This module is the ONLY place that reads or
writes the DB, so "what actually happened" is always resolvable from these
tables — independent of any agent claim, screenshot, or browser state.

Schema invariants:
- ``messages.send_uid`` has a UNIQUE index. A second send with the same uid is
  rejected at the DB layer (this is the idempotency backstop). A retry that
  mints a NEW uid is *not* rejected and will create a duplicate row — which is
  exactly the duplicate-send signal we measure.
- ``send_attempts`` audit log records every attempt including rejections, so
  we can distinguish "never tried" from "tried once and rejected" from
  "tried & committed".
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('new','saved')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    draft_id INTEGER REFERENCES drafts(id),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    send_uid TEXT NOT NULL UNIQUE,
    sent_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS send_attempts (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    send_uid TEXT NOT NULL,
    outcome TEXT NOT NULL,            -- 'committed' | 'duplicate_rejected'
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_drafts_user ON drafts(user_id);
"""

DEFAULT_DB = Path(__file__).parent / "btb.db"


def _now() -> float:
    return time.time()


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection. Does NOT run DDL (to avoid write-lock contention in a
    threaded server). Schema/setup is handled by init_db()/reset()."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL + busy timeout let short-lived connections in a threaded server
    # (FastAPI threadpool) coexist without "database is locked" failures.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: Path | str = DEFAULT_DB, *, seed_user: Optional[str] = None) -> None:
    """Ensure schema exists and (optionally) a user. Called once at app startup
    from the main event-loop thread, not per-request."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(_SCHEMA)
    rows = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if rows == 0 and seed_user is not None:
        conn.execute("INSERT INTO users (name) VALUES (?)", (seed_user,))
    conn.commit()
    conn.close()


def new_send_uid(*, seed: str | None = None) -> str:
    """Create a send uid. The uid is what binds identity for idempotency.

    For deterministic tests we allow a fixed seed; in production/normal use a
    uuid4 is used. A retry that blindly calls ``new_send_uid()`` again gets a
    NEW uid and therefore is NOT idempotent-safe — that is being measured.
    """
    if seed is not None:
        return hashlib.sha256(seed.encode()).hexdigest()[:32]
    return uuid.uuid4().hex[:32]


# ---------------------------------------------------------------------------
# Resets & setup (used by the harness to load a deterministic initial state)
# ---------------------------------------------------------------------------

def reset(path: Path | str = DEFAULT_DB, *, seed_user: str = "alice") -> None:
    """Wipe and rebuild schema with a single user. Deterministic initial state."""
    conn = connect(path)
    conn.executescript(
        """
        DROP TABLE IF EXISTS send_attempts;
        DROP TABLE IF EXISTS messages;
        DROP TABLE IF EXISTS drafts;
        DROP TABLE IF EXISTS users;
        """
    )
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO users (name) VALUES (?)", (seed_user,))
    conn.commit()
    conn.close()


def seed_draft(
    path: Path | str = DEFAULT_DB,
    *,
    subject: str,
    body: str,
    status: str = "new",
    user_id: int = 1,
    draft_id: int | None = None,
) -> int:
    """Insert a draft and return its id. Used by the task definitions' initial
    state loader and by the app itself via create_draft."""
    conn = connect(path)
    t = _now()
    if draft_id is not None:
        cur = conn.execute(
            "INSERT INTO drafts (id, user_id, subject, body, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (draft_id, user_id, subject, body, status, t, t),
        )
    else:
        cur = conn.execute(
            "INSERT INTO drafts (user_id, subject, body, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, subject, body, status, t, t),
        )
    conn.commit()
    assert cur.lastrowid is not None
    did = cur.lastrowid
    conn.close()
    return did


# ---------------------------------------------------------------------------
# Durable effect handlers (called by the app; synchronous + durable)
# ---------------------------------------------------------------------------

def create_draft(
    path: Path | str = DEFAULT_DB,
    *,
    user_id: int,
    subject: str,
    body: str,
    status: str = "new",
) -> dict:
    conn = connect(path)
    t = _now()
    cur = conn.execute(
        "INSERT INTO drafts (user_id, subject, body, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, subject, body, status, t, t),
    )
    conn.commit()
    draft = dict(
        cur.execute("SELECT * FROM drafts WHERE id=?", (cur.lastrowid,)).fetchone()
    )
    conn.close()
    return draft


def save_draft(path: Path | str = DEFAULT_DB, *, draft_id: int) -> dict:
    """Mark a draft 'saved'. Reversible-edit effect class. Idempotent (saving a
    saved draft is a no-op)."""
    conn = connect(path)
    conn.execute(
        "UPDATE drafts SET status='saved', updated_at=? WHERE id=?",
        (_now(), draft_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    conn.close()
    if row is None:
        raise KeyError(f"draft {draft_id} not found")
    return dict(row)


def send_message(
    path: Path | str = DEFAULT_DB,
    *,
    draft_id: int,
    send_uid: str | None = None,
) -> dict:
    """Send a draft. OUTBOUND effect class (sandboxed).

    Durability: the message row + attempt log are written synchronously before
    this returns. If a send with the same ``send_uid`` already exists, the UNIQUE
    constraint rejects it and the attempt is logged as 'duplicate_rejected'.
    """
    conn = connect(path)
    t = _now()
    uid = send_uid or new_send_uid()
    draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if draft is None:
        conn.close()
        raise KeyError(f"draft {draft_id} not found")

    try:
        cur = conn.execute(
            "INSERT INTO messages (user_id, draft_id, subject, body, send_uid, sent_at) "
            "VALUES (?,?,?,?,?,?)",
            (draft["user_id"], draft_id, draft["subject"], draft["body"], uid, t),
        )
    except sqlite3.IntegrityError:
        # Same uid already committed -> duplicate attempt, idempotency backstop.
        conn.execute(
            "INSERT INTO send_attempts (draft_id, send_uid, outcome, created_at) "
            "VALUES (?,?, 'duplicate_rejected', ?)",
            (draft_id, uid, t),
        )
        conn.commit()
        conn.close()
        return {"committed": False, "duplicate_rejected": True, "send_uid": uid}

    msg_id = cur.lastrowid
    conn.execute(
        "INSERT INTO send_attempts (draft_id, send_uid, outcome, created_at) "
        "VALUES (?,?, 'committed', ?)",
        (draft_id, uid, t),
    )
    conn.commit()
    msg = dict(
        conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    )
    conn.close()
    return {"committed": True, "duplicate_rejected": False, "message": msg}


# ---------------------------------------------------------------------------
# Oracle queries (authoritative read-only views the harness uses for scoring)
# ---------------------------------------------------------------------------

def count_sent(path: Path | str = DEFAULT_DB, *, user_id: int = 1) -> int:
    conn = connect(path)
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    conn.close()
    return n


def messages(path: Path | str = DEFAULT_DB) -> list[dict]:
    conn = connect(path)
    rows = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_draft(path: Path | str = DEFAULT_DB, *, draft_id: int) -> Optional[dict]:
    conn = connect(path)
    row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_drafts(path: Path | str = DEFAULT_DB) -> list[dict]:
    conn = connect(path)
    rows = conn.execute("SELECT * FROM drafts ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def draft_status(path: Path | str = DEFAULT_DB, *, draft_id: int) -> Optional[str]:
    d = get_draft(path, draft_id=draft_id)
    return d["status"] if d else None


def was_sent_once(path: Path | str = DEFAULT_DB, *, send_uid: str) -> bool:
    conn = connect(path)
    row = conn.execute(
        "SELECT id FROM messages WHERE send_uid=?", (send_uid,)
    ).fetchone()
    conn.close()
    return row is not None


def duplicate_attempts(path: Path | str = DEFAULT_DB) -> list[dict]:
    conn = connect(path)
    rows = conn.execute(
        "SELECT * FROM send_attempts WHERE outcome='duplicate_rejected' ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sends(path: Path | str = DEFAULT_DB) -> list[dict]:
    conn = connect(path)
    rows = conn.execute(
        "SELECT draft_id, send_uid, outcome FROM send_attempts ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
