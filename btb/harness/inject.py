"""Harness-owned reverse proxy for verified post-commit connection loss.

The injector forwards a send to the fixture first and only drops the downstream
connection when the *actual* successful upstream JSON response proves that a
message committed.  Every configured send receives an immutable attempt receipt
so manifests can distinguish a delivered treatment from an upstream rejection
or network failure.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx

SEND_PATH = "/api/messages/send"
_SKIP_HEADERS = frozenset(
    {"host", "content-length", "connection", "transfer-encoding"}
)
_TREATMENTS = (
    "none",
    "response_forwarded",
    "downstream_disconnected_before_response",
    "connection_dropped_after_commit",
)


@dataclass(frozen=True)
class _UpstreamResult:
    """Fully-consumed upstream response (or a represented network failure)."""

    status: int | None
    body: bytes
    content_type: str | None
    completed_at: float
    classification: Literal[
        "committed", "duplicate_rejected", "rejected", "network_error"
    ]
    committed_message_id: int | str | None = None
    committed_send_uid: str | None = None
    error: str | None = None

    @property
    def is_verified_commit(self) -> bool:
        return self.classification == "committed"


class _ProxyHTTPServer(ThreadingHTTPServer):
    """HTTP server that accounts for accepted work before spawning a thread."""

    # Request handlers are short-lived because every downstream response closes
    # its connection.  Non-daemon threads let server_close() join them rather
    # than leaking work beyond this proxy's lifecycle.
    daemon_threads = False

    def __init__(
        self,
        server_address: tuple[str, int],
        owner: InjectProxy,
    ) -> None:
        self.owner = owner
        super().__init__(server_address, InjectionProxyHandler)

    def process_request(self, request, client_address) -> None:
        # Increment in the accepting thread, before ThreadingMixIn starts the
        # handler.  This closes the race where quiescence could otherwise be
        # reported while an accepted request was waiting to enter its handler.
        self.owner._request_accepted()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.owner._request_finished()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.owner._request_finished()


class InjectionProxyHandler(BaseHTTPRequestHandler):
    """HTTP transport adapter; policy and receipts live on ``InjectProxy``."""

    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> InjectProxy:
        return cast(_ProxyHTTPServer, self.server).owner

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _dispatch(self) -> None:
        proxy = self._proxy
        method = self.command
        raw_path = self.path
        is_send = method == "POST" and urlsplit(raw_path).path == proxy.send_path
        request = proxy._new_request(method, raw_path)
        attempt = proxy._new_attempt(request) if is_send else None
        body = b""
        treatment = "none"
        upstream: _UpstreamResult | None = None
        downstream_close_error: str | None = None

        try:
            body = self._read_body()
            upstream = proxy._forward(
                method=method,
                path=raw_path,
                body=body,
                headers=dict(self.headers.items()),
                is_send=is_send,
            )

            if (
                is_send
                and upstream.is_verified_commit
                and attempt is not None
            ):
                committed_sequence = proxy._register_verified_commit()
                attempt["committed_sequence"] = committed_sequence
                if proxy._should_inject(committed_sequence):
                    treatment = "connection_dropped_after_commit"
                    downstream_close_error = self._drop_downstream_connection()
                    return

            downstream_close_error = self._forward_downstream(upstream)
            treatment = (
                "response_forwarded"
                if downstream_close_error is None
                else "downstream_disconnected_before_response"
            )
        finally:
            proxy._record_request(
                request=request,
                upstream=upstream,
                treatment=treatment,
            )
            if attempt is not None:
                proxy._record_attempt(
                    attempt=attempt,
                    body=body,
                    upstream=upstream,
                    treatment=treatment,
                    downstream_close_error=downstream_close_error,
                )

    def _forward_downstream(self, upstream: _UpstreamResult) -> str | None:
        # A missing upstream status denotes a real network failure.  The proxy
        # returns a gateway error, while the receipt retains status=None rather
        # than inventing an upstream status.
        status = upstream.status if upstream.status is not None else 502
        self.close_connection = True
        try:
            self.send_response(status)
            if upstream.content_type is not None:
                self.send_header("Content-Type", upstream.content_type)
            self.send_header("Content-Length", str(len(upstream.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if upstream.body:
                self.wfile.write(upstream.body)
        except OSError as exc:
            # Browser automation may close immediately after a click. The
            # upstream result is still durable, but acknowledgement delivery was
            # not observed and must not be mislabeled as response_forwarded.
            return f"{exc.__class__.__name__}: {exc}"
        return None

    def _drop_downstream_connection(self) -> str | None:
        """Emit no HTTP bytes and force the client-side transport to fail."""
        self.close_connection = True
        errors: list[str] = []
        try:
            # A zero linger timeout asks the kernel to reset rather than
            # gracefully acknowledge a response that the proxy never emitted.
            self.connection.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
        except OSError as exc:
            errors.append(f"setsockopt: {exc.__class__.__name__}: {exc}")
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            # The peer may already have closed.  Closing below still guarantees
            # that this handler cannot send a synthetic status or body.
            errors.append(f"shutdown: {exc.__class__.__name__}: {exc}")
        try:
            self.connection.close()
        except OSError as exc:
            errors.append(f"close: {exc.__class__.__name__}: {exc}")
        return "; ".join(errors) or None

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


class InjectProxy:
    """One isolated reverse proxy and its complete injection receipt state."""

    def __init__(
        self,
        target: str,
        *,
        inject_send: bool = True,
        inject_after_committed: int = 1,
        host: str = "127.0.0.1",
        port: int = 0,
        send_path: str = SEND_PATH,
        upstream_timeout: float = 10.0,
    ) -> None:
        if inject_after_committed < 1:
            raise ValueError("inject_after_committed must be at least 1")
        self.target = target.rstrip("/")
        self.inject_send = inject_send
        self.inject_after_committed = inject_after_committed
        self.send_path = send_path
        self.upstream_timeout = upstream_timeout

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._in_flight = 0
        self._next_sequence = 1
        self._next_request_sequence = 1
        self._committed_count = 0
        self._send_forwards = 0
        # Receipts are appended only after finalization and are never mutated.
        # report() returns fresh dictionary copies so callers cannot alter them.
        self._attempts: list[dict[str, object]] = []
        self._requests: list[dict[str, object]] = []
        self._started = False
        self._closed = False

        self._client = httpx.Client(timeout=upstream_timeout)
        try:
            self._server = _ProxyHTTPServer((host, port), self)
        except BaseException:
            self._client.close()
            raise
        self.host = host
        self.port = int(self._server.server_address[1])
        self.url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"btb-inject-proxy-{self.port}",
            daemon=True,
        )

    def start(self) -> InjectProxy:
        with self._condition:
            if self._closed:
                raise RuntimeError("cannot start a closed injection proxy")
            if self._started:
                raise RuntimeError("injection proxy has already been started")
            self._started = True
            self._thread.start()
        return self

    def shutdown(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            started = self._started

        try:
            if started:
                self._server.shutdown()
            self._server.server_close()
            if started:
                self._thread.join()
        finally:
            self._client.close()

    def __enter__(self) -> InjectProxy:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, exc_value, traceback
        self.shutdown()
        return False

    def _request_accepted(self) -> None:
        with self._condition:
            self._in_flight += 1

    def _request_finished(self) -> None:
        with self._condition:
            self._in_flight -= 1
            if self._in_flight < 0:
                raise RuntimeError("injection proxy in-flight accounting underflow")
            self._condition.notify_all()

    def wait_for_quiescence(self, timeout: float | None = 10.0) -> bool:
        """Wait until every request accepted by this proxy has completed."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        with self._condition:
            return self._condition.wait_for(lambda: self._in_flight == 0, timeout)

    def _new_request(self, method: str, path: str) -> dict[str, object]:
        with self._condition:
            sequence = self._next_request_sequence
            self._next_request_sequence += 1
        return {
            "sequence": sequence,
            "method": method,
            "path": path,
            "started_at": time.time(),
        }

    def _new_attempt(self, request: dict[str, object]) -> dict[str, object]:
        with self._condition:
            sequence = self._next_sequence
            self._next_sequence += 1
        return {
            "sequence": sequence,
            "request_sequence": request["sequence"],
            "method": request["method"],
            "path": request["path"],
            "started_at": request["started_at"],
            "committed_sequence": None,
        }

    def _register_verified_commit(self) -> int:
        with self._condition:
            self._committed_count += 1
            return self._committed_count

    def _should_inject(self, committed_sequence: int) -> bool:
        """Treat only the configured Nth verified durable commit."""
        return (
            self.inject_send
            and committed_sequence == self.inject_after_committed
        )

    def _forward(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
        is_send: bool,
    ) -> _UpstreamResult:
        if is_send:
            with self._condition:
                self._send_forwards += 1

        forwarded_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in _SKIP_HEADERS
        }
        try:
            response = self._client.request(
                method,
                f"{self.target}{path}",
                content=body,
                headers=forwarded_headers,
            )
            try:
                status = response.status_code
                response_body = response.content
                content_type = response.headers.get("content-type")
            finally:
                response.close()
        except httpx.RequestError as exc:
            completed_at = time.time()
            error = f"{exc.__class__.__name__}: {exc}"
            return _UpstreamResult(
                status=None,
                body=f"upstream network error: {error}".encode("utf-8"),
                content_type="text/plain; charset=utf-8",
                completed_at=completed_at,
                classification="network_error",
                error=error,
            )

        completed_at = time.time()
        payload = _parse_json_object(response_body)
        message_id, send_uid = _committed_identity(payload)
        response_claims_commit = (
            payload is not None and payload.get("committed") is True
        )
        if payload is not None and payload.get("duplicate_rejected") is True:
            classification: Literal[
                "committed", "duplicate_rejected", "rejected", "network_error"
            ] = "duplicate_rejected"
        elif (
            200 <= status < 300
            and response_claims_commit
            and message_id is not None
            and send_uid is not None
        ):
            classification = "committed"
        else:
            classification = "rejected"

        return _UpstreamResult(
            status=status,
            body=response_body,
            content_type=content_type,
            completed_at=completed_at,
            classification=classification,
            committed_message_id=message_id if response_claims_commit else None,
            committed_send_uid=send_uid if response_claims_commit else None,
        )

    def _record_attempt(
        self,
        *,
        attempt: dict[str, object],
        body: bytes,
        upstream: _UpstreamResult | None,
        treatment: str,
        downstream_close_error: str | None,
    ) -> None:
        request_payload = _parse_json_object(body)
        request_draft_id = request_payload.get("draft_id") if request_payload else None
        request_send_uid = request_payload.get("send_uid") if request_payload else None
        if isinstance(request_draft_id, bool) or not isinstance(request_draft_id, int):
            request_draft_id = None
        if not isinstance(request_send_uid, str):
            request_send_uid = None

        receipt: dict[str, object] = {
            **attempt,
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "request_draft_id": request_draft_id,
            "request_send_uid": request_send_uid,
            "upstream_completed_at": (
                upstream.completed_at if upstream is not None else None
            ),
            "upstream_status": upstream.status if upstream is not None else None,
            "upstream_classification": (
                upstream.classification if upstream is not None else "network_error"
            ),
            "committed_message_id": (
                upstream.committed_message_id if upstream is not None else None
            ),
            "committed_send_uid": (
                upstream.committed_send_uid if upstream is not None else None
            ),
            "treatment": treatment,
            "finished_at": time.time(),
        }
        if upstream is not None and upstream.error is not None:
            receipt["upstream_error"] = upstream.error
        if downstream_close_error is not None:
            receipt["downstream_close_error"] = downstream_close_error

        # Copy at the ownership boundary.  The finalized object retained here is
        # never exposed directly and is never changed after append.
        with self._condition:
            self._attempts.append(dict(receipt))

    def _record_request(
        self,
        *,
        request: dict[str, object],
        upstream: _UpstreamResult | None,
        treatment: str,
    ) -> None:
        receipt = {
            **request,
            "upstream_completed_at": (
                upstream.completed_at if upstream is not None else None
            ),
            "upstream_status": upstream.status if upstream is not None else None,
            "treatment": treatment,
            "finished_at": time.time(),
        }
        with self._condition:
            self._requests.append(receipt)

    def forwards(self) -> int:
        with self._condition:
            return self._send_forwards

    def report(self) -> dict:
        """Return a detached, JSON-safe snapshot of treatment and attempt state."""
        with self._condition:
            attempts = [
                dict(attempt)
                for attempt in sorted(
                    self._attempts,
                    key=lambda item: cast(int, item["sequence"]),
                )
            ]
            requests = [
                dict(request)
                for request in sorted(
                    self._requests,
                    key=lambda item: cast(int, item["sequence"]),
                )
            ]
            in_flight = self._in_flight
            send_forwards = self._send_forwards

        treatment_summary = {name: 0 for name in _TREATMENTS}
        for attempt in attempts:
            treatment = cast(str, attempt["treatment"])
            treatment_summary[treatment] += 1
        treatment_delivered = (
            treatment_summary["connection_dropped_after_commit"] > 0
        )
        statuses = [attempt["upstream_status"] for attempt in attempts]

        return {
            "injection": "disconnect_after_possible_send",
            "injection_enabled": self.inject_send,
            "inject_after_committed": self.inject_after_committed,
            "forwarded": send_forwards > 0,
            # Compatibility field, now the latest *actual* upstream status.
            "forwarded_status": statuses[-1] if statuses else None,
            "upstream_statuses": statuses,
            "aborted_response": treatment_delivered,
            "treatment_delivered": treatment_delivered,
            "send_forwards": send_forwards,
            "in_flight": in_flight,
            "treatment_summary": treatment_summary,
            "attempts": attempts,
            "requests": requests,
        }


def _parse_json_object(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _committed_identity(
    payload: dict[str, object] | None,
) -> tuple[int | str | None, str | None]:
    if payload is None:
        return None, None

    message = payload.get("message")
    message_object = message if isinstance(message, dict) else {}
    message_id = message_object.get("id", payload.get("message_id"))
    send_uid = message_object.get("send_uid", payload.get("send_uid"))

    if (
        isinstance(message_id, bool)
        or not isinstance(message_id, (int, str))
        or (isinstance(message_id, str) and not message_id)
    ):
        message_id = None
    if not isinstance(send_uid, str) or not send_uid:
        send_uid = None
    return message_id, send_uid
