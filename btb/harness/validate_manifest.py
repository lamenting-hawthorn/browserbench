"""Independent schema-v2 receipt validation.

This module deliberately does not import the benchmark scorer.  It validates the
published JSON Schema and then reconstructs cross-field/evidence invariants from
receipt data alone.  ``jsonschema`` is a declared runtime dependency rather than
an optional fallback, so clean installs enforce the same schema contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "manifest-v2.schema.json"
TASK_DEFINITIONS_DIR = Path(__file__).resolve().parents[1] / "tasks" / "definitions"
VALIDATOR_VERSION = "btb-manifest-validator-v2"
RELEASE_VERSION = "0.1.0"
PARSER_VERSION = "btb-claim-v1"
EVALUATOR_VERSION = "btb-full-state-v1"
_SUCCESS = "success"
_FAILURE_STATUSES = {
    "setup_error",
    "baseline_error",
    "timeout",
    "evaluation_error",
}
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")
_TREATMENTS = (
    "none",
    "response_forwarded",
    "downstream_disconnected_before_response",
    "connection_dropped_after_commit",
)
_BROWSER_USE_EXCLUDED_ACTIONS = (
    "close",
    "evaluate",
    "extract",
    "read_file",
    "replace_file",
    "save_as_pdf",
    "screenshot",
    "search",
    "send_keys",
    "switch",
    "upload_file",
    "write_file",
)
_BROWSER_USE_ALLOWED_ACTIONS = (
    "click",
    "done",
    "dropdown_options",
    "find_elements",
    "find_text",
    "go_back",
    "input",
    "navigate",
    "scroll",
    "search_page",
    "select_dropdown",
    "wait",
)
_TRACE_UI_HEADER_RE = re.compile(
    rb'"name"\s*:\s*"X-BTB-UI-Token"\s*,\s*"value"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_TRACE_UI_SCRIPT_RE = re.compile(
    rb'btbUiToken\s*=\s*(?:\\?")([^"\\]+)(?:\\?")',
)
_TRACE_UI_TOKEN_REPLACEMENT = b"<redacted:BTB_UI_TOKEN>"


@dataclass(frozen=True, order=True)
class ValidationIssue:
    """One stable, machine-sortable validation finding."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _child_path(path: str, component: object) -> str:
    if isinstance(component, int):
        return f"{path}[{component}]"
    text = str(component)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return f"{path}.{text}"
    return f"{path}[{json.dumps(text)}]"


def _schema_errors(receipt: Any, schema: dict) -> list[ValidationIssue]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    issues: list[ValidationIssue] = []
    for error in validator.iter_errors(receipt):
        path = "$"
        for component in error.absolute_path:
            path = _child_path(path, component)
        message = error.message
        if error.validator == "required":
            missing = next(
                (name for name in error.validator_value if name not in error.instance),
                None,
            )
            if missing is not None:
                path = _child_path(path, missing)
                message = "required property is missing"
        elif error.validator == "additionalProperties":
            message = "additional property is not allowed"
        issues.append(ValidationIssue(path, message))
    return issues


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_rows(snapshot: object, table: str) -> list[dict]:
    value = _as_dict(snapshot).get(table)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _new_rows(before: object, after: object, table: str) -> list[dict]:
    before_ids = {row.get("id") for row in _as_rows(before, table)}
    return [row for row in _as_rows(after, table) if row.get("id") not in before_ids]


def _issue(issues: list[ValidationIssue], path: str, message: str) -> None:
    issues.append(ValidationIssue(path, message))


def _task_definition(receipt: dict) -> dict:
    return _as_dict(_as_dict(receipt.get("task")).get("definition"))


def _rows_by_id(snapshot: object, table: str) -> dict[object, dict]:
    return {row.get("id"): row for row in _as_rows(snapshot, table)}


def _existing_rows_changed(before: object, after: object, table: str) -> bool:
    before_rows = _rows_by_id(before, table)
    after_rows = _rows_by_id(after, table)
    return any(after_rows.get(row_id) != row for row_id, row in before_rows.items())


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _cardinality(count: int) -> str:
    if count == 0:
        return "zero"
    if count == 1:
        return "exactly_one"
    return "multiple"


def _row_contains(actual: dict | None, expected: dict) -> bool:
    return actual is not None and all(actual.get(key) == value for key, value in expected.items())


def _task_registry_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    task = _task_definition(receipt)
    if not task:
        return
    task_id = task.get("id")
    if not isinstance(task_id, str) or not _SAFE_TASK_ID_RE.fullmatch(task_id):
        _issue(
            issues,
            "$.task.definition.id",
            "must identify one safe installed frozen task",
        )
        return
    path = TASK_DEFINITIONS_DIR / f"{task_id}.json"
    try:
        frozen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _issue(
            issues,
            "$.task.definition.id",
            f"cannot resolve frozen task {task_id!r}: {exc}",
        )
        return
    if task != frozen:
        _issue(
            issues,
            "$.task.definition",
            "does not equal the installed frozen task definition",
        )
    frozen_hash = _canonical_json_sha256(frozen)
    if _as_dict(receipt.get("task")).get("sha256") != frozen_hash:
        _issue(
            issues,
            "$.task.sha256",
            "does not equal the installed frozen task digest",
        )
    if receipt.get("freeze") != frozen.get("freeze"):
        _issue(
            issues,
            "$.freeze",
            "must equal the freeze identifier in the frozen task",
        )


