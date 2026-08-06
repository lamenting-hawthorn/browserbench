"""Tests for managed per-run fixture isolation and lifecycle cleanup."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from btb.app import db
from btb.harness import runtime


def _create_draft(base_url: str, ui_token: str, *, subject: str) -> None:
    response = httpx.post(
        f"{base_url}/api/drafts",
        json={"subject": subject, "body": f"body for {subject}"},
        headers={"X-BTB-UI-Token": ui_token},
        timeout=2.0,
    )
    response.raise_for_status()


def test_simultaneous_environments_are_isolated() -> None:
    first = runtime.managed_run_environment(run_id="runtime-first")
    second = runtime.managed_run_environment(run_id="runtime-second")

    with first, second:
        assert first.base_url != second.base_url
        assert first.run_directory != second.run_directory
        assert first.db_path != second.db_path
        assert first.db_path.parent == first.run_directory
        assert second.db_path.parent == second.run_directory

        assert httpx.get(f"{first.base_url}/health", timeout=2.0).json() == {
            "ok": True,
            "db": str(first.db_path),
            "run_id": "runtime-first",
        }
        assert httpx.get(f"{second.base_url}/health", timeout=2.0).json() == {
            "ok": True,
            "db": str(second.db_path),
            "run_id": "runtime-second",
        }

        _create_draft(first.base_url, first.ui_token, subject="first-only")
        _create_draft(second.base_url, second.ui_token, subject="second-only")
        assert [draft["subject"] for draft in db.get_drafts(first.db_path)] == [
            "first-only"
        ]
        assert [draft["subject"] for draft in db.get_drafts(second.db_path)] == [
            "second-only"
        ]

        db.reset(first.db_path)
        assert db.get_drafts(first.db_path) == []
        assert [draft["subject"] for draft in db.get_drafts(second.db_path)] == [
            "second-only"
        ]
        assert httpx.get(
            f"{second.base_url}/api/drafts",
            headers={"X-BTB-UI-Token": second.ui_token},
            timeout=2.0,
        ).json()[0]["subject"] == "second-only"


def test_health_identity_verification_fails_closed() -> None:
    with runtime.managed_run_environment(run_id="identity-run") as environment:
        payload = runtime.verify_fixture_identity(
            environment.base_url,
            environment.db_path,
            run_id="identity-run",
        )
        assert payload["db"] == str(environment.db_path)

        with pytest.raises(runtime.FixtureIdentityError, match="database mismatch"):
            runtime.verify_fixture_identity(
                environment.base_url,
                environment.run_directory / "other.sqlite3",
                run_id="identity-run",
            )
        with pytest.raises(runtime.FixtureIdentityError, match="run ID mismatch"):
            runtime.verify_fixture_identity(
                environment.base_url,
                environment.db_path,
                run_id="different-run",
            )


def test_resources_stop_after_normal_exit() -> None:
    environment = runtime.managed_run_environment(run_id="normal-cleanup")
    with environment:
        assert environment.server_thread.is_alive()
        assert environment.run_directory.exists()

    assert not environment.server_thread.is_alive()
    assert not environment.is_running
    assert environment.socket_closed
    assert not environment.run_directory.exists()
    with pytest.raises(httpx.RequestError):
        httpx.get(f"{environment.base_url}/health", timeout=0.2)


def test_resources_stop_after_exception_exit() -> None:
    environment = runtime.managed_run_environment(run_id="exception-cleanup")

    with pytest.raises(ValueError, match="body failed"):
        with environment:
            assert environment.server_thread.is_alive()
            assert environment.run_directory.exists()
            raise ValueError("body failed")

    assert not environment.server_thread.is_alive()
    assert not environment.is_running
    assert environment.socket_closed
    assert not environment.run_directory.exists()
    with pytest.raises(httpx.RequestError):
        httpx.get(f"{environment.base_url}/health", timeout=0.2)


def test_body_exception_is_not_replaced_by_server_failure() -> None:
    environment = runtime.managed_run_environment(run_id="body-and-server-failure")

    with pytest.raises(ValueError, match="body failed") as captured:
        with environment:
            environment._server_error = RuntimeError("server failed")
            raise ValueError("body failed")

    assert any("server failed" in note for note in captured.value.__notes__)


def test_server_failure_is_reported_on_otherwise_normal_exit() -> None:
    environment = runtime.managed_run_environment(run_id="server-failure")

    with pytest.raises(RuntimeError, match="fixture server failed"):
        with environment:
            environment._server_error = RuntimeError("server failed")


def test_database_paths_are_canonical(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / ".." / "fixture.db"
    assert runtime.canonical_database_path(database_path) == database_path.resolve()
