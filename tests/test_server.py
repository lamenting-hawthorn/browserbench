"""Tests for the FastAPI app round-trip (btb.app.server) via TestClient.

Verifies the public effect endpoints against the authoritative DB: create,
save, send, duplicate-reject. Uses a throwaway DB via the BTB_DB env var.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btb.app import db

# Point the app at a throwaway DB BEFORE importing the server module.
_TMP = Path(tempfile.mkdtemp()) / "http.db"
os.environ["BTB_DB"] = str(_TMP)

from btb.app.server import app  # noqa: E402


@pytest.fixture()
def client():
    db.reset(_TMP)
    with TestClient(app) as c:
        yield c


def test_health(client) -> None:
    assert client.get("/health").json()["ok"] is True


def test_create_and_list_draft(client) -> None:
    r = client.post("/api/drafts", json={"subject": "S", "body": "B"})
    assert r.status_code == 200
    draft = r.json()
    assert draft["status"] == "new"
    assert draft["id"] == 1

    lst = client.get("/api/drafts").json()
    assert [d["id"] for d in lst] == [1]


def test_save_then_send(client) -> None:
    client.post("/api/drafts", json={"subject": "S", "body": "B"})
    client.post("/api/drafts/1/save", json={"draft_id": 1})
    r = client.post("/api/messages/send", json={"draft_id": 1, "send_uid": "httpA"})
    assert r.json()["committed"] is True

    msgs = client.get("/api/messages").json()
    assert len(msgs) == 1


def test_duplicate_send_rejected(client) -> None:
    client.post("/api/drafts", json={"subject": "S", "body": "B"})
    client.post("/api/drafts/1/save", json={"draft_id": 1})
    client.post("/api/messages/send", json={"draft_id": 1, "send_uid": "httpA"})
    r = client.post("/api/messages/send", json={"draft_id": 1, "send_uid": "httpA"})
    assert r.json()["duplicate_rejected"] is True
    assert len(client.get("/api/messages").json()) == 1  # still one


def test_send_missing_draft_404(client) -> None:
    r = client.post("/api/messages/send", json={"draft_id": 99})
    assert r.status_code == 404