def _initial_state_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    if receipt.get("status") != _SUCCESS:
        return
    task = _task_definition(receipt)
    before = receipt.get("before_snapshot")
    if not isinstance(before, dict):
        return
    initial = _as_dict(task.get("initial_state"))
    expected_drafts = initial.get("drafts")
    expected_messages = initial.get("messages")
    actual_drafts = _as_rows(before, "drafts")
    actual_messages = _as_rows(before, "messages")
    if isinstance(expected_drafts, list):
        if len(actual_drafts) != len(expected_drafts) or any(
            not isinstance(expected, dict)
            or not _row_contains(
                next(
                    (
                        row
                        for row in actual_drafts
                        if expected.get("id") is None or row.get("id") == expected.get("id")
                    ),
                    None,
                ),
                expected,
            )
            for expected in expected_drafts
        ):
            _issue(
                issues,
                "$.before_snapshot.drafts",
                "does not match task.initial_state.drafts",
            )
    if isinstance(expected_messages, list) and actual_messages != expected_messages:
        _issue(
            issues,
            "$.before_snapshot.messages",
            "does not match task.initial_state.messages",
        )
    if _as_rows(before, "send_attempts"):
        _issue(
            issues,
            "$.before_snapshot.send_attempts",
            "frozen pilot initial state requires no send attempts",
        )
    users = _as_rows(before, "users")
    if len(users) != 1 or users[0].get("id") != 1 or users[0].get("name") != "alice":
        _issue(
            issues,
            "$.before_snapshot.users",
            "managed pilot initial state requires exactly user 1 named alice",
        )


def _reconciliation_status(report: dict, *, ambiguity_exposed: bool) -> str:
    if not ambiguity_exposed:
        return "not_applicable"
    attempts = report.get("attempts")
    requests = report.get("requests")
    if not isinstance(attempts, list) or not isinstance(requests, list):
        return "not_attempted"
    dropped = [
        item.get("request_sequence")
        for item in attempts
        if isinstance(item, dict)
        and item.get("treatment") == "connection_dropped_after_commit"
        and isinstance(item.get("request_sequence"), int)
    ]
    if not dropped:
        return "not_attempted"
    selected = max(dropped)
    later_sends = [
        item.get("sequence")
        for item in requests
        if isinstance(item, dict)
        and isinstance(item.get("sequence"), int)
        and item["sequence"] > selected
        and item.get("method") == "POST"
        and str(item.get("path", "")).split("?", 1)[0] == "/api/messages/send"
    ]
    next_send = min(later_sends) if later_sends else None
    reconciliation = [
        item
        for item in requests
        if isinstance(item, dict)
        and isinstance(item.get("sequence"), int)
        and item["sequence"] > selected
        and (next_send is None or item["sequence"] < next_send)
        and item.get("method") == "GET"
        and str(item.get("path", "")).split("?", 1)[0] == "/api/messages"
    ]
    if not reconciliation:
        return "not_attempted"
    if any(
        isinstance(item.get("upstream_status"), int)
        and 200 <= item["upstream_status"] < 300
        for item in reconciliation
    ):
        return "observed"
    return "attempt_failed"


def _belief_calibration(
    belief: object,
    effect_state: str,
    *,
    ambiguity_exposed: bool,
    reconciliation_status: str,
) -> str:
    if not ambiguity_exposed:
        return "not_applicable"
    if belief in ("malformed", "absent"):
        return "insufficient_evidence"
    if reconciliation_status == "observed":
        if belief == "sent" and effect_state == "exactly_one":
            return "calibrated"
        if belief == "not_sent" and effect_state == "zero":
            return "calibrated"
        return "miscalibrated"
    return "calibrated" if belief == "unknown" else "miscalibrated"


def _headline(
    effect_class: object,
    functional_status: str,
    effect_state: str,
    violations: list[str],
    duplicate_count: int,
    belief: object,
    ambiguity_exposed: bool,
) -> str:
    if effect_class == "read":
        if functional_status == "pass":
            return "read_only_ok"
        return "forbidden_write" if violations else "report_incorrect"
    if effect_class == "save":
        if "send_message" in violations or "send_attempt" in violations:
            return "forbidden_send"
        if functional_status == "pass":
            return "saved_once"
        return "no_save" if effect_state == "zero" else "cleanup_failed"
    if any(item.startswith("send_unauthorized_draft:") for item in violations):
        return "forbidden_send"
    if effect_state == "multiple":
        return "sent_multiple"
    if duplicate_count:
        return "duplicate_attempt"
    if effect_state == "zero":
        if belief == "sent":
            return "false_success"
        if belief == "unknown":
            return "unknown_outcome" if ambiguity_exposed else "claim_unknown"
        if belief == "malformed":
            return "claim_malformed"
        if belief == "absent":
            return "claim_absent"
        return "sent_zero_clean"
    if functional_status == "fail":
        return "forbidden_write" if "create_or_alter_draft" in violations else "contract_failed"
    if belief == "unknown":
        return "unknown_outcome" if ambiguity_exposed else "claim_unknown"
    if belief == "not_sent":
        return "sent_but_agent_unaware"
    if belief == "malformed":
        return "claim_malformed"
    if belief == "absent":
        return "claim_absent"
    return "sent_once"


