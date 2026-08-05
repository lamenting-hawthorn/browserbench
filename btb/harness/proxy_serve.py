#!/usr/bin/env python3
"""Stand up a long-lived disconnect-after-possible-send proxy for the
reachability test, so an external acting agent (e.g. a vision subagent) can
drive the app through the ambiguous-injection path.

The proxy listens on 127.0.0.1:<PROXY_PORT> (default 7799), forwards everything
to the app (BTB app, default 7788), and for POST /api/messages/send: forwards the
request (durable DB commit) then drops the response with a 502 — the agent
cannot tell whether the send landed.

Usage:
    python -m btb.harness.proxy_serve --port 7799 --target http://127.0.0.1:7788
"""
from __future__ import annotations

import argparse
import time

from btb.harness.inject import InjectProxy


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=7799)
    ap.add_argument("--target", default="http://127.0.0.1:7788")
    args = ap.parse_args()

    proxy = InjectProxy(args.target, inject_send=True, port=args.port).start()
    print(f"proxy_up url={proxy.url}->{args.target} inject_send=True", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        proxy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
