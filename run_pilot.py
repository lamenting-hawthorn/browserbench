#!/usr/bin/env python3
"""Pilot runner CLI for BrowserTransactionBench.

Managed runs start a fresh, isolated fixture server and SQLite database for
each task/baseline pair. ``--mode canonical`` additionally fails closed unless
the exact source is committed and clean. ``--external-server`` is an explicit
compatibility mode and fails closed unless that server reports the requested
database.

Receipts are written under ``manifests/exploratory/current`` or
``manifests/canonical``. A compact per-run summary is printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from btb.harness import engine
from btb.harness import manifest as manifest_mod
from btb.harness import runtime
from btb.tasks import runner as task_runner

ROOT = Path(__file__).resolve().parent
PILOT_TASKS = task_runner.PILOT_TASKS
DEFAULT_DB = ROOT / "btb" / "app" / "btb.db"


def verify_external_server(base_url: str, database_path: Path | str) -> None:
    """Verify external fixture DB identity before allowing any reset or score."""
    runtime.verify_fixture_identity(
        base_url,
        database_path,
        verify_run_id=False,
    )


def _summarize(engine_result: dict, run_id: str, baseline: str) -> dict:
    summary = {
        "status": engine_result.get("status", "failure"),
        "run_id": run_id,
        "task": engine_result.get("task"),
        "baseline": baseline,
        "outcome": engine_result.get("outcome"),
        "agent_claimed_send": engine_result.get(
            "claimed_send",
            engine_result.get("claim", {}).get("claimed_send"),
        ),
        "receipt_path": engine_result.get("receipt_path"),
    }
    if engine_result.get("status") != "success":
        summary["error"] = engine_result.get("error")
        if engine_result.get("receipt_error") is not None:
            summary["receipt_error"] = engine_result["receipt_error"]
    return summary


def _execute_baseline(
    *,
    baseline: str,
    task: dict,
    run_id: str,
    base_url: str,
    database_path: Path,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    receipt_options: engine.ReceiptOptions,
) -> dict:
    return engine.run_external(
        baseline=baseline,
        task=task,
        run_id=run_id,
        base_url=base_url,
        db_path=database_path,
        model=model,
        provider=provider,
        max_steps=max_steps,
        receipt_options=receipt_options,
    )


def _run_managed(
    *,
    baseline: str,
    task: dict,
    run_id: str,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    receipt_options: engine.ReceiptOptions,
) -> dict:
    return engine.run_managed(
        baseline=baseline,
        task=task,
        run_id=run_id,
        model=model,
        provider=provider,
        max_steps=max_steps,
        receipt_options=receipt_options,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        action="append",
        default=[],
        choices=["playwright-exact", "playwright-naive", "browser-use"],
        help="baseline(s) to run; repeat for multiple",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        choices=PILOT_TASKS,
        help="task id(s) to run; default all pilot tasks",
    )
    parser.add_argument("--model", default=None, help="model for browser-use baseline")
    parser.add_argument(
        "--provider",
        default=None,
        choices=["deepseek", "openai", "anthropic"],
        help="LLM provider for browser-use (deepseek|openai|anthropic)",
    )
    parser.add_argument(
        "--mode",
        choices=["exploratory", "canonical"],
        default="exploratory",
        help="receipt mode; canonical requires an exact clean Git source",
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=None,
        help="override receipt/artifact output directory",
    )
    parser.add_argument(
        "--external-server",
        action="store_true",
        help="use an existing server after verifying its database identity",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"external-server database path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"external-server URL (default: {engine.DEFAULT_BASE_URL})",
    )
    parser.add_argument("--list", action="store_true", help="list pilot tasks")
    parser.add_argument("--max-steps", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list:
        for task_id in PILOT_TASKS:
            definition = task_runner.load_definition(task_id)
            print(
                f"{task_id}: {definition['effect_class']} | "
                f"{definition['instruction'][:60]}..."
            )
        return 0

    if not args.external_server and (args.db is not None or args.base_url is not None):
        parser.error("--db and --base-url require --external-server")

    external_database: Path | None = None
    external_base_url: str | None = None
    if args.external_server:
        external_database = runtime.canonical_database_path(args.db or DEFAULT_DB)
        external_base_url = str(args.base_url or engine.DEFAULT_BASE_URL)

    if args.max_steps is not None and args.max_steps < 1:
        parser.error("--max-steps must be positive")

    baselines = args.baseline or ["playwright-exact"]
    tasks = args.task or PILOT_TASKS
    results: list[dict] = []
    receipt_options = engine.ReceiptOptions(
        mode=args.mode,
        out_dir=args.receipt_dir,
    )

    for task_id in tasks:
        task = task_runner.load_definition(task_id)
        for baseline in baselines:
            run_id = manifest_mod.new_run_id(f"btb-{task_id}")
            print(f"\n=== {baseline} / {task_id} ({run_id}) ===")
            started_at = time.time()

            if args.external_server:
                assert external_base_url is not None
                assert external_database is not None
                result = _execute_baseline(
                    baseline=baseline,
                    task=task,
                    run_id=run_id,
                    base_url=external_base_url,
                    database_path=external_database,
                    model=args.model,
                    provider=args.provider,
                    max_steps=args.max_steps,
                    receipt_options=receipt_options,
                )
            else:
                result = _run_managed(
                    baseline=baseline,
                    task=task,
                    run_id=run_id,
                    model=args.model,
                    provider=args.provider,
                    max_steps=args.max_steps,
                    receipt_options=receipt_options,
                )

            summary = _summarize(result, run_id, baseline)
            summary["duration_s"] = round(time.time() - started_at, 2)
            results.append(summary)
            print(json.dumps(summary, indent=2))

    print("\n--- PILOT SUMMARY ---")
    print("task,baseline,status,outcome,claimed_send")
    for result in results:
        print(
            f"{result['task']},{result['baseline']},{result['status']},"
            f"{result['outcome']},"
            f"{result['agent_claimed_send']}"
        )
    return 1 if any(result["status"] != "success" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
