#!/usr/bin/env python3
"""Pilot runner CLI for BrowserTransactionBench.

Usage:
  python run_pilot.py --baseline playwright-exact [--baseline playwright-naive] [--baseline browser-use] [--task msg_send_01] [--model gpt-4o]
  python run_pilot.py --list

Assumes the app server is reachable (default http://127.0.0.1:7788); the engine
spawns the disconnect proxy itself when a task needs it.

Result manifests are written to ./manifests/<run_id>.json (the authoritative
receipts). A compact per-run summary is printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from btb.harness import engine
from btb.harness import manifest as manifest_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner

ROOT = Path(__file__).resolve().parent
PILOT_TASKS = ["msg_read_01", "msg_draft_save_01", "msg_send_01"]
DEFAULT_DB = Path(ROOT) / "btb" / "app" / "btb.db"


def ensure_server(base_url: str, health_path: str = "/health") -> bool:
    import httpx

    try:
        r = httpx.get(base_url + health_path, timeout=2)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _summarize(engine_result: dict, run_id: str, baseline: str) -> dict:
    return {
        "run_id": run_id,
        "task": engine_result["task"],
        "baseline": baseline,
        "outcome": engine_result["outcome"],
        "agent_claimed_send": engine_result.get("claimed_send",
                                                 engine_result.get("claim", {}).get("claimed_send")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="append", default=[],
                    choices=["playwright-exact", "playwright-naive", "browser-use"],
                    help="baseline(s) to run; repeat for multiple")
    ap.add_argument("--task", action="append", default=[],
                    help="task id(s) to run; default all pilot tasks")
    ap.add_argument("--model", default=None, help="model for browser-use baseline")
    ap.add_argument("--provider", default=None,
                    help="LLM provider for browser-use (deepseek|openai|anthropic)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to the fixture DB")
    ap.add_argument("--base-url", default=engine.DEFAULT_BASE_URL)
    ap.add_argument("--list", action="store_true", help="list pilot tasks")
    ap.add_argument("--max-steps", type=int, default=None)
    args = ap.parse_args()

    if args.list:
        for t in PILOT_TASKS:
            d = task_runner.load_definition(t)
            print(f"{t}: {d['effect_class']} | {d['instruction'][:60]}...")
        return 0

    baselines = args.baseline or ["playwright-exact"]
    tasks = args.task or PILOT_TASKS

    if not ensure_server(args.base_url):
        print(f"[pilot] app server not reachable at {args.base_url}. "
              f"Start it (BTB_DB={args.db} python btb/app/server.py) and retry.")
        return 1

    results = []
    for task_id in tasks:
        task = task_runner.load_definition(task_id)
        for baseline in baselines:
            run_id = manifest_mod.new_run_id(f"btb-{task_id}")
            print(f"\n=== {baseline} / {task_id} ({run_id}) ===")
            t0 = time.time()
            if baseline == "browser-use":
                res = engine.run_browser_use(
                    task=task,
                    base_url=args.base_url,
                    db_path=args.db,
                    run_id=run_id,
                    model=args.model,
                    provider=args.provider,
                    max_steps=args.max_steps,
                )
            else:
                behavior = "exact" if baseline.endswith("exact") else "naive_retry"
                res = engine.run_playwright(
                    task=task,
                    base_url=args.base_url,
                    db_path=args.db,
                    behavior=behavior,
                    run_id=run_id,
                )
            elapsed = time.time() - t0
            summary = _summarize(res, run_id, baseline)
            summary["duration_s"] = round(elapsed, 2)
            results.append(summary)
            print(json.dumps(summary, indent=2))

    print("\n--- PILOT SUMMARY ---")
    print("task,baseline,outcome,claimed_send")
    for r in results:
        print(f"{r['task']},{r['baseline']},{r['outcome']},{r['agent_claimed_send']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
