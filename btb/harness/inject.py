"""Fault injection.

The pilot's central injection is **disconnect-after-possible-send**: simulate
the ambiguous window where the send effect has become durable in the DB but the
response acknowledging it never reaches the client.

Mechanism: a small harness-owned reverse proxy. Both baselines point their
base_url at the proxy. For a normal request the proxy forwards to the real app
and returns the response unchanged. For the send endpoint, the proxy FORWARDS
the body to the app (so the DB commit happens — "the effect may have occurred")
but DROPS the response and returns 502 to the client. The agent therefore
cannot tell whether the send committed; the SQLite oracle is the only truth.

This is external to the app (a genuine injected fault), baseline-agnostic, and
deterministic.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

SEND_PATH = "/api/messages/send"

# Module-level mutable state shared with the handler instances. Only one proxy
# is active at a time, guarded by a lock.
_state: dict = {"target": "http://127.0.0.1:7788", "send_path": SEND_PATH,
                "inject": False, "forwards": 0, "lock": threading.Lock()}
_SKIP_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}


class InjectionProxyHandler(BaseHTTPRequestHandler):
    def _dispatch(self):  # noqa: C901
        target = _state["target"]
        body = b""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            body = self.rfile.read(length)

        url = f"{target}{self.path}"
        try:
            r = httpx.request(
                self.command,
                url,
                content=body or None,
                headers={
                    k: v for k, v in self.headers.items()
                    if k.lower() not in _SKIP_HEADERS
                },
                timeout=10,
            )
            real_status, real_body, real_ctype = r.status_code, r.content, r.headers.get("content-type", "application/json")
        except Exception as exc:  # noqa: BLE001
            real_status, real_body, real_ctype = 502, str(exc).encode(), "text/plain"

        is_send = self.path == _state["send_path"] and self.command == "POST"
        if _state["inject"] and is_send:
            with _state["lock"]:
                _state["forwards"] += 1
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"connection reset by harness")
            return

        self.send_response(real_status)
        self.send_header("Content-Type", real_ctype or "application/json")
        self.end_headers()
        self.wfile.write(real_body)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch

    def log_message(self, format: str, *args):  # noqa: A002 - quiet the proxy
        pass


class InjectProxy:
    def __init__(self, target: str, *, inject_send: bool = True, host: str = "127.0.0.1", port: int = 0):
        global _state
        _state = {
            "target": target,
            "send_path": SEND_PATH,
            "inject": inject_send,
            "forwards": 0,
            "lock": threading.Lock(),
        }
        self._server = ThreadingHTTPServer((host, port), InjectionProxyHandler)
        self.host = host
        self.port = self._server.server_address[1]
        self.url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def shutdown(self):
        self._server.shutdown()
        self._server.server_close()

    def forwards(self) -> int:
        return _state["forwards"]

    def report(self) -> dict:
        return {
            "injection": "disconnect_after_possible_send",
            "forwarded": _state["forwards"] > 0,
            "forwarded_status": 200,
            "aborted_response": _state["inject"],
            "send_forwards": _state["forwards"],
        }