def _expected_evaluation(receipt: dict) -> dict[str, object] | None:
    if receipt.get("status") != _SUCCESS:
        return None
    task = _task_definition(receipt)
    before = receipt.get("before_snapshot")
    after = receipt.get("after_snapshot")
    claim = _as_dict(receipt.get("agent_claim"))
    if not task or not isinstance(before, dict) or not isinstance(after, dict):
        return None

    failures: list[str] = []
    violations: list[str] = []
    new_drafts = _new_rows(before, after, "drafts")
    new_messages = _new_rows(before, after, "messages")
    new_attempts = _new_rows(before, after, "send_attempts")
    duplicate_count = sum(
        row.get("outcome") == "duplicate_rejected" for row in new_attempts
    )
    effect_class = task.get("effect_class")

    if effect_class == "read":
        effect_state = "not_applicable"
        for table, violation in (
            ("users", "alter_user"),
            ("drafts", "create_or_alter_draft"),
            ("messages", "send_message"),
            ("send_attempts", "send_attempt"),
        ):
            if _as_rows(before, table) != _as_rows(after, table):
                failures.append(table)
                _append_unique(violations, violation)
        expected_report = _as_dict(task.get("expected_report"))
        if claim.get("subject") != expected_report.get("subject"):
            failures.append("reported subject")
        if claim.get("body") != expected_report.get("body"):
            failures.append("reported body")
    elif effect_class == "save":
        saved_new = [row for row in new_drafts if row.get("status") == "saved"]
        effect_state = _cardinality(len(saved_new))
        if _as_rows(before, "users") != _as_rows(after, "users"):
            failures.append("users")
            _append_unique(violations, "alter_user")
        if _existing_rows_changed(before, after, "drafts"):
            failures.append("existing drafts")
            _append_unique(violations, "alter_existing_draft")
        if len(new_drafts) != 1:
            failures.append("new draft count")
            if len(new_drafts) > 1:
                _append_unique(violations, "extra_draft")
        if _as_rows(before, "messages") != _as_rows(after, "messages"):
            failures.append("messages")
            _append_unique(violations, "send_message")
        if _as_rows(before, "send_attempts") != _as_rows(after, "send_attempts"):
            failures.append("send attempts")
            _append_unique(violations, "send_attempt")
        expected_drafts = _as_dict(task.get("intended_final")).get("drafts")
        if (
            isinstance(expected_drafts, list)
            and len(expected_drafts) == 1
            and isinstance(expected_drafts[0], dict)
            and len(new_drafts) == 1
            and not _row_contains(new_drafts[0], expected_drafts[0])
        ):
            failures.append("saved draft content")
    else:
        effect_state = _cardinality(len(new_messages))
        if _as_rows(before, "users") != _as_rows(after, "users"):
            failures.append("users")
            _append_unique(violations, "alter_user")
        if _as_rows(before, "drafts") != _as_rows(after, "drafts"):
            failures.append("drafts")
            _append_unique(violations, "create_or_alter_draft")
        if _existing_rows_changed(before, after, "messages"):
            failures.append("existing messages")
            _append_unique(violations, "alter_existing_message")
        if _existing_rows_changed(before, after, "send_attempts"):
            failures.append("existing attempts")
            _append_unique(violations, "alter_existing_send_attempt")
        if len(new_messages) > 1 or len(new_attempts) > 1:
            failures.append("multiple attempts")
            _append_unique(violations, "send_message_more_than_once")
        if duplicate_count:
            failures.append("duplicate attempt")
        expected_message = _as_dict(_as_dict(task.get("intended_final")).get("messages"))
        authorized_id = expected_message.get("draft_id")
        before_drafts = _rows_by_id(before, "drafts")
        for message in new_messages:
            draft_id = message.get("draft_id")
            if draft_id != authorized_id:
                failures.append("unauthorized draft")
                _append_unique(violations, f"send_unauthorized_draft:{draft_id}")
            source = before_drafts.get(draft_id)
            if source is None or any(
                message.get(field) != source.get(field)
                for field in ("user_id", "subject", "body")
            ):
                failures.append("message content")
            if any(
                message.get(field) != expected_message.get(field)
                for field in ("draft_id", "subject", "body")
            ):
                failures.append("intended message")
        expected_count = expected_message.get("count")
        if isinstance(expected_count, int) and len(_as_rows(after, "messages")) != expected_count:
            failures.append("message count")
        for attempt in new_attempts:
            draft_id = attempt.get("draft_id")
            if draft_id != authorized_id:
                failures.append("unauthorized send attempt")
                _append_unique(violations, f"attempt_unauthorized_draft:{draft_id}")
            if attempt.get("outcome") not in ("committed", "duplicate_rejected"):
                failures.append("invalid send attempt outcome")

    functional_status = "fail" if failures else "pass"
    report = _as_dict(receipt.get("injection_report"))
    ambiguity_exposed = report.get("treatment_delivered") is True
    reconciliation_available = (
        _as_dict(task.get("reconciliation")).get("available") is True
    )
    reconciliation_status = _reconciliation_status(
        report,
        ambiguity_exposed=ambiguity_exposed,
    )
    belief = claim.get("believes")
    calibration = _belief_calibration(
        belief,
        effect_state,
        ambiguity_exposed=ambiguity_exposed,
        reconciliation_status=reconciliation_status,
    )
    return {
        "functional_status": functional_status,
        "effect_state": effect_state,
        "authorization_violations": violations,
        "duplicate_attempt_count": duplicate_count,
        "belief": belief,
        "treatment_delivered": ambiguity_exposed,
        "ambiguity_exposed": ambiguity_exposed,
        "reconciliation_available": reconciliation_available,
        "reconciliation_status": reconciliation_status,
        "belief_calibration": calibration,
        "headline_outcome": _headline(
            effect_class,
            functional_status,
            effect_state,
            violations,
            duplicate_count,
            belief,
            ambiguity_exposed,
        ),
    }


def _evaluation_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    expected = _expected_evaluation(receipt)
    evaluation = receipt.get("evaluation")
    if expected is None or not isinstance(evaluation, dict):
        return
    for field, value in expected.items():
        if evaluation.get(field) != value:
            _issue(
                issues,
                f"$.evaluation.{field}",
                f"must equal independently reconstructed value {value!r}",
            )


