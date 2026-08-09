"""Prove the checked-in Browser Use semantic-schema fixture is regenerated exactly."""

from __future__ import annotations

import json
from pathlib import Path

from btb.harness import engine
from btb.harness import manifest


_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "browser_use_0136_semantic_policy.json"
)
_ARTIFACT = "btb-browser-use-0136-semantic-schema-fixture-v1"
_FIXTURE_TRAILER = b"\n"
_GENERATION_COMMAND = (
    "python -m pytest -q tests/test_browser_use_policy_fixture.py::"
    "test_browser_use_0136_fixture_matches_installed_post_agent_audit"
)


def _semantic_schema_fixture_payload(
    policies: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Keep only the public schemas whose exact semantics are frozen."""

    conditions: dict[str, object] = {}
    for condition, policy in sorted(policies.items()):
        name, provider_model = condition.split(":", 1)
        provider, model = provider_model.split("/", 1)
        actions = policy.get("actions")
        if not isinstance(actions, list):
            raise AssertionError(f"{condition} did not expose action schemas")
        conditions[condition] = {
            "name": name,
            "provider": provider,
            "model": model,
            "action_model_schema": policy["action_model_schema"],
            "actions": [
                {
                    "name": action["name"],
                    "parameter_schema": action["parameter_schema"],
                }
                for action in actions
            ],
        }
    return {
        "artifact": _ARTIFACT,
        "browser_use_version": "0.13.6",
        "canonical_encoding": "manifest.canonical_json_bytes plus one trailing LF",
        "generation": {
            "source": "installed BrowserTransactionBench candidate wheel post-Agent "
            "constructor audit",
            "command": _GENERATION_COMMAND,
            "browser_use_version": "0.13.6",
        },
        "conditions": conditions,
    }


def test_browser_use_0136_fixture_is_canonical_json() -> None:
    raw = _FIXTURE_PATH.read_bytes()
    assert raw == manifest.canonical_json_bytes(json.loads(raw)) + _FIXTURE_TRAILER


def test_browser_use_0136_fixture_matches_installed_post_agent_audit() -> None:
    """Re-derive schemas through the installed worker, not a source-path import."""

    expected = (
        manifest.canonical_json_bytes(
            _semantic_schema_fixture_payload(engine.audit_browser_use_installation())
        )
        + _FIXTURE_TRAILER
    )
    assert _FIXTURE_PATH.read_bytes() == expected
