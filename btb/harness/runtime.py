"""Managed, per-run application runtime for benchmark fixtures.

Canonical benchmark runs use :class:`ManagedRunEnvironment` instead of a
shared developer server. Each environment owns its temporary directory,
SQLite database, listening socket, Uvicorn server, and server thread.
"""

from __future__ import annotations

import socket
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Literal

import httpx
import uvicorn

from btb.app.server import create_app

_LOCALHOST = "127.0.0.1"


class FixtureHealthError(RuntimeError):
    """Base class for a fixture that cannot prove its expected identity."""


class FixtureUnavailableError(FixtureHealthError):
    """The fixture health endpoint could not provide a usable response."""


class FixtureIdentityError(FixtureHealthError):
    """The fixture health endpoint describes a different run or database."""


def canonical_database_path(database_path: Path | str) -> Path:
    """Return the absolute, symlink-resolved path used in health identities."""
    return Path(database_path).expanduser().resolve()


def _read_health(base_url: str, *, timeout: float) -> dict[str, object]:
    health_url = f"{base_url.rstrip('/')}/health"
    try:
        response = httpx.get(health_url, timeout=timeout)
    except httpx.RequestError as exc:
        raise FixtureUnavailableError(f"fixture is not reachable at {health_url}") from exc

    if response.status_code != 200:
        raise FixtureUnavailableError(
            f"fixture health check returned HTTP {response.status_code} at {health_url}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise FixtureUnavailableError(
            f"fixture health check did not return JSON at {health_url}"
        ) from exc
    if not isinstance(payload, dict):
        raise FixtureUnavailableError(
            f"fixture health check did not return an object at {health_url}"
        )
    return payload