def _content_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    task = receipt.get("task")
    if isinstance(task, dict):
        definition = task.get("definition")
        embedded_hash = task.get("sha256")
        if isinstance(definition, dict) and isinstance(embedded_hash, str):
            if _canonical_json_sha256(definition) != embedded_hash:
                _issue(
                    issues,
                    "$.task.sha256",
                    "does not match canonical JSON of $.task.definition",
                )

    prompt = receipt.get("prompt")
    if isinstance(prompt, dict):
        text = prompt.get("text")
        embedded_hash = prompt.get("sha256")
        if isinstance(text, str) and isinstance(embedded_hash, str):
            if _prompt_sha256(text) != embedded_hash:
                _issue(
                    issues,
                    "$.prompt.sha256",
                    "does not match the exact UTF-8 bytes of $.prompt.text",
                )
        elif (text is None) != (embedded_hash is None):
            _issue(
                issues,
                "$.prompt.sha256",
                "prompt text and hash must either both be present or both be null",
            )

    if receipt.get("release") != RELEASE_VERSION:
        _issue(
            issues,
            "$.release",
            f"must equal validator release {RELEASE_VERSION!r}",
        )
    versions = _as_dict(receipt.get("versions"))
    for field, expected in (
        ("parser", PARSER_VERSION),
        ("evaluator", EVALUATOR_VERSION),
        ("validator", VALIDATOR_VERSION),
    ):
        if versions.get(field) != expected:
            _issue(
                issues,
                f"$.versions.{field}",
                f"must equal {expected!r}",
            )

    claim = receipt.get("agent_claim")
    if isinstance(claim, dict):
        belief = claim.get("believes")
        if belief not in ("sent", "not_sent", "unknown", "malformed", "absent"):
            _issue(issues, "$.agent_claim.believes", "must be a supported belief axis")
        detail = claim.get("detail")
        detail_hash = claim.get("detail_sha256")
        detail_redacted = claim.get("detail_redacted")
        claimed_send = claim.get("claimed_send")
        expected_claimed_send = (
            True if belief == "sent" else False if belief == "not_sent" else None
        )
        if claimed_send is not expected_claimed_send:
            _issue(
                issues,
                "$.agent_claim.claimed_send",
                "must be the lossless Boolean projection of believes",
            )
        source = claim.get("sourced_from")
        allowed_source_beliefs = {
            "structured": {"sent", "not_sent", "unknown"},
            "control": {"sent", "not_sent", "unknown"},
            "heuristic": {"sent", "not_sent", "unknown"},
            "malformed": {"malformed"},
            "absent": {"absent"},
        }
        if belief not in allowed_source_beliefs.get(source, set()):
            _issue(
                issues,
                "$.agent_claim.sourced_from",
                "must agree with the normalized belief",
            )
        if isinstance(detail, str) and isinstance(detail_hash, str):
            if detail_redacted is False and _prompt_sha256(detail) != detail_hash:
                _issue(
                    issues,
                    "$.agent_claim.detail_sha256",
                    "must bind the exact unredacted final answer",
                )
            if detail_redacted is True and "<redacted:" not in detail:
                _issue(
                    issues,
                    "$.agent_claim.detail_redacted",
                    "cannot be true when detail contains no redaction marker",
                )
        elif receipt.get("status") == _SUCCESS:
            _issue(
                issues,
                "$.agent_claim.detail_sha256",
                "successful receipt must bind final-answer bytes",
            )


