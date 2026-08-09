"""Version 2 benchmark receipts and source provenance.

New runs are written only as schema-v2 receipts.  The writer records evidence
that was captured by the engine; it never snapshots the database or evaluates a
run itself.  Canonical source checks intentionally use Git only for identity and
cleanliness while the source-tree digest binds the bytes actually on disk.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "2.0"
RELEASE_VERSION = "0.1.0"
EVALUATOR_VERSION = "btb-full-state-v1"
VALIDATOR_VERSION = "btb-manifest-validator-v2"
PARSER_VERSION = "btb-claim-v1"

_SECRET_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "BROWSER_USE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|secret|token)\b(\s*[:=]\s*)([^\s,;]+)"
)

RunStatus = Literal[
    "success",
    "setup_error",
    "baseline_error",
    "timeout",
    "evaluation_error",
]

_REQUIRED_SOURCE_SINGLE_FILES = ("run_pilot.py",)
_OPTIONAL_SOURCE_SINGLE_FILES = ("pyproject.toml", "PROTOCOL.md")
_EXCLUDED_STATUS_PREFIXES = (
    ".git/",
    ".venv/",
    "build/",
    "dist/",
    "manifests/",
    "results/",
    ".pytest_cache/",
    ".ruff_cache/",
)
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SAFE_ARTIFACT_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")
_TRACE_UI_HEADER_RE = re.compile(
    rb'"name"\s*:\s*"X-BTB-UI-Token"\s*,\s*"value"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_TRACE_UI_SCRIPT_RE = re.compile(
    rb'btbUiToken\s*=\s*(?:\\?")([^"\\]+)(?:\\?")',
)
_TRACE_UI_TOKEN_REPLACEMENT = b"<redacted:BTB_UI_TOKEN>"


class SourceProvenanceError(RuntimeError):
    """The current executable source cannot be identified exactly."""


class CanonicalSourceError(SourceProvenanceError):
    """A requested canonical run does not have a clean, identified source."""


@dataclass(frozen=True)
class SourceProvenance:
    git_commit: str | None
    git_dirty: bool
    source_tree_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineProvenance:
    """Public, non-secret baseline configuration recorded in a receipt."""

    name: str
    framework_name: str
    framework_version: str | None
    provider: str | None
    model: str | None
    parameters: dict[str, object]
    modality_policy: dict[str, object]
    capability_policy: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "framework": {
                "name": self.framework_name,
                "installed_version": self.framework_version,
            },
            "provider": self.provider,
            "model": self.model,
            "parameters": copy.deepcopy(self.parameters),
            "modality_policy": copy.deepcopy(self.modality_policy),
            "capability_policy": copy.deepcopy(self.capability_policy),
        }


def utc_now() -> str:
    """Return an unambiguous ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON deterministically for embedded-content digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def prompt_sha256(text: str) -> str:
    """Hash the exact UTF-8 prompt text, including all whitespace."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_text(
    text: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[str, bool]:
    """Remove common credentials from receipt-owned free text.

    Frozen task and prompt fields are not passed through this function because
    their hashes bind exact executed bytes.  Free-form model output, traces, and
    exception messages are sanitized before persistence instead.
    """

    redacted = text
    for name in _SECRET_ENVIRONMENT_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "<redacted:RUNTIME_SECRET>")
    home_directory = str(Path.home())
    if home_directory and home_directory != "/":
        redacted = redacted.replace(home_directory, "$HOME")
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = _LABELED_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    return redacted, redacted != text


def redact_value(
    value: object,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[object, bool]:
    """Recursively sanitize JSON-compatible trace or evidence content."""

    if isinstance(value, str):
        return redact_text(value, sensitive_values=sensitive_values)
    if isinstance(value, list):
        changed = False
        result: list[object] = []
        for item in value:
            sanitized, item_changed = redact_value(
                item,
                sensitive_values=sensitive_values,
            )
            result.append(sanitized)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, dict):
        changed = False
        result: dict[str, object] = {}
        for key, item in value.items():
            sanitized, item_changed = redact_value(
                item,
                sensitive_values=sensitive_values,
            )
            result[str(key)] = sanitized
            changed = changed or item_changed
        return result, changed
    return value, False


def _source_paths(repo_root: Path) -> list[Path]:
    """Return every executable/frozen benchmark input bound by the tree hash."""

    candidates: set[Path] = set()
    candidates.update(path for path in (repo_root / "btb").glob("**/*.py") if path.is_file())
    candidates.update(
        path
        for path in (repo_root / "btb" / "tasks" / "definitions").glob("*.json")
        if path.is_file()
    )
    candidates.update(
        path
        for path in (repo_root / "btb" / "app" / "templates").glob("**/*.html")
        if path.is_file()
    )
    schema_dir = repo_root / "btb" / "schemas"
    if schema_dir.is_dir():
        candidates.update(
            path
            for path in schema_dir.glob("**/*")
            if path.is_file() and path.suffix in {".json", ".py"}
        )

    missing: list[str] = []
    for relative in (*_REQUIRED_SOURCE_SINGLE_FILES, *_OPTIONAL_SOURCE_SINGLE_FILES):
        path = repo_root / relative
        if path.is_file():
            candidates.add(path)
        elif relative in _REQUIRED_SOURCE_SINGLE_FILES:
            missing.append(relative)
    if not any(path.suffix == ".py" for path in candidates):
        missing.append("btb/**/*.py")
    if not any(
        path.parent == repo_root / "btb" / "tasks" / "definitions"
        and path.suffix == ".json"
        for path in candidates
    ):
        missing.append("btb/tasks/definitions/*.json")
    if not any(path.suffix == ".html" for path in candidates):
        missing.append("btb/app/templates/**/*.html")
    if not any(path.is_relative_to(schema_dir) for path in candidates):
        missing.append("btb/schemas/**/*")
    if missing:
        raise SourceProvenanceError(
            "required source inputs are missing: " + ", ".join(sorted(missing))
        )
    return sorted(candidates, key=lambda path: path.relative_to(repo_root).as_posix())


def source_tree_sha256(repo_root: Path = REPO_ROOT) -> str:
    """Hash sorted ``relative path + byte length + bytes`` source records."""

    root = repo_root.expanduser().resolve()
    digest = hashlib.sha256()
    for path in _source_paths(root):
        relative_bytes = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _run_git(repo_root: Path, arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def _git_commit(repo_root: Path) -> str | None:
    output = _run_git(repo_root, ["rev-parse", "--verify", "HEAD"])
    if output is None:
        return None
    commit = output.strip().lower()
    return commit if _GIT_COMMIT_RE.fullmatch(commit) else None


def _status_path_is_excluded(path: str) -> bool:
    normalized = path.strip().strip('"').replace("\\", "/")
    return normalized in {prefix.rstrip("/") for prefix in _EXCLUDED_STATUS_PREFIXES} or any(
        normalized.startswith(prefix) for prefix in _EXCLUDED_STATUS_PREFIXES
    )


def _git_dirty(repo_root: Path) -> bool:
    output = _run_git(
        repo_root,
        ["status", "--porcelain=v1", "--untracked-files=all", "--", "."],
    )
    if output is None:
        return True
    for line in output.splitlines():
        if not line:
            continue
        status_path = line[3:] if len(line) >= 4 else line
        paths = status_path.split(" -> ")
        if not all(_status_path_is_excluded(path) for path in paths):
            return True
    return False


def source_provenance(repo_root: Path = REPO_ROOT) -> SourceProvenance:
    root = repo_root.expanduser().resolve()
    return SourceProvenance(
        git_commit=_git_commit(root),
        git_dirty=_git_dirty(root),
        source_tree_sha256=source_tree_sha256(root),
    )


def require_canonical_source(source: SourceProvenance) -> None:
    """Fail closed unless source has an exact commit and no working-tree edits."""

    problems: list[str] = []
    if source.git_commit is None:
        problems.append("Git commit is unavailable")
    if source.git_dirty:
        problems.append("source tree is dirty")
    if problems:
        raise CanonicalSourceError(
            "canonical run refused: " + "; ".join(problems)
        )


def classify_failure(exc: Exception, stage: str) -> RunStatus:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if stage == "evaluation":
        return "evaluation_error"
    if stage in {
        "baseline",
        "injection_quiescence",
        "after_snapshot",
        "playwright_trace",
        "playwright_trace_publish",
        "playwright_context_close",
        "playwright_browser_close",
    }:
        return "baseline_error"
    return "setup_error"


@dataclass
class ReceiptBuilder:
    """Accumulate one run's already-captured evidence and atomically write it."""

    run_id: str
    freeze: str
    baseline: BaselineProvenance
    configured_steps: int | None
    configured_wall_s: float | int | None
    canonical_requested: bool
    task_definition: dict | None
    prompt_text: str | None
    release: str = RELEASE_VERSION
    source: SourceProvenance | None = None
    out_dir: Path | None = None
    parser_version: str = PARSER_VERSION
    evaluator_version: str = EVALUATOR_VERSION
    validator_version: str = VALIDATOR_VERSION
    started_at: str = field(default_factory=utc_now)
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    before_snapshot: dict | None = None
    after_snapshot: dict | None = None
    agent_claim: dict | None = None
    injection_report: dict | None = None
    evaluation: dict | None = None
    outcome: str | None = None
    trace: dict | None = None
    framework_filesystem: dict | None = None
    evidence_failures: list[dict[str, str]] = field(default_factory=list)
    _finalized_path: Path | None = field(default=None, init=False, repr=False)
    _sensitive_values: set[str] = field(default_factory=set, init=False, repr=False)
    _effective_baseline_bound: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        _require_safe_artifact_component(self.run_id, name="run_id")
        if not self.freeze:
            raise ValueError("freeze must not be empty")
        if self.configured_steps is not None and (
            isinstance(self.configured_steps, bool)
            or not isinstance(self.configured_steps, int)
            or self.configured_steps < 1
        ):
            raise ValueError("configured_steps must be a positive integer when provided")
        if self.configured_wall_s is not None and (
            isinstance(self.configured_wall_s, bool)
            or not isinstance(self.configured_wall_s, (int, float))
            or self.configured_wall_s <= 0
        ):
            raise ValueError("configured_wall_s must be positive when provided")
        self.task_definition = copy.deepcopy(self.task_definition)
        self.source = self.source or source_provenance()

    @property
    def canonical(self) -> bool:
        if self.source is None:
            raise RuntimeError("source provenance has not been initialized")
        return bool(
            self.canonical_requested
            and self.source.git_commit is not None
            and not self.source.git_dirty
        )

    def ensure_canonical_source(self) -> None:
        if self.canonical_requested:
            if self.source is None:
                raise RuntimeError("source provenance has not been initialized")
            require_canonical_source(self.source)

    def set_prompt(self, text: str) -> None:
        """Record the exact prompt once, when runtime-dependent text is generated."""

        if self.prompt_text is not None:
            raise RuntimeError("prompt provenance has already been recorded")
        self.prompt_text = text

    def bind_effective_baseline(self, baseline: BaselineProvenance) -> None:
        """Replace desired provenance once with its observed semantic policy."""

        if self._effective_baseline_bound:
            raise RuntimeError("effective baseline provenance has already been bound")
        current = self.baseline.to_dict()
        observed = baseline.to_dict()
        current_parameters = current.get("parameters")
        observed_parameters = observed.get("parameters")
        if not isinstance(current_parameters, dict) or not isinstance(
            observed_parameters, dict
        ):
            raise ValueError("baseline parameters must be objects")
        current_policy = current_parameters.pop("effective_policy", None)
        observed_policy = observed_parameters.pop("effective_policy", None)
        if current_policy != {"status": "unobserved"}:
            raise ValueError("baseline is not awaiting an effective policy observation")
        if not isinstance(observed_policy, dict) or observed_policy.get("status") != (
            "observed"
        ):
            raise ValueError("effective baseline policy must be observed")
        if current != observed:
            raise ValueError(
                "effective baseline binding may only replace the observation fields"
            )
        self.baseline = copy.deepcopy(baseline)
        self._effective_baseline_bound = True

    def register_sensitive_value(self, value: str) -> None:
        """Register a runtime-only secret that must never enter receipt text."""

        if not isinstance(value, str) or not value:
            raise ValueError("sensitive runtime value must be a non-empty string")
        self._sensitive_values.add(value)

    @property
    def redaction_values(self) -> tuple[str, ...]:
        return tuple(sorted(self._sensitive_values))

    def record_evidence_failure(self, exc: Exception, *, stage: str) -> None:
        """Retain a secondary capture/cleanup error without hiding the run failure."""

        message, _redacted = redact_text(
            str(exc),
            sensitive_values=self.redaction_values,
        )
        self.evidence_failures.append(
            {
                "type": exc.__class__.__name__,
                "message": message,
                "stage": stage,
            }
        )

    @property
    def finalized_path(self) -> Path | None:
        return self._finalized_path

    def artifact_directory(self) -> Path:
        receipt_root = self.out_dir or receipt_directory(canonical=self.canonical)
        return receipt_root / "artifacts"

    def write_json_trace(self, value: object, *, kind: str) -> dict[str, object]:
        """Persist one sanitized complete JSON trace and return bound metadata."""

        sanitized, redacted = redact_value(
            value,
            sensitive_values=self.redaction_values,
        )
        payload = json.dumps(
            sanitized,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        path = write_artifact(
            payload,
            run_id=self.run_id,
            suffix=f"{kind}.json",
            out_dir=self.artifact_directory(),
        )
        metadata: dict[str, object] = {
            "kind": kind,
            "format": "json",
            "path": f"artifacts/{path.name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "complete": True,
            "redacted": redacted,
        }
        self.trace = metadata
        return metadata

    def bind_binary_trace(
        self,
        path: Path,
        *,
        kind: str,
        format_name: str,
        redacted: bool = False,
    ) -> dict[str, object]:
        """Bind an already-written binary trace to this receipt."""

        content = path.read_bytes()
        metadata: dict[str, object] = {
            "kind": kind,
            "format": format_name,
            "path": f"artifacts/{path.name}",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "complete": True,
            "redacted": redacted,
        }
        self.trace = metadata
        return metadata

    def register_framework_sandbox(self, sandbox_root: Path) -> None:
        """Register the private runtime root before any trace or failure persists."""

        self.register_sensitive_value(str(sandbox_root))

    def bind_framework_filesystem(
        self,
        *,
        state: str,
        inventory: dict | None,
        cleanup_error: Exception | None = None,
    ) -> dict[str, object]:
        """Bind the terminal Browser Use filesystem lifecycle without its root path."""

        if self.framework_filesystem is not None:
            raise RuntimeError("framework filesystem lifecycle has already been bound")
        if state not in {"not_created", "cleaned", "cleanup_failed"}:
            raise ValueError("framework filesystem state is invalid")
        if state == "not_created" and inventory is not None:
            raise ValueError("an uncreated sandbox cannot have an inventory")
        value: dict[str, object] = {
            "state": state,
            "cleanup_verified": state == "cleaned",
            "inventory": None,
            "cleanup_error": None,
        }
        if inventory is not None:
            try:
                payload = canonical_json_bytes(inventory) + b"\n"
                path = write_artifact(
                    payload,
                    run_id=self.run_id,
                    suffix="browser-use-sandbox-inventory.json",
                    out_dir=self.artifact_directory(),
                )
                value["inventory"] = {
                    "path": f"artifacts/{path.name}",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "entry_count": inventory.get("entry_count"),
                    "file_count": inventory.get("file_count"),
                    "total_bytes": inventory.get("total_bytes"),
                    "inventory_sha256": inventory.get("inventory_sha256"),
                }
            except Exception:
                # Preserve the terminal cleanup fact for a failure receipt even
                # when the separate inventory artifact cannot be published.
                self.framework_filesystem = value
                raise
        if cleanup_error is not None:
            message, _redacted = redact_text(
                str(cleanup_error), sensitive_values=self.redaction_values
            )
            value["cleanup_error"] = {
                "type": cleanup_error.__class__.__name__, "message": message
            }
        self.framework_filesystem = value
        return value

    def _ensure_learned_filesystem_state(self) -> None:
        if self.baseline.name in {"browser-use", "browser-use-full"} and (
            self.framework_filesystem is None
        ):
            self.bind_framework_filesystem(
                state="not_created", inventory=None, cleanup_error=None
            )

    def _receipt(
        self,
        *,
        status: RunStatus,
        failure: dict[str, str] | None,
    ) -> dict[str, object]:
        if self.source is None:
            raise RuntimeError("source provenance has not been initialized")
        ended_at = utc_now()
        duration_s = max(0.0, time.monotonic() - self._started_monotonic)
        task = None
        if self.task_definition is not None:
            task = {
                "definition": copy.deepcopy(self.task_definition),
                "sha256": canonical_json_sha256(self.task_definition),
            }
        prompt = {
            "text": self.prompt_text,
            "sha256": (
                prompt_sha256(self.prompt_text) if self.prompt_text is not None else None
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "release": self.release,
            "freeze": self.freeze,
            "status": status,
            "canonical": self.canonical,
            "started_at": self.started_at,
            "ended_at": ended_at,
            "duration_s": round(duration_s, 6),
            "source": self.source.to_dict(),
            "task": task,
            "prompt": prompt,
            "baseline": self.baseline.to_dict(),
            "execution": {
                "configured_steps": self.configured_steps,
                "configured_wall_s": self.configured_wall_s,
                "requested_canonical": self.canonical_requested,
                "failure": failure,
                "evidence_failures": copy.deepcopy(self.evidence_failures),
            },
            "agent_claim": copy.deepcopy(self.agent_claim),
            "trace": copy.deepcopy(self.trace),
            "framework_filesystem": copy.deepcopy(self.framework_filesystem),
            "injection_report": copy.deepcopy(self.injection_report),
            "before_snapshot": copy.deepcopy(self.before_snapshot),
            "after_snapshot": copy.deepcopy(self.after_snapshot),
            "evaluation": copy.deepcopy(self.evaluation),
            "outcome": self.outcome,
            "versions": {
                "parser": self.parser_version,
                "evaluator": self.evaluator_version,
                "validator": self.validator_version,
            },
        }

    def write_success(self) -> Path:
        self.ensure_canonical_source()
        if self.baseline.name in {"browser-use", "browser-use-full"}:
            effective = self.baseline.parameters.get("effective_policy")
            if not isinstance(effective, dict) or effective.get("status") != "observed":
                raise ValueError(
                    "successful learned-baseline receipt requires observed semantic "
                    "policy provenance"
                )
            filesystem = self.framework_filesystem
            if (
                not isinstance(filesystem, dict)
                or filesystem.get("state") != "cleaned"
                or filesystem.get("cleanup_verified") is not True
                or not isinstance(filesystem.get("inventory"), dict)
            ):
                raise ValueError(
                    "successful learned-baseline receipt requires a cleaned, verified "
                    "framework filesystem inventory"
                )
        if self.task_definition is None:
            raise ValueError("successful receipt requires complete task provenance")
        if self.prompt_text is None:
            raise ValueError("successful receipt requires exact prompt provenance")
        if self.before_snapshot is None or self.after_snapshot is None:
            raise ValueError("successful receipt requires before and after snapshots")
        if self.agent_claim is None:
            raise ValueError("successful receipt requires an agent claim")
        if self.injection_report is None:
            raise ValueError("successful receipt requires an injection report")
        if self.trace is None:
            raise ValueError("successful receipt requires a complete execution trace")
        if self.evaluation is None or self.outcome is None:
            raise ValueError("successful receipt requires evaluation and outcome")
        return self._write_once(
            self._receipt(status="success", failure=None),
            canonical=self.canonical,
        )

    def write_failure(
        self,
        exc: Exception,
        *,
        stage: str,
        status: RunStatus | None = None,
    ) -> Path:
        if not stage:
            raise ValueError("failure stage must not be empty")
        self._ensure_learned_filesystem_state()
        message, _redacted = redact_text(
            str(exc),
            sensitive_values=self.redaction_values,
        )
        failure = {
            "type": exc.__class__.__name__,
            "message": message,
            "stage": stage,
        }
        self.evaluation = None
        self.outcome = None
        return self._write_once(
            self._receipt(
                status=status or classify_failure(exc, stage),
                failure=failure,
            ),
            canonical=self.canonical,
        )

    def _write_once(self, receipt: dict[str, object], *, canonical: bool) -> Path:
        if self._finalized_path is not None:
            raise RuntimeError(
                f"receipt for run {self.run_id!r} has already been finalized"
            )
        path = write_receipt(receipt, canonical=canonical, out_dir=self.out_dir)
        self._finalized_path = path
        return path


def receipt_directory(*, canonical: bool, root: Path = REPO_ROOT) -> Path:
    if canonical:
        return root / "manifests" / "canonical"
    return root / "manifests" / "exploratory" / "current"


def write_receipt(
    receipt: dict[str, object],
    *,
    canonical: bool,
    out_dir: Path | None = None,
) -> Path:
    """Atomically persist a receipt without a partially-written JSON window."""

    directory = out_dir or receipt_directory(canonical=canonical)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("receipt run_id must be a string")
    _require_safe_artifact_component(run_id, name="receipt run_id")
    path = directory / f"{run_id}.json"
    temporary = directory / f".{run_id}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _link_without_overwrite(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def write_artifact(
    payload: bytes,
    *,
    run_id: str,
    suffix: str,
    out_dir: Path,
) -> Path:
    """Atomically write a run artifact without ever exposing a partial file."""

    _require_safe_artifact_component(run_id, name="artifact run_id")
    _require_safe_artifact_component(suffix, name="artifact suffix")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.{suffix}"
    temporary = out_dir / f".{run_id}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _link_without_overwrite(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _require_safe_artifact_component(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not _SAFE_ARTIFACT_COMPONENT_RE.fullmatch(value):
        raise ValueError(
            f"{name} must contain only letters, digits, dot, underscore, and hyphen"
        )


def _link_without_overwrite(temporary: Path, destination: Path) -> None:
    """Atomically publish one file while refusing an existing run artifact."""

    os.link(temporary, destination)
    temporary.unlink()


def redact_playwright_trace(
    path: Path,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> bool:
    """Remove ephemeral UI capability tokens from a Playwright trace ZIP.

    The managed runtime supplies the token directly. Header/script discovery is
    retained for explicit external-server mode, where the harness does not own
    the server token. Every discovered value is replaced in every ZIP member so
    snapshots and network records cannot preserve different copies.
    """

    replacements = {
        value.encode("utf-8")
        for value in sensitive_values
        if isinstance(value, str) and value
    }
    with zipfile.ZipFile(path, "r") as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    for _info, content in members:
        replacements.update(_TRACE_UI_HEADER_RE.findall(content))
        replacements.update(_TRACE_UI_SCRIPT_RE.findall(content))
    replacements.discard(b"")
    if not replacements:
        return False

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "x") as archive:
            for info, content in members:
                sanitized = content
                for secret in replacements:
                    sanitized = sanitized.replace(secret, _TRACE_UI_TOKEN_REPLACEMENT)
                archive.writestr(info, sanitized)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return True


def new_run_id(prefix: str = "run") -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}_{uuid.uuid4().hex[:6]}"
