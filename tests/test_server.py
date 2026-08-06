"""Tests for the FastAPI app round-trip via its explicit app factory.

Verifies the public effect endpoints against the authoritative DB: create,
save, send, and duplicate rejection.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btb.app import db
from btb.app.server import create_app


@pytest.fixture()
def database_path(tmp_path: Path) -> Path:
    return (tmp_path / "http.db").resolve()


@pytest.fixture()
def client(database_path: Path) -> Iterator[TestClient]:
    db.reset(database_path)
    with TestClient(
        create_app(database_path, run_id="server-test", ui_token="test-ui-token")
    ) as test_client:
        test_client.headers["X-BTB-UI-Token"] = "test-ui-token"
        yield test_client


def test_health(client: TestClient, database_path: Path) -> None:
    assert client.get("/health").json() == {
        "ok": True,
        "db": str(database_path),
        "run_id": "server-test",
    }


def test_api_rejects_requests_not_originating_from_visible_controls(
    client: TestClient,
) -> None:
    response = client.get("/api/drafts", headers={"X-BTB-UI-Token": ""})
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "fixture API is available only through visible page controls"
    )


def test_create_and_list_draft(client: TestClient) -> None:
    response = client.post("/api/drafts", json={"subject": "S", "body": "B"})
    assert response.status_code == 200
    draft = response.json()
    assert draft["status"] == "new"
    assert draft["id"] == 1

    drafts = client.get("/api/drafts").json()
    assert [draft["id"] for draft in drafts] == [1]


def test_save_then_send(client: TestClient) -> None:
    client.post("/api/drafts", json={"subject": "S", "body": "B"})
    client.post("/api/drafts/1/save", json={"draft_id": 1})
    response = client.post(
        "/api/messages/send",
        json={"draft_id": 1, "send_uid": "httpA"},
    )
    assert response.json()["committed"] is True

    messages = client.get("/api/messages").json()
    assert len(messages) == 1


def test_duplicate_send_rejected(client: TestClient) -> None:
    client.post("/api/drafts", json={"subject": "S", "body": "B"})
    client.post("/api/drafts/1/save", json={"draft_id": 1})
    client.post(
        "/api/messages/send",
        json={"draft_id": 1, "send_uid": "httpA"},
    )
    response = client.post(
        "/api/messages/send",
        json={"draft_id": 1, "send_uid": "httpA"},
    )
    assert response.json()["duplicate_rejected"] is True
    assert len(client.get("/api/messages").json()) == 1


def test_send_missing_draft_404(client: TestClient) -> None:
    response = client.post("/api/messages/send", json={"draft_id": 99})
    assert response.status_code == 404


def test_send_unsaved_draft_409_without_durable_attempt(
    client: TestClient,
    database_path: Path,
) -> None:
    client.post("/api/drafts", json={"subject": "S", "body": "B"})
    response = client.post(
        "/api/messages/send",
        json={"draft_id": 1, "send_uid": "unsaved-uid"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "draft 1 must be saved before sending"
    snapshot = db.full_snapshot(database_path)
    assert snapshot["messages"] == []
    assert snapshot["send_attempts"] == []