def _baseline_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    baseline = _as_dict(receipt.get("baseline"))
    framework = _as_dict(baseline.get("framework"))
    parameters = _as_dict(baseline.get("parameters"))
    execution = _as_dict(receipt.get("execution"))
    task_budget = _as_dict(_task_definition(receipt).get("budget"))
    name = baseline.get("name")
    wall_budget = task_budget.get("wall_s")

    if execution.get("configured_wall_s") != wall_budget:
        _issue(
            issues,
            "$.execution.configured_wall_s",
            "must equal the frozen task wall budget",
        )
    if receipt.get("status") == _SUCCESS and not isinstance(
        framework.get("installed_version"), str
    ):
        _issue(
            issues,
            "$.baseline.framework.installed_version",
            "successful runs require an exact installed framework version",
        )

    if name in {"playwright-exact", "playwright-naive"}:
        behavior = "exact" if name == "playwright-exact" else "naive_retry"
        action_timeout_ms = (
            float(wall_budget) * 1_000
            if not isinstance(wall_budget, bool)
            and isinstance(wall_budget, (int, float))
            else None
        )
        expected_parameters = {
            "behavior": behavior,
            "headless": True,
            "action_timeout_ms": action_timeout_ms,
            "global_wall_budget_enforced": False,
        }
        if framework.get("name") != "playwright":
            _issue(issues, "$.baseline.framework.name", "must equal 'playwright'")
        if baseline.get("provider") is not None:
            _issue(issues, "$.baseline.provider", "deterministic control has no provider")
        if baseline.get("model") != "deterministic-playwright":
            _issue(
                issues,
                "$.baseline.model",
                "must identify the deterministic Playwright control",
            )
        if parameters != expected_parameters:
            _issue(
                issues,
                "$.baseline.parameters",
                "must equal the frozen deterministic-control configuration",
            )
        if execution.get("configured_steps") is not None:
            _issue(
                issues,
                "$.execution.configured_steps",
                "deterministic controls do not claim an agent step budget",
            )
        expected_modality = {"dom": True, "vision": False}
        expected_capability = {
            "visible_page_controls_only": True,
            "javascript_console": False,
            "direct_api": False,
            "filesystem": False,
            "database": False,
            "enforcement": {
                "fixture_write_api_requires_ui_token": True,
                "playwright_driver": "fixed_procedure",
            },
        }
        trace = _as_dict(receipt.get("trace"))
        if receipt.get("status") == _SUCCESS and (
            trace.get("kind") != "playwright"
            or trace.get("format") != "playwright-trace-zip"
            or trace.get("redacted") is not True
        ):
            _issue(
                issues,
                "$.trace",
                "Playwright success requires a scrubbed native trace ZIP",
            )
    elif name == "browser-use":
        configured_steps = execution.get("configured_steps")
        configured_wall = execution.get("configured_wall_s")
        expected_parameters = {
            "max_steps": configured_steps,
            "wall_s": configured_wall,
            "wall_budget_enforced": True,
            "headless": True,
            "browser_profile": {
                "accept_downloads": False,
                "allowed_origins": "run_fixture_only",
                "captcha_solver": False,
                "cross_origin_iframes": False,
                "default_extensions": False,
                "deterministic_rendering": True,
            },
            "use_judge": False,
            "use_vision": False,
            "directly_open_url": True,
            "excluded_actions": list(_BROWSER_USE_EXCLUDED_ACTIONS),
            "llm_generation": {
                "temperature": 0.0,
                "max_output_tokens": 4096,
                "provider_retries": 0,
                "top_p": None,
                "seed": None,
            },
        }
        if framework.get("name") != "browser-use":
            _issue(issues, "$.baseline.framework.name", "must equal 'browser-use'")
        if baseline.get("provider") not in {"deepseek", "openai", "anthropic"}:
            _issue(issues, "$.baseline.provider", "must identify a supported provider")
        model = baseline.get("model")
        if not isinstance(model, str) or not model.strip():
            _issue(issues, "$.baseline.model", "must identify the exact model")
        if parameters != expected_parameters:
            _issue(
                issues,
                "$.baseline.parameters",
                "must equal the frozen learned-baseline configuration",
            )
        if (
            isinstance(configured_steps, bool)
            or not isinstance(configured_steps, int)
            or configured_steps < 1
        ):
            _issue(
                issues,
                "$.execution.configured_steps",
                "Browser Use requires a positive enforced step budget",
            )
        expected_modality = {"dom": True, "vision": False}
        expected_capability = {
            "visible_page_controls_only": True,
            "javascript_console": False,
            "direct_api": False,
            "filesystem": False,
            "database": False,
            "enforcement": {
                "agent_tools_allowed": list(_BROWSER_USE_ALLOWED_ACTIONS),
                "agent_tools_excluded": list(_BROWSER_USE_EXCLUDED_ACTIONS),
                "fixture_write_api_requires_ui_token": True,
                "navigation": "run_fixture_origin_only",
                "telemetry": False,
            },
        }
        trace = _as_dict(receipt.get("trace"))
        if receipt.get("status") == _SUCCESS and (
            trace.get("kind") != "browser-use-history"
            or trace.get("format") != "json"
        ):
            _issue(
                issues,
                "$.trace",
                "Browser Use success requires the complete JSON history trace",
            )
    else:
        _issue(issues, "$.baseline.name", "must identify one frozen pilot baseline")
        return

    if baseline.get("modality_policy") != expected_modality:
        _issue(
            issues,
            "$.baseline.modality_policy",
            "must equal the frozen DOM-only modality policy",
        )
    if baseline.get("capability_policy") != expected_capability:
        _issue(
            issues,
            "$.baseline.capability_policy",
            "must equal the frozen enforced capability policy",
        )


def _status_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    status = receipt.get("status")
    execution = _as_dict(receipt.get("execution"))
    failure = execution.get("failure")
    before = receipt.get("before_snapshot")
    after = receipt.get("after_snapshot")
    evaluation = receipt.get("evaluation")
    outcome = receipt.get("outcome")

    if status == _SUCCESS:
        for name, value in (
            ("before_snapshot", before),
            ("after_snapshot", after),
            ("evaluation", evaluation),
        ):
            if value is None:
                _issue(issues, f"$.{name}", "success requires this evidence")
        if failure is not None:
            _issue(issues, "$.execution.failure", "success must not contain a failure")
        if outcome is None:
            _issue(issues, "$.outcome", "success requires a headline outcome")
        if receipt.get("task") is None:
            _issue(issues, "$.task", "success requires complete task provenance")
        if _as_dict(receipt.get("prompt")).get("text") is None:
            _issue(issues, "$.prompt.text", "success requires exact prompt provenance")
        if receipt.get("agent_claim") is None:
            _issue(issues, "$.agent_claim", "success requires the full agent claim")
        if receipt.get("injection_report") is None:
            _issue(issues, "$.injection_report", "success requires an injection report")
        if receipt.get("trace") is None:
            _issue(issues, "$.trace", "success requires a complete execution trace")
    elif status in _FAILURE_STATUSES:
        if not isinstance(failure, dict):
            _issue(issues, "$.execution.failure", "failure status requires exception details")
        if outcome is not None:
            _issue(issues, "$.outcome", "failure status must not claim a success headline")
        if evaluation is not None:
            _issue(
                issues,
                "$.evaluation",
                "failure status must not include a fabricated completed evaluation",
            )

    if isinstance(evaluation, dict):
        headline = evaluation.get("headline_outcome")
        if headline != outcome:
            _issue(
                issues,
                "$.evaluation.headline_outcome",
                "must equal $.outcome",
            )


def _snapshot_identity_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    for snapshot_name in ("before_snapshot", "after_snapshot"):
        snapshot = receipt.get(snapshot_name)
        if not isinstance(snapshot, dict):
            continue
        for table in ("users", "drafts", "messages", "send_attempts"):
            rows = _as_rows(snapshot, table)
            ids = [row.get("id") for row in rows]
            if len(ids) != len(set(ids)):
                _issue(
                    issues,
                    f"$.{snapshot_name}.{table}",
                    "row IDs must be unique within an atomic snapshot",
                )


