"""One planned managed run, executed inside its parent-owned process group."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

from btb.harness import engine, repetition

_REQUEST_FIELDS = {
    "baseline",
    "provider",
    "model",
    "max_steps",
    "run_id",
    "task",
    "receipt_dir",
    "source",
    "runtime",
}


def _request() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("invalid parent request") from exc
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise ValueError("parent request shape is invalid")
    return dict(value)


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return value


def main() -> int:
    try:
        request = _request()
        runtime = repetition.RuntimeIdentity.from_plan(request["runtime"])
        if runtime != repetition.RuntimeIdentity.current():
            raise ValueError("worker runtime differs from the parent plan")
        source = repetition.PlanSource.from_plan(request["source"])
        task = request["task"]
        if not isinstance(task, dict):
            raise TypeError("task is invalid")
        baseline = _required_string(request["baseline"], "baseline")
        run_id = _required_string(request["run_id"], "run_id")
        receipt_dir = Path(_required_string(request["receipt_dir"], "receipt_dir"))
        provider, model, max_steps = (
            request["provider"],
            request["model"],
            request["max_steps"],
        )
        if provider is not None and not isinstance(provider, str):
            raise ValueError("provider is invalid")
        if model is not None and not isinstance(model, str):
            raise ValueError("model is invalid")
        if max_steps is not None and (
            isinstance(max_steps, bool) or not isinstance(max_steps, int)
        ):
            raise ValueError("max_steps is invalid")
        builder = engine.receipt_builder_for(
            task=task,
            run_id=run_id,
            baseline=baseline,
            provider=provider,
            model=model,
            max_steps=max_steps,
            options=engine.ReceiptOptions(mode="canonical", out_dir=receipt_dir),
            source=source.to_provenance(),
            release=source.release,
        )
        engine.run_managed(
            baseline=baseline,
            task=task,
            run_id=run_id,
            provider=provider,
            model=model,
            max_steps=max_steps,
            receipt_options=engine.ReceiptOptions(mode="canonical", out_dir=receipt_dir),
            receipt_builder=builder,
        )
    except Exception:  # noqa: BLE001 - never leak request/environment data
        # The parent owns terminal fallback receipts. Never print task/provider
        # inputs or inherited environment values that could carry credentials.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
