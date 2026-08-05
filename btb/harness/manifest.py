"""Result manifest writer.

Every run writes a machine-readable JSON manifest that ties together the
authoritative DB state, the agent's claim, the injection, and outcome class.
These are the receipts (PROTOCOL.md §11) and are used to regenerate paper
tables. Nothing is dropped on failure.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def git_commit() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def write_manifest(
    *,
    run_id: str,
    freeze: str,
    baseline: str,
    model: str,
    task_id: str,
    task: dict,
    db_state: dict,
    agent_claim: dict,
    injection: dict,
    outcome: str,
    duration_s: float,
    version: str = "1",
    out_dir: Path | None = None,
) -> Path:
    out_dir = out_dir or (REPO_ROOT / "manifests")
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": version,
        "run_id": run_id,
        "freeze": freeze,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline": baseline,
        "baseline_model": model,
        "task": {
            "id": task_id,
            "effect_class": task.get("effect_class"),
            "instruction": task.get("instruction"),
            "failure_injection": task.get("failure_injection"),
            "builtin_scoring_context": task.get("scoring_context"),
        },
        "authoritative_db_state": db_state,
        "agent_claim": agent_claim,
        "injection_report": injection,
        "outcome_class": outcome,
        "duration_s": round(duration_s, 3),
        "git_commit": git_commit(),
    }
    path = out_dir / f"{run_id}.json"
    with open(path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    return path


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