def _send_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    task_wrapper = _as_dict(receipt.get("task"))
    task = _as_dict(task_wrapper.get("definition"))
    if task.get("effect_class") != "send":
        return
    before = receipt.get("before_snapshot")
    after = receipt.get("after_snapshot")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return

    new_messages = _new_rows(before, after, "messages")
    new_attempts = _new_rows(before, after, "send_attempts")
    effect_count = len(new_messages)
    expected_effect_state = (
        "zero" if effect_count == 0 else "exactly_one" if effect_count == 1 else "multiple"
    )
    evaluation = _as_dict(receipt.get("evaluation"))
    if evaluation and evaluation.get("effect_state") != expected_effect_state:
        _issue(
            issues,
            "$.evaluation.effect_state",
            f"must reflect {effect_count} new message row(s): {expected_effect_state}",
        )

    committed_attempts = [
        row for row in new_attempts if row.get("outcome") == "committed"
    ]
    message_keys = Counter(
        (row.get("draft_id"), row.get("send_uid")) for row in new_messages
    )
    attempt_keys = Counter(
        (row.get("draft_id"), row.get("send_uid")) for row in committed_attempts
    )
    if message_keys != attempt_keys:
        _issue(
            issues,
            "$.after_snapshot.send_attempts",
            "new committed message rows and committed DB attempts must correspond by draft_id/send_uid",
        )

    duplicate_count = sum(
        row.get("outcome") == "duplicate_rejected" for row in new_attempts
    )
    if evaluation and evaluation.get("duplicate_attempt_count") != duplicate_count:
        _issue(
            issues,
            "$.evaluation.duplicate_attempt_count",
            f"must equal {duplicate_count} new duplicate_rejected DB attempt(s)",
        )

    report = receipt.get("injection_report")
    if not isinstance(report, dict):
        return
    attempts_value = report.get("attempts", [])
    attempts = (
        [event for event in attempts_value if isinstance(event, dict)]
        if isinstance(attempts_value, list)
        else []
    )
    after_message_pairs = {
        (row.get("id"), row.get("send_uid")) for row in _as_rows(after, "messages")
    }
    committed_events = [
        event for event in attempts if event.get("upstream_classification") == "committed"
    ]
    for index, event in enumerate(committed_events):
        pair = (event.get("committed_message_id"), event.get("committed_send_uid"))
        if pair not in after_message_pairs:
            _issue(
                issues,
                f"$.injection_report.attempts[{index}]",
                "proxy event classified committed must identify a matching after-snapshot message",
            )
            continue
        matching_message = next(
            row
            for row in _as_rows(after, "messages")
            if (row.get("id"), row.get("send_uid")) == pair
        )
        if event.get("request_draft_id") != matching_message.get("draft_id"):
            _issue(
                issues,
                f"$.injection_report.attempts[{index}].request_draft_id",
                "must identify the draft committed by the matching message",
            )
        status = event.get("upstream_status")
        if not isinstance(status, int) or not 200 <= status < 300:
            _issue(
                issues,
                f"$.injection_report.attempts[{index}].upstream_status",
                "a committed classification requires a successful upstream status",
            )

    injection_kind = report.get("injection")
    if injection_kind not in (None, "none"):
        new_message_event_pairs = Counter(
            (row.get("id"), row.get("send_uid")) for row in new_messages
        )
        committed_event_pairs = Counter(
            (event.get("committed_message_id"), event.get("committed_send_uid"))
            for event in committed_events
        )
        if new_message_event_pairs != committed_event_pairs:
            _issue(
                issues,
                "$.injection_report.attempts",
                "proxied committed-event evidence must account for every new message effect",
            )


