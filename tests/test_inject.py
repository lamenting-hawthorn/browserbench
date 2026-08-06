"""Tests for verified post-commit connection-loss injection receipts."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from btb.app import db
from btb.harness.inject import InjectProxy, SEND_PATH
from btb.harness.runtime import managed_run_environment


@dataclass(frozen=True)
class _Reply:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json"
    drop_connection: bool = False


class _ScriptedUpstream:
    """Small real HTTP upstream for response-shape and coordination tests."""

    def __init__(
        self,
        reply: _Reply,
        *,
        request_entered: threading.Event | None = None,
        release_response: threading.Event | None = None,
    ) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length:
                    self.rfile.read(length)
                if request_entered is not None:
                    request_entered.set()
                if release_response is not None:
                    release_response.wait(2.0)
                if owner.reply.drop_connection:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        # A peer close already produces the intended upstream
                        # network failure; close remains deterministic below.
                        pass
                    self.connection.close()
                    return
                self.send_response(owner.reply.status)
                self.send_header("Content-Type", owner.reply.content_type)
                self.send_header("Content-Length", str(len(owner.reply.body)))
                self.end_headers()
                self.wfile.write(owner.reply.body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                del format, args

        self.reply = reply
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"test-scripted-upstream-{port}",
            daemon=True,
        )

    def __enter__(self) -> _ScriptedUpstream:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(2.0)
        assert not self._thread.is_alive()


def _seed_saved_draft(database_path: Path | str) -> None:
    db.seed_draft(
        database_path,
        draft_id=1,
        subject="receipt test",
        body="durable body",
        status="saved",
    )


def _ui_headers(environment) -> dict[str, str]:
    return {"X-BTB-UI-Token": environment.ui_token}


def test_no_injection_forwards_verified_send_and_records_receipt() -> None:
    request_body = b'{"draft_id":1,"send_uid":"forwarded-uid"}'
    with managed_run_environment(run_id="inject-forward") as environment:
        _seed_saved_draft(environment.db_path)
        with InjectProxy(environment.base_url, inject_send=False) as proxy:
            with httpx.Client(timeout=2.0) as client:
                health = client.get(f"{proxy.url}/health")
                assert health.status_code == 200
                assert health.json()["run_id"] == "inject-forward"
                health.close()

                response = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    content=request_body,
                    headers={
                        "Content-Type": "application/json",
                        **_ui_headers(environment),
                    },
                )
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("application/json")
                payload = response.json()
                response.close()

            assert payload["committed"] is True
            assert db.count_sent(environment.db_path) == 1
            assert proxy.wait_for_quiescence(timeout=1.0)
            report = proxy.report()

            assert report["treatment_delivered"] is False
            assert report["forwarded_status"] == 200
            assert report["upstream_statuses"] == [200]
            assert report["send_forwards"] == 1
            event = report["attempts"][0]
            assert event["sequence"] == 1
            assert event["method"] == "POST"
            assert event["path"] == SEND_PATH
            assert event["request_body_sha256"] == hashlib.sha256(request_body).hexdigest()
            assert event["request_draft_id"] == 1
            assert event["request_send_uid"] == "forwarded-uid"
            assert event["upstream_status"] == 200
            assert event["upstream_classification"] == "committed"
            assert event["committed_message_id"] == 1
            assert event["committed_send_uid"] == "forwarded-uid"
            assert event["treatment"] == "response_forwarded"
            assert (
                event["started_at"]
                <= event["upstream_completed_at"]
                <= event["finished_at"]
            )
            json.dumps(report)

            # Reports are detached snapshots; callers cannot mutate receipts.
            event["treatment"] = "none"
            assert proxy.report()["attempts"][0]["treatment"] == "response_forwarded"


def test_injection_drops_only_after_verified_commit() -> None:
    with managed_run_environment(run_id="inject-drop") as environment:
        _seed_saved_draft(environment.db_path)
        with InjectProxy(environment.base_url, inject_send=True) as proxy:
            with httpx.Client(timeout=2.0) as client:
                with pytest.raises(httpx.RequestError):
                    client.post(
                        f"{proxy.url}{SEND_PATH}",
                        json={"draft_id": 1, "send_uid": "dropped-uid"},
                        headers=_ui_headers(environment),
                    )

            assert proxy.wait_for_quiescence(timeout=1.0)
            assert db.count_sent(environment.db_path) == 1
            assert db.messages(environment.db_path)[0]["send_uid"] == "dropped-uid"
            report = proxy.report()
            assert report["treatment_delivered"] is True
            assert report["aborted_response"] is True
            assert report["treatment_summary"] == {
                "none": 0,
                "response_forwarded": 0,
                "downstream_disconnected_before_response": 0,
                "connection_dropped_after_commit": 1,
            }
            event = report["attempts"][0]
            assert event["upstream_status"] == 200
            assert event["upstream_classification"] == "committed"
            assert event["committed_message_id"] == 1
            assert event["committed_send_uid"] == "dropped-uid"
            assert event["treatment"] == "connection_dropped_after_commit"


def test_injection_is_delivered_only_on_configured_attempt() -> None:
    with managed_run_environment(run_id="inject-once") as environment:
        _seed_saved_draft(environment.db_path)
        with InjectProxy(
            environment.base_url,
            inject_send=True,
            inject_after_committed=1,
        ) as proxy:
            with httpx.Client(timeout=2.0) as client:
                with pytest.raises(httpx.RequestError):
                    client.post(
                        f"{proxy.url}{SEND_PATH}",
                        json={"draft_id": 1, "send_uid": "first-uid"},
                        headers=_ui_headers(environment),
                    )
                second = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    json={"draft_id": 1, "send_uid": "second-uid"},
                    headers=_ui_headers(environment),
                )
                assert second.status_code == 200
                assert second.json()["committed"] is True

            assert proxy.wait_for_quiescence(timeout=1.0)
            report = proxy.report()
            assert report["inject_after_committed"] == 1
            assert report["treatment_summary"] == {
                "none": 0,
                "response_forwarded": 1,
                "downstream_disconnected_before_response": 0,
                "connection_dropped_after_commit": 1,
            }
            assert [event["treatment"] for event in report["attempts"]] == [
                "connection_dropped_after_commit",
                "response_forwarded",
            ]
            assert db.count_sent(environment.db_path) == 2


def test_rejection_does_not_consume_configured_committed_treatment() -> None:
    with managed_run_environment(run_id="inject-after-commit") as environment:
        _seed_saved_draft(environment.db_path)
        with InjectProxy(
            environment.base_url,
            inject_send=True,
            inject_after_committed=1,
        ) as proxy:
            with httpx.Client(timeout=2.0) as client:
                rejected = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    json={"draft_id": 404, "send_uid": "rejected-first"},
                    headers=_ui_headers(environment),
                )
                assert rejected.status_code == 404
                with pytest.raises(httpx.RequestError):
                    client.post(
                        f"{proxy.url}{SEND_PATH}",
                        json={"draft_id": 1, "send_uid": "first-commit"},
                        headers=_ui_headers(environment),
                    )

            assert proxy.wait_for_quiescence(timeout=1.0)
            report = proxy.report()
            assert [event["committed_sequence"] for event in report["attempts"]] == [
                None,
                1,
            ]
            assert [event["treatment"] for event in report["attempts"]] == [
                "response_forwarded",
                "connection_dropped_after_commit",
            ]


def test_real_fixture_rejections_and_duplicate_are_forwarded_normally() -> None:
    with managed_run_environment(run_id="inject-rejections") as environment:
        _seed_saved_draft(environment.db_path)
        db.send_message(environment.db_path, draft_id=1, send_uid="existing-uid")

        with InjectProxy(environment.base_url, inject_send=True) as proxy:
            with httpx.Client(timeout=2.0) as client:
                missing = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    json={"draft_id": 404, "send_uid": "missing-draft"},
                    headers=_ui_headers(environment),
                )
                malformed = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    content=b"{not-json",
                    headers={
                        "Content-Type": "application/json",
                        **_ui_headers(environment),
                    },
                )
                duplicate = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    json={"draft_id": 1, "send_uid": "existing-uid"},
                    headers=_ui_headers(environment),
                )

                assert missing.status_code == 404
                assert missing.json()["detail"] == "draft not found"
                assert malformed.status_code == 422
                assert duplicate.status_code == 200
                assert duplicate.json()["duplicate_rejected"] is True
                missing.close()
                malformed.close()
                duplicate.close()

            assert proxy.wait_for_quiescence(timeout=1.0)
            assert db.count_sent(environment.db_path) == 1
            report = proxy.report()
            assert report["treatment_delivered"] is False
            assert report["aborted_response"] is False
            assert report["upstream_statuses"] == [404, 422, 200]
            assert report["forwarded_status"] == 200
            assert [event["upstream_classification"] for event in report["attempts"]] == [
                "rejected",
                "rejected",
                "duplicate_rejected",
            ]
            assert {event["treatment"] for event in report["attempts"]} == {
                "response_forwarded"
            }


@pytest.mark.parametrize(
    ("reply", "classification"),
    [
        (_Reply(status=202, body=b"{}"), "rejected"),
        (
            _Reply(
                status=200,
                body=b'{"committed":true,"message":{"id":9}}',
            ),
            "rejected",
        ),
        (_Reply(status=201, body=b"not-json"), "rejected"),
        (
            _Reply(
                status=409,
                body=b'{"committed":false,"duplicate_rejected":false}',
            ),
            "rejected",
        ),
    ],
)
def test_unverified_or_malformed_upstream_response_is_not_treated(
    reply: _Reply,
    classification: str,
) -> None:
    with _ScriptedUpstream(reply) as upstream:
        with InjectProxy(upstream.url, inject_send=True) as proxy:
            with httpx.Client(timeout=2.0) as client:
                response = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    json={"draft_id": 1, "send_uid": "unverified"},
                )
                assert response.status_code == reply.status
                assert response.content == reply.body
                response.close()

            assert proxy.wait_for_quiescence(timeout=1.0)
            report = proxy.report()
            assert report["forwarded_status"] == reply.status
            assert report["upstream_statuses"] == [reply.status]
            assert report["treatment_delivered"] is False
            event = report["attempts"][0]
            assert event["upstream_status"] == reply.status
            assert event["upstream_classification"] == classification
            assert event["treatment"] == "response_forwarded"


def test_upstream_network_failure_has_no_invented_status_or_treatment() -> None:
    reply = _Reply(drop_connection=True)
    with _ScriptedUpstream(reply) as upstream:
        with InjectProxy(upstream.url, inject_send=True) as proxy:
            with httpx.Client(timeout=2.0) as client:
                response = client.post(
                    f"{proxy.url}{SEND_PATH}",
                    json={"draft_id": 1, "send_uid": "network-error"},
                )
                assert response.status_code == 502
                response.close()

            assert proxy.wait_for_quiescence(timeout=1.0)
            report = proxy.report()
            assert report["forwarded_status"] is None
            assert report["upstream_statuses"] == [None]
            assert report["treatment_delivered"] is False
            event = report["attempts"][0]
            assert event["upstream_status"] is None
            assert event["upstream_classification"] == "network_error"
            assert event["treatment"] == "response_forwarded"


def test_two_proxies_do_not_share_targets_counters_or_events() -> None:
    first_environment = managed_run_environment(run_id="proxy-isolation-first")
    second_environment = managed_run_environment(run_id="proxy-isolation-second")
    with first_environment, second_environment:
        _seed_saved_draft(first_environment.db_path)
        _seed_saved_draft(second_environment.db_path)

        with (
            InjectProxy(first_environment.base_url, inject_send=False) as first,
            InjectProxy(second_environment.base_url, inject_send=True) as second,
        ):
            with httpx.Client(timeout=2.0) as client:
                response = client.post(
                    f"{first.url}{SEND_PATH}",
                    json={"draft_id": 1, "send_uid": "first-only"},
                    headers=_ui_headers(first_environment),
                )
                assert response.status_code == 200
                response.close()

            assert first.wait_for_quiescence(timeout=1.0)
            assert second.wait_for_quiescence(timeout=1.0)
            assert first.forwards() == 1
            assert second.forwards() == 0
            assert len(first.report()["attempts"]) == 1
            assert second.report()["attempts"] == []
            assert db.count_sent(first_environment.db_path) == 1
            assert db.count_sent(second_environment.db_path) == 0


def test_quiescence_times_out_while_request_is_in_flight_then_completes() -> None:
    request_entered = threading.Event()
    release_response = threading.Event()
    reply = _Reply(status=202, body=b'{"committed":false}')
    client_statuses: list[int] = []
    client_errors: list[Exception] = []

    with _ScriptedUpstream(
        reply,
        request_entered=request_entered,
        release_response=release_response,
    ) as upstream:
        with InjectProxy(upstream.url, inject_send=False) as proxy:

            def make_request() -> None:
                try:
                    with httpx.Client(timeout=2.0) as client:
                        response = client.post(
                            f"{proxy.url}{SEND_PATH}",
                            json={"draft_id": 1, "send_uid": "blocked"},
                        )
                        client_statuses.append(response.status_code)
                        response.close()
                except Exception as exc:  # surfaced in the owning test thread
                    client_errors.append(exc)

            client_thread = threading.Thread(target=make_request, daemon=True)
            client_thread.start()
            try:
                assert request_entered.wait(1.0)
                assert proxy.report()["in_flight"] == 1
                assert proxy.wait_for_quiescence(timeout=0.01) is False
            finally:
                release_response.set()

            assert proxy.wait_for_quiescence(timeout=1.0) is True
            client_thread.join(1.0)
            assert not client_thread.is_alive()
            assert client_errors == []
            assert client_statuses == [202]
            assert proxy.report()["in_flight"] == 0