def verify_fixture_identity(
    base_url: str,
    database_path: Path | str,
    *,
    run_id: str | None = None,
    verify_run_id: bool = True,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Fail closed unless ``/health`` identifies the requested fixture exactly.

    External servers may not belong to one manifest run, so callers can disable
    only the run-ID comparison. Database identity and the healthy flag are
    always checked.
    """
    payload = _read_health(base_url, timeout=timeout)
    if payload.get("ok") is not True:
        raise FixtureIdentityError("fixture health response did not report ok=true")

    expected_database = str(canonical_database_path(database_path))
    actual_database = payload.get("db")
    if actual_database != expected_database:
        raise FixtureIdentityError(
            "fixture database mismatch: "
            f"expected {expected_database!r}, received {actual_database!r}"
        )

    if verify_run_id and payload.get("run_id") != run_id:
        raise FixtureIdentityError(
            "fixture run ID mismatch: "
            f"expected {run_id!r}, received {payload.get('run_id')!r}"
        )
    return payload


class ManagedRunEnvironment:
    """Context manager owning one isolated in-process fixture server."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        startup_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if run_id is not None and not run_id.strip():
            raise ValueError("run_id must not be empty")
        self.run_id = run_id or f"btb-run-{uuid.uuid4().hex}"
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout

        self._temporary_directory: tempfile.TemporaryDirectory | None = None
        self._run_directory: Path | None = None
        self._database_path: Path | None = None
        self._base_url: str | None = None
        self._ui_token = secrets.token_urlsafe(32)
        self._listener: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._server_error: BaseException | None = None
        self._entered = False

    @property
    def run_directory(self) -> Path:
        if self._run_directory is None:
            raise RuntimeError("managed environment has not been entered")
        return self._run_directory

    @property
    def db_path(self) -> Path:
        if self._database_path is None:
            raise RuntimeError("managed environment has not been entered")
        return self._database_path

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise RuntimeError("managed environment has not been entered")
        return self._base_url

    @property
    def ui_token(self) -> str:
        return self._ui_token

    @property
    def server_thread(self) -> threading.Thread:
        if self._server_thread is None:
            raise RuntimeError("managed environment has not been entered")
        return self._server_thread

    @property
    def is_running(self) -> bool:
        return self._server_thread is not None and self._server_thread.is_alive()

    @property
    def socket_closed(self) -> bool:
        return self._listener is None or self._listener.fileno() == -1

    def __enter__(self) -> ManagedRunEnvironment:
        if self._entered:
            raise RuntimeError("managed environment instances are single-use")
        self._entered = True

        try:
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="btb-run-")
            self._run_directory = Path(self._temporary_directory.name).resolve()
            self._database_path = self._run_directory / "fixture.sqlite3"

            # Pre-binding makes port ownership atomic: no other process can take
            # the selected ephemeral port between discovery and Uvicorn startup.
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((_LOCALHOST, 0))
            self._listener.listen(2048)
            port = int(self._listener.getsockname()[1])
            self._base_url = f"http://{_LOCALHOST}:{port}"

            application = create_app(
                self._database_path,
                run_id=self.run_id,
                ui_token=self._ui_token,
            )
            config = uvicorn.Config(
                application,
                host=_LOCALHOST,
                port=port,
                log_level="warning",
                access_log=False,
                lifespan="on",
            )
            self._server = uvicorn.Server(config)
            self._server_thread = threading.Thread(
                target=self._serve,
                name=f"btb-fixture-{self.run_id}",
                daemon=True,
            )
            self._server_thread.start()
            self._wait_until_ready()
            return self
        except BaseException:
            # Setup failures receive the same deterministic teardown as body
            # failures; the exception itself is re-raised after cleanup.
            try:
                self._stop_server()
            finally:
                self._cleanup_directory()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, traceback
        stop_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            self._stop_server()
        except BaseException as server_stop_error:
            stop_error = server_stop_error
        try:
            self._cleanup_directory()
        except BaseException as directory_error:
            cleanup_error = directory_error

        secondary_errors = [
            error
            for error in (stop_error, cleanup_error)
            if error is not None
        ]
        if self._server_error is not None:
            server_error = RuntimeError(
                "fixture server failed while the environment was active"
            )
            server_error.__cause__ = self._server_error
            secondary_errors.append(server_error)

        if secondary_errors:
            if exc_value is not None:
                if hasattr(exc_value, "add_note"):
                    for error in secondary_errors:
                        exc_value.add_note(
                            f"managed fixture teardown also failed: "
                            f"{error.__class__.__name__}: {error}"
                        )
                return False
            primary = secondary_errors[0]
            if hasattr(primary, "add_note"):
                for error in secondary_errors[1:]:
                    primary.add_note(
                        f"additional teardown failure: "
                        f"{error.__class__.__name__}: {error}"
                    )
            raise primary
        return False

    def _serve(self) -> None:
        assert self._server is not None
        assert self._listener is not None
        try:
            self._server.run(sockets=[self._listener])
        except BaseException as exc:
            # Thread exceptions cannot propagate directly. Retain the exact
            # failure so startup/teardown can report it to the owning thread.
            self._server_error = exc

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_unavailable: FixtureUnavailableError | None = None
        while time.monotonic() < deadline:
            if self._server_thread is not None and not self._server_thread.is_alive():
                if self._server_error is not None:
                    raise RuntimeError("fixture server stopped during startup") from self._server_error
                raise RuntimeError("fixture server stopped during startup")
            try:
                verify_fixture_identity(
                    self.base_url,
                    self.db_path,
                    run_id=self.run_id,
                    timeout=min(0.25, self.startup_timeout),
                )
                return
            except FixtureUnavailableError as exc:
                last_unavailable = exc
                time.sleep(0.02)

        raise TimeoutError(
            f"fixture did not become healthy within {self.startup_timeout:.1f}s"
        ) from last_unavailable

    def _stop_server(self) -> None:
        server = self._server
        thread = self._server_thread
        listener = self._listener

        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(self.shutdown_timeout)

        if thread is not None and thread.is_alive():
            # Closing the pre-bound socket unblocks any lingering accept before
            # asking Uvicorn to take its force-exit path.
            if listener is not None:
                listener.close()
            if server is not None:
                server.force_exit = True
            thread.join(self.shutdown_timeout)

        if listener is not None:
            listener.close()
        if thread is not None and thread.is_alive():
            raise RuntimeError("fixture server thread did not stop")

    def _cleanup_directory(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()


def managed_run_environment(
    *,
    run_id: str | None = None,
    startup_timeout: float = 5.0,
    shutdown_timeout: float = 5.0,
) -> ManagedRunEnvironment:
    """Create a single-use managed fixture context for one benchmark run."""
    return ManagedRunEnvironment(
        run_id=run_id,
        startup_timeout=startup_timeout,
        shutdown_timeout=shutdown_timeout,
    )