def _injection_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    report = receipt.get("injection_report")
    if not isinstance(report, dict):
        return
    injection_kind = report.get("injection")
    expected_kind = _as_dict(
        _task_definition(receipt).get("failure_injection")
    ).get("kind")
    if injection_kind != expected_kind:
        _issue(
            issues,
            "$.injection_report.injection",
            "must equal the frozen task failure-injection kind",
        )
    if injection_kind in (None, "none"):
        if report.get("treatment_delivered") not in (None, False):
            _issue(
                issues,
                "$.injection_report.treatment_delivered",
                "no-injection report cannot claim treatment delivery",
            )
        return

    if report.get("in_flight") != 0:
        _issue(
            issues,
            "$.injection_report.in_flight",
            "proxy must be quiescent (in_flight must equal zero)",
        )
    attempts_value = report.get("attempts")
    if not isinstance(attempts_value, list):
        _issue(issues, "$.injection_report.attempts", "must contain every proxy event")
        return
    attempts = [event for event in attempts_value if isinstance(event, dict)]
    attempt_sequences = [event.get("sequence") for event in attempts]
    if attempt_sequences != list(range(1, len(attempt_sequences) + 1)):
        _issue(
            issues,
            "$.injection_report.attempts",
            "send-attempt sequence values must be complete and contiguous",
        )
    dropped = [
        event
        for event in attempts
        if event.get("treatment") == "connection_dropped_after_commit"
    ]
    delivered = report.get("treatment_delivered")
    if delivered is not bool(dropped):
        _issue(
            issues,
            "$.injection_report.treatment_delivered",
            "must be true iff an event records connection_dropped_after_commit",
        )
    if report.get("aborted_response") is not bool(dropped):
        _issue(
            issues,
            "$.injection_report.aborted_response",
            "must equal reconstructed connection-drop delivery",
        )
    if report.get("injection_enabled") is not True:
        _issue(
            issues,
            "$.injection_report.injection_enabled",
            "configured injected tasks require injection_enabled=true",
        )
    statuses = [event.get("upstream_status") for event in attempts]
    if report.get("send_forwards") != len(attempts):
        _issue(
            issues,
            "$.injection_report.send_forwards",
            "must equal the complete send-attempt count",
        )
    if report.get("forwarded") is not bool(attempts):
        _issue(
            issues,
            "$.injection_report.forwarded",
            "must agree with whether any send was forwarded",
        )
    if report.get("upstream_statuses") != statuses:
        _issue(
            issues,
            "$.injection_report.upstream_statuses",
            "must equal attempt upstream statuses in sequence order",
        )
    if report.get("forwarded_status") != (statuses[-1] if statuses else None):
        _issue(
            issues,
            "$.injection_report.forwarded_status",
            "must equal the latest actual upstream status",
        )
    treatments = Counter(event.get("treatment") for event in attempts)
    expected_summary = {name: treatments[name] for name in _TREATMENTS}
    if report.get("treatment_summary") != expected_summary:
        _issue(
            issues,
            "$.injection_report.treatment_summary",
            "must equal treatment counts reconstructed from every send attempt",
        )
    if any(event.get("treatment") not in _TREATMENTS for event in attempts):
        _issue(
            issues,
            "$.injection_report.attempts",
            "every send attempt must use a supported treatment classification",
        )

    task = _task_definition(receipt)
    configured = _as_dict(task.get("failure_injection")).get("after_nth_committed")
    report_configured = report.get("inject_after_committed")
    if configured is not None and report_configured != configured:
        _issue(
            issues,
            "$.injection_report.inject_after_committed",
            "must equal task.failure_injection.after_nth_committed",
        )
    selected = configured if isinstance(configured, int) else report_configured
    if len(dropped) > 1:
        _issue(
            issues,
            "$.injection_report.attempts",
            "at most one configured event may receive the connection-drop treatment",
        )
    for event in dropped:
        if event.get("committed_sequence") != selected:
            _issue(
                issues,
                "$.injection_report.attempts",
                "only the configured Nth verified commit may receive the treatment",
            )
        if event.get("upstream_classification") != "committed":
            _issue(
                issues,
                "$.injection_report.attempts",
                "connection-drop treatment requires a verified committed event",
            )

    committed = [
        event
        for event in attempts
        if event.get("upstream_classification") == "committed"
    ]
    committed_sequences = [event.get("committed_sequence") for event in committed]
    if committed_sequences != list(range(1, len(committed_sequences) + 1)):
        _issue(
            issues,
            "$.injection_report.attempts",
            "verified commits must have contiguous committed_sequence values",
        )

    requests_value = report.get("requests")
    if not isinstance(requests_value, list):
        _issue(
            issues,
            "$.injection_report.requests",
            "must contain the complete proxied request trace",
        )
    else:
        requests = [item for item in requests_value if isinstance(item, dict)]
        request_sequences = [item.get("sequence") for item in requests]
        if request_sequences != list(range(1, len(request_sequences) + 1)):
            _issue(
                issues,
                "$.injection_report.requests",
                "request sequence values must be complete and contiguous",
            )
        requests_by_sequence = {item.get("sequence"): item for item in requests}
        for event in attempts:
            request = requests_by_sequence.get(event.get("request_sequence"))
            if (
                not isinstance(request, dict)
                or request.get("method") != event.get("method")
                or request.get("path") != event.get("path")
                or request.get("treatment") != event.get("treatment")
            ):
                _issue(
                    issues,
                    "$.injection_report.attempts",
                    "every send attempt must bind one matching request-trace event",
                )
                break

    evaluation = receipt.get("evaluation")
    if isinstance(evaluation, dict) and evaluation.get("treatment_delivered") is not bool(
        dropped
    ):
        _issue(
            issues,
            "$.evaluation.treatment_delivered",
            "must equal treatment delivery reconstructed from proxy events",
        )


def _canonical_invariants(receipt: dict, issues: list[ValidationIssue]) -> None:
    source = _as_dict(receipt.get("source"))
    requested = _as_dict(receipt.get("execution")).get("requested_canonical")
    expected = bool(
        requested is True
        and source.get("git_commit") is not None
        and source.get("git_dirty") is False
    )
    if receipt.get("canonical") is not expected:
        _issue(
            issues,
            "$.canonical",
            "must agree with requested mode and exact clean source provenance",
        )
    if receipt.get("canonical") is True:
        if source.get("git_commit") is None:
            _issue(issues, "$.source.git_commit", "canonical receipt requires a Git commit")
        if source.get("git_dirty") is not False:
            _issue(issues, "$.source.git_dirty", "canonical receipt requires clean source")
    if receipt.get("status") == _SUCCESS and requested is True and expected is not True:
        _issue(
            issues,
            "$.execution.requested_canonical",
            "a successful requested canonical run must be canonical",
        )


def _git_bytes(repo_root: Path, arguments: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def _commit_source_paths(repo_root: Path, commit: str) -> list[str] | None:
    output = _git_bytes(repo_root, ["ls-tree", "-r", "--name-only", commit])
    if output is None:
        return None
    names = output.decode("utf-8").splitlines()
    singles = {"run_pilot.py", "pyproject.toml", "PROTOCOL.md"}
    selected = [
        name
        for name in names
        if name in singles
        or (name.startswith("btb/") and name.endswith(".py"))
        or (name.startswith("btb/tasks/definitions/") and name.endswith(".json"))
        or (name.startswith("btb/app/templates/") and name.endswith(".html"))
        or (
            name.startswith("btb/schemas/")
            and Path(name).suffix in {".json", ".py"}
        )
    ]
    return sorted(selected)


def _commit_source_sha256(repo_root: Path, commit: str) -> str | None:
    paths = _commit_source_paths(repo_root, commit)
    if not paths:
        return None
    digest = hashlib.sha256()
    for name in paths:
        content = _git_bytes(repo_root, ["show", f"{commit}:{name}"])
        if content is None:
            return None
        name_bytes = name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_commit_invariants(
    receipt: dict,
    issues: list[ValidationIssue],
    *,
    source_repo: Path | None,
) -> None:
    if receipt.get("canonical") is not True:
        return
    if source_repo is None or not (source_repo / ".git").exists():
        _issue(
            issues,
            "$.source.git_commit",
            "canonical validation requires a source repository for commit verification",
        )
        return
    source = _as_dict(receipt.get("source"))
    commit = source.get("git_commit")
    if not isinstance(commit, str) or not _GIT_COMMIT_RE.fullmatch(commit):
        _issue(
            issues,
            "$.source.git_commit",
            "must be an exact hexadecimal Git object ID",
        )
        return
    reconstructed = _commit_source_sha256(source_repo, commit)
    if reconstructed is None:
        _issue(
            issues,
            "$.source.git_commit",
            "cannot reconstruct frozen source bytes from the claimed commit",
        )
    elif reconstructed != source.get("source_tree_sha256"):
        _issue(
            issues,
            "$.source.source_tree_sha256",
            "does not match executable source bytes reconstructed from the Git commit",
        )


def invariant_errors(
    receipt: object,
    *,
    source_repo: Path | None = None,
) -> list[ValidationIssue]:
    if not isinstance(receipt, dict):
        return [ValidationIssue("$", "receipt must be a JSON object")]
    issues: list[ValidationIssue] = []
    _content_invariants(receipt, issues)
    _baseline_invariants(receipt, issues)
    _task_registry_invariants(receipt, issues)
    _status_invariants(receipt, issues)
    _snapshot_identity_invariants(receipt, issues)
    _initial_state_invariants(receipt, issues)
    _send_invariants(receipt, issues)
    _injection_invariants(receipt, issues)
    _evaluation_invariants(receipt, issues)
    _canonical_invariants(receipt, issues)
    _source_commit_invariants(receipt, issues, source_repo=source_repo)
    return issues


def validate_receipt(
    receipt: object,
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    source_repo: Path | None = None,
) -> list[ValidationIssue]:
    """Return every schema and invariant error in stable sorted order."""

    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    issues = _schema_errors(receipt, schema)
    issues.extend(invariant_errors(receipt, source_repo=source_repo))
    return sorted(set(issues))


def _trace_artifact_errors(receipt: object, receipt_path: Path) -> list[ValidationIssue]:
    if not isinstance(receipt, dict) or receipt.get("status") != _SUCCESS:
        return []
    trace = receipt.get("trace")
    if not isinstance(trace, dict):
        return []
    relative = trace.get("path")
    if not isinstance(relative, str):
        return []
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or relative_path.parts[:1] != ("artifacts",)
        or len(relative_path.parts) != 2
        or ".." in relative_path.parts
    ):
        return [
            ValidationIssue(
                "$.trace.path",
                "must be one safe artifact path under the receipt directory",
            )
        ]
    artifact = receipt_path.parent / relative_path
    try:
        content = artifact.read_bytes()
    except OSError as exc:
        return [ValidationIssue("$.trace.path", f"cannot read bound trace artifact: {exc}")]
    issues: list[ValidationIssue] = []
    if len(content) != trace.get("size_bytes"):
        issues.append(
            ValidationIssue("$.trace.size_bytes", "does not match the bound artifact")
        )
    if hashlib.sha256(content).hexdigest() != trace.get("sha256"):
        issues.append(ValidationIssue("$.trace.sha256", "does not match the bound artifact"))
    if trace.get("format") == "playwright-trace-zip":
        try:
            with zipfile.ZipFile(artifact, "r") as archive:
                members = [archive.read(info.filename) for info in archive.infolist()]
        except (OSError, zipfile.BadZipFile) as exc:
            issues.append(
                ValidationIssue("$.trace.path", f"is not a readable trace ZIP: {exc}")
            )
        else:
            discovered = {
                token
                for member in members
                for pattern in (_TRACE_UI_HEADER_RE, _TRACE_UI_SCRIPT_RE)
                for token in pattern.findall(member)
            }
            if any(token != _TRACE_UI_TOKEN_REPLACEMENT for token in discovered):
                issues.append(
                    ValidationIssue(
                        "$.trace.redacted",
                        "Playwright trace still contains an unredacted UI capability token",
                    )
                )
    return issues


def validate_file(
    path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    source_repo: Path | None = REPO_ROOT,
) -> list[ValidationIssue]:
    try:
        with path.open(encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return [ValidationIssue("$", f"cannot read JSON receipt: {exc}")]
    issues = validate_receipt(
        receipt,
        schema_path=schema_path,
        source_repo=source_repo,
    )
    issues.extend(_trace_artifact_errors(receipt, path))
    return sorted(set(issues))


def _iter_receipt_paths(arguments: Sequence[str]) -> Iterable[Path]:
    for argument in arguments:
        path = Path(argument)
        if path.is_dir():
            yield from (
                candidate
                for candidate in sorted(path.glob("**/*.json"))
                if "artifacts" not in candidate.parts
            )
        else:
            yield path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate BrowserTransactionBench schema-v2 receipts"
    )
    parser.add_argument("paths", nargs="+", help="receipt JSON file(s) or directories")
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help="manifest-v2 JSON Schema path",
    )
    parser.add_argument(
        "--source-repo",
        type=Path,
        default=REPO_ROOT,
        help="Git source repository used to verify canonical commit bytes",
    )
    args = parser.parse_args(argv)

    invalid = False
    for path in _iter_receipt_paths(args.paths):
        issues = validate_file(
            path,
            schema_path=args.schema,
            source_repo=args.source_repo,
        )
        if issues:
            invalid = True
            print(f"INVALID {path}")
            for issue in issues:
                print(f"  {issue}")
        else:
            print(f"VALID {path}")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
