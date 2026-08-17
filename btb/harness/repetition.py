"""Immutable planning and receipt-safe execution for repeated BTB studies.

The single-run engine remains the only owner of a fixture lifecycle and its
schema-v2 receipt.  This layer only plans a balanced matrix, calls that engine,
and refuses to continue unless the expected canonical receipt validates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from btb import __file__ as _BTB_PACKAGE_FILE
from btb.harness import (
    browser_use_policy,
    browser_use_sandbox,
    engine,
    validate_manifest,
)
from btb.harness import manifest as manifest_mod
from btb.tasks import runner as task_runner

PLAN_SCHEMA_VERSION = "btb-repetition-plan-v3"
PLAN_HASH_ALGORITHM = "sha256-canonical-json-v1"
RUN_ID_ALGORITHM = "sha256-plan-source-runtime-task-condition-repetition-v3"
RUNTIME_TREE_ALGORITHM = "sha256-btb-runtime-tree-v1"
ORDERING_ALGORITHM = "sha256-seed-sort-v1"
ORDERING_VERSION = "1"
PROMPT_COMPATIBILITY_ALGORITHM = "managed-loopback-url-v1"
_STUDY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")
_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "plan_hash_algorithm",
        "run_id_algorithm",
        "study_id",
        "canonical_receipts_required",
        "seed",
        "ordering",
        "repetitions",
        "study_wall_s",
        "treatment_assignment",
        "source",
        "runtime",
        "tasks",
        "conditions",
        "plan_identity_sha256",
        "runs",
        "plan_sha256",
    }
)
_FULL_PROVIDER_MODELS = frozenset(
    {
        ("anthropic", "claude-sonnet-4-0"),
        ("openai", "gpt-4.1-mini"),
    }
)
_CONTROL_BASELINES = frozenset({"playwright-exact", "playwright-naive"})


class RepetitionError(RuntimeError):
    """Base class for study-plan and receipt-contract failures."""


class PlanError(RepetitionError):
    """A plan is malformed, changed, or incompatible with its own hash."""


class ReceiptError(RepetitionError):
    """A receipt is missing, invalid, noncanonical, or not plan-bound."""


class StudyExecutionError(RepetitionError):
    """A started attempt cannot truthfully obtain one terminal receipt."""


@dataclass(frozen=True)
class PlanSource:
    """Canonical executable-source identity frozen into a study plan."""

    release: str
    git_commit: str
    git_dirty: bool
    source_tree_sha256: str
    canonical_eligible: bool

    @classmethod
    def from_provenance(cls, source: manifest_mod.SourceProvenance) -> PlanSource:
        try:
            manifest_mod.require_canonical_source(source)
        except manifest_mod.CanonicalSourceError as exc:
            raise PlanError(f"canonical plan source is unavailable: {exc}") from exc
        if (
            not isinstance(source.git_commit, str)
            or not _GIT_COMMIT_RE.fullmatch(source.git_commit)
            or not isinstance(source.source_tree_sha256, str)
            or not _SHA256_RE.fullmatch(source.source_tree_sha256)
        ):
            raise PlanError("canonical plan source has an invalid commit or tree digest")
        return cls(
            release=manifest_mod.RELEASE_VERSION,
            git_commit=source.git_commit,
            git_dirty=False,
            source_tree_sha256=source.source_tree_sha256,
            canonical_eligible=True,
        )

    @classmethod
    def from_plan(cls, value: object) -> PlanSource:
        fields = {
            "release",
            "git_commit",
            "git_dirty",
            "source_tree_sha256",
            "canonical_eligible",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise PlanError("plan source is invalid")
        result = cls(**dict(value))  # type: ignore[arg-type]
        if (
            not isinstance(result.release, str)
            or not result.release
            or result.release != result.release.strip()
            or not isinstance(result.git_commit, str)
            or not _GIT_COMMIT_RE.fullmatch(result.git_commit)
            or result.git_dirty is not False
            or not isinstance(result.source_tree_sha256, str)
            or not _SHA256_RE.fullmatch(result.source_tree_sha256)
            or result.canonical_eligible is not True
        ):
            raise PlanError("plan source is not a clean canonical source identity")
        return result

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def receipt_source(self) -> dict[str, object]:
        return {
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "source_tree_sha256": self.source_tree_sha256,
        }

    def to_provenance(self) -> manifest_mod.SourceProvenance:
        return manifest_mod.SourceProvenance(**self.receipt_source())  # type: ignore[arg-type]


@dataclass(frozen=True)
class RuntimeIdentity:
    """Exact imported BTB package bytes that execute a planned run."""

    algorithm: str
    package: str
    tree_sha256: str

    @classmethod
    def current(cls) -> RuntimeIdentity:
        root = Path(_BTB_PACKAGE_FILE).resolve().parent
        entries: list[dict[str, object]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            payload = path.read_bytes()
            entries.append(
                {
                    "path": relative.as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
        if not entries:
            raise PlanError("imported BTB runtime package has no bindable files")
        return cls(
            algorithm=RUNTIME_TREE_ALGORITHM,
            package="btb",
            tree_sha256=manifest_mod.canonical_json_sha256({"entries": entries}),
        )

    @classmethod
    def from_plan(cls, value: object) -> RuntimeIdentity:
        fields = {"algorithm", "package", "tree_sha256"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise PlanError("plan runtime identity is invalid")
        result = cls(**dict(value))  # type: ignore[arg-type]
        if (
            result.algorithm != RUNTIME_TREE_ALGORITHM
            or result.package != "btb"
            or not isinstance(result.tree_sha256, str)
            or not _SHA256_RE.fullmatch(result.tree_sha256)
        ):
            raise PlanError("plan runtime identity is invalid")
        return result

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _source_binding(
    *,
    source_repo: Path | str,
    source_provenance: manifest_mod.SourceProvenance | None = None,
) -> PlanSource:
    """Identify the clean source artifact, with a typed seam for offline fixtures."""

    if source_provenance is not None:
        return PlanSource.from_provenance(source_provenance)
    root = Path(source_repo).expanduser().resolve()
    try:
        return PlanSource.from_provenance(manifest_mod.source_provenance(root))
    except manifest_mod.SourceProvenanceError as exc:
        raise PlanError(f"cannot identify canonical plan source at {root}: {exc}") from exc


def _framework_name(baseline: str) -> str:
    return "playwright" if baseline in _CONTROL_BASELINES else "browser-use"


def _framework_identity(baseline: str) -> tuple[str, str]:
    """Read, but never import or initialize, the framework distribution."""

    framework = _framework_name(baseline)
    try:
        version = metadata.version(framework)
    except metadata.PackageNotFoundError as exc:
        raise PlanError(f"{framework} must be installed to create this condition") from exc
    if framework == "browser-use" and version != browser_use_policy.BROWSER_USE_VERSION:
        raise PlanError(
            "browser-use condition requires "
            f"{browser_use_policy.BROWSER_USE_VERSION}; installed={version!r}"
        )
    return framework, version


def _normalized_condition_fields(
    value: Mapping[str, object],
) -> tuple[str, str | None, str | None, int | None]:
    baseline, provider, model, max_steps = (
        value.get("baseline"),
        value.get("provider"),
        value.get("model"),
        value.get("max_steps"),
    )
    if not isinstance(baseline, str):
        raise PlanError("condition.baseline must be a string")
    if provider is not None and not isinstance(provider, str):
        raise PlanError("condition.provider must be a string or null")
    if model is not None and not isinstance(model, str):
        raise PlanError("condition.model must be a string or null")
    if max_steps is not None and (
        isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1
    ):
        raise PlanError("condition.max_steps must be a positive integer or null")
    provider = provider.strip().lower() if provider is not None else None
    model = model.strip() if model is not None else None
    if provider == "" or model == "":
        raise PlanError("condition provider/model must not be empty")
    if baseline in _CONTROL_BASELINES:
        valid = (provider, model, max_steps) == (None, "deterministic-playwright", None)
    elif baseline == "browser-use":
        valid = (provider, model) == ("deepseek", "deepseek-chat") and max_steps is not None
    elif baseline == "browser-use-full":
        valid = (provider, model) in _FULL_PROVIDER_MODELS and max_steps is not None
    else:
        valid = False
    if not valid:
        raise PlanError(f"unsupported or ambiguous condition: {baseline!r}")
    return baseline, provider, model, max_steps


@dataclass(frozen=True)
class Condition:
    """The full, explicit execution configuration for one planned condition."""

    baseline: str
    provider: str | None
    model: str | None
    max_steps: int | None
    framework: str
    framework_version: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Condition:
        """Expand ergonomic four-field caller input into a frozen condition."""

        if set(value) != {"baseline", "provider", "model", "max_steps"}:
            raise PlanError("condition must contain exactly baseline/provider/model/max_steps")
        baseline, provider, model, max_steps = _normalized_condition_fields(value)
        framework, framework_version = _framework_identity(baseline)
        return cls(baseline, provider, model, max_steps, framework, framework_version)

    @classmethod
    def from_plan_mapping(cls, value: Mapping[str, object]) -> Condition:
        """Load only the expanded, already-frozen condition representation."""

        fields = {
            "baseline",
            "provider",
            "model",
            "max_steps",
            "framework",
            "framework_version",
        }
        if set(value) != fields:
            raise PlanError("persisted condition must include frozen framework/version")
        # Reuse execution-field validation without consulting current installs:
        # a historical plan must remain aggregatable on another machine.
        baseline, provider, model, max_steps = _normalized_condition_fields(value)
        framework, framework_version = value.get("framework"), value.get("framework_version")
        if (
            not isinstance(framework, str)
            or not isinstance(framework_version, str)
            or not framework_version
            or framework_version != framework_version.strip()
        ):
            raise PlanError("persisted condition framework/version is invalid")
        if framework != _framework_name(baseline):
            raise PlanError("persisted condition framework does not match its baseline")
        if framework == "browser-use" and framework_version != browser_use_policy.BROWSER_USE_VERSION:
            raise PlanError("persisted browser-use condition has an unsupported framework version")
        return cls(baseline, provider, model, max_steps, framework, framework_version)

    def input_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline,
            "provider": self.provider,
            "model": self.model,
            "max_steps": self.max_steps,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline,
            "provider": self.provider,
            "model": self.model,
            "max_steps": self.max_steps,
            "framework": self.framework,
            "framework_version": self.framework_version,
        }

    @property
    def key(self) -> str:
        return "condition-v1-" + manifest_mod.canonical_json_sha256(self.to_dict())


@dataclass(frozen=True)
class TaskBinding:
    """Task identity/budgets copied into a plan and checked before execution."""

    task_id: str
    definition_sha256: str
    freeze: str
    steps: int
    wall_s: float

    @classmethod
    def from_definition(cls, task_id: str, definition: Mapping[str, object]) -> TaskBinding:
        budget = definition.get("budget")
        if definition.get("id") != task_id or not isinstance(budget, Mapping):
            raise PlanError(f"invalid frozen task definition: {task_id}")
        freeze, steps, wall_s = definition.get("freeze"), budget.get("steps"), budget.get("wall_s")
        if not isinstance(freeze, str) or not freeze:
            raise PlanError(f"task {task_id} has no freeze")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise PlanError(f"task {task_id} has invalid budget.steps")
        if not _positive(wall_s):
            raise PlanError(f"task {task_id} has invalid budget.wall_s")
        return cls(
            task_id,
            manifest_mod.canonical_json_sha256(dict(definition)),
            freeze,
            steps,
            float(wall_s),
        )

    @classmethod
    def from_plan(cls, value: object) -> TaskBinding:
        if not isinstance(value, Mapping) or set(value) != {
            "id",
            "definition_sha256",
            "freeze",
            "budget",
        }:
            raise PlanError("invalid task binding")
        task_id, digest, freeze, budget = (
            value.get("id"),
            value.get("definition_sha256"),
            value.get("freeze"),
            value.get("budget"),
        )
        if not isinstance(task_id, str) or not task_id:
            raise PlanError("task binding id is invalid")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise PlanError("task binding hash is invalid")
        if not isinstance(freeze, str) or not freeze or not isinstance(budget, Mapping):
            raise PlanError("task binding freeze/budget is invalid")
        steps, wall_s = budget.get("steps"), budget.get("wall_s")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1 or not _positive(wall_s):
            raise PlanError("task binding budget is invalid")
        return cls(task_id, digest, freeze, steps, float(wall_s))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.task_id,
            "definition_sha256": self.definition_sha256,
            "freeze": self.freeze,
            "budget": {"steps": self.steps, "wall_s": self.wall_s},
        }


@dataclass(frozen=True)
class PlannedRun:
    ordinal: int
    run_id: str
    task_id: str
    task_sha256: str
    condition_key: str
    repetition: int
    random_order_sha256: str

    @classmethod
    def from_mapping(cls, value: object) -> PlannedRun:
        fields = {
            "ordinal",
            "run_id",
            "task_id",
            "task_sha256",
            "condition_key",
            "repetition",
            "random_order_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise PlanError("invalid planned run")
        result = cls(**dict(value))  # type: ignore[arg-type]
        if (
            isinstance(result.ordinal, bool)
            or not isinstance(result.ordinal, int)
            or result.ordinal < 1
            or not isinstance(result.repetition, int)
            or result.repetition < 1
            or not isinstance(result.run_id, str)
            or not _RUN_ID_RE.fullmatch(result.run_id)
            or not isinstance(result.task_id, str)
            or not isinstance(result.condition_key, str)
            or not _SHA256_RE.fullmatch(str(result.task_sha256))
            or not _SHA256_RE.fullmatch(str(result.random_order_sha256))
        ):
            raise PlanError("planned run has invalid fields")
        return result

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StudyPlan:
    data: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return json.loads(manifest_mod.canonical_json_bytes(self.data))

    @property
    def study_id(self) -> str:
        return str(self.data["study_id"])

    @property
    def plan_sha256(self) -> str:
        return str(self.data["plan_sha256"])

    @property
    def study_wall_s(self) -> float:
        return float(self.data["study_wall_s"])

    @property
    def source(self) -> PlanSource:
        return PlanSource.from_plan(self.data["source"])

    @property
    def runtime(self) -> RuntimeIdentity:
        return RuntimeIdentity.from_plan(self.data["runtime"])

    @property
    def tasks(self) -> tuple[TaskBinding, ...]:
        return tuple(TaskBinding.from_plan(value) for value in self.data["tasks"])  # type: ignore[index,union-attr]

    @property
    def conditions(self) -> dict[str, Condition]:
        result: dict[str, Condition] = {}
        for value in self.data["conditions"]:  # type: ignore[index,union-attr]
            if not isinstance(value, Mapping):
                raise PlanError("invalid condition entry")
            condition = Condition.from_plan_mapping(
                {
                    name: value.get(name)
                    for name in (
                        "baseline",
                        "provider",
                        "model",
                        "max_steps",
                        "framework",
                        "framework_version",
                    )
                }
            )
            if value.get("key") != condition.key or condition.key in result:
                raise PlanError("condition key is invalid or duplicated")
            result[condition.key] = condition
        return result

    @property
    def runs(self) -> tuple[PlannedRun, ...]:
        return tuple(PlannedRun.from_mapping(value) for value in self.data["runs"])  # type: ignore[index,union-attr]


@dataclass(frozen=True)
class ExecutionReport:
    study_id: str
    plan_sha256: str
    attempted_run_ids: tuple[str, ...]
    completed_run_ids: tuple[str, ...]
    skipped_run_ids: tuple[str, ...]
    unstarted_run_ids: tuple[str, ...]
    stopped_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


Executor = Callable[..., object]
Clock = Callable[[], float]


def _positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value > 0
    )


def _identity(
    study_id: str,
    seed: str,
    repetitions: int,
    study_wall_s: float,
    source: PlanSource,
    runtime: RuntimeIdentity,
    tasks: Sequence[TaskBinding],
    conditions: Sequence[Condition],
) -> dict[str, object]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_hash_algorithm": PLAN_HASH_ALGORITHM,
        "run_id_algorithm": RUN_ID_ALGORITHM,
        "study_id": study_id,
        "canonical_receipts_required": True,
        "seed": seed,
        "ordering": {"algorithm": ORDERING_ALGORITHM, "version": ORDERING_VERSION},
        "repetitions": repetitions,
        "study_wall_s": study_wall_s,
        "treatment_assignment": {
            "kind": "frozen-task-definition",
            "seeded_assignment_mutates_tasks": False,
        },
        "source": source.to_dict(),
        "runtime": runtime.to_dict(),
        "tasks": [task.to_dict() for task in tasks],
        "conditions": [{"key": item.key, **item.to_dict()} for item in conditions],
    }


def _runs(
    identity_sha256: str,
    seed: str,
    repetitions: int,
    tasks: Sequence[TaskBinding],
    conditions: Sequence[Condition],
) -> list[dict[str, object]]:
    planned: list[dict[str, object]] = []
    run_ids: set[str] = set()
    for task in tasks:
        for condition in conditions:
            for repetition in range(1, repetitions + 1):
                digest = manifest_mod.canonical_json_sha256(
                    {
                        "algorithm": RUN_ID_ALGORITHM,
                        "plan_identity_sha256": identity_sha256,
                        "task": task.to_dict(),
                        "condition": condition.to_dict(),
                        "repetition": repetition,
                    }
                )
                run_id = f"btb-r1-{digest}"
                if not _RUN_ID_RE.fullmatch(run_id) or run_id in run_ids:
                    raise PlanError("generated a colliding or unsafe run ID")
                run_ids.add(run_id)
                order = manifest_mod.canonical_json_sha256(
                    {
                        "algorithm": ORDERING_ALGORITHM,
                        "version": ORDERING_VERSION,
                        "seed": seed,
                        "run_id": run_id,
                    }
                )
                planned.append(
                    {
                        "ordinal": 0,
                        "run_id": run_id,
                        "task_id": task.task_id,
                        "task_sha256": task.definition_sha256,
                        "condition_key": condition.key,
                        "repetition": repetition,
                        "random_order_sha256": order,
                    }
                )
    planned.sort(key=lambda item: (str(item["random_order_sha256"]), str(item["run_id"])))
    for ordinal, item in enumerate(planned, start=1):
        item["ordinal"] = ordinal
    return planned


def _plan_document(
    study_id: str,
    seed: str,
    repetitions: int,
    study_wall_s: float,
    source: PlanSource,
    runtime: RuntimeIdentity,
    tasks: Sequence[TaskBinding],
    conditions: Sequence[Condition],
) -> dict[str, object]:
    identity = _identity(
        study_id,
        seed,
        repetitions,
        study_wall_s,
        source,
        runtime,
        tasks,
        conditions,
    )
    identity_sha256 = manifest_mod.canonical_json_sha256(identity)
    document = {
        **identity,
        "plan_identity_sha256": identity_sha256,
        "runs": _runs(identity_sha256, seed, repetitions, tasks, conditions),
    }
    document["plan_sha256"] = manifest_mod.canonical_json_sha256(document)
    return document


def build_plan(
    *,
    study_id: str,
    seed: str | int,
    task_ids: Sequence[str],
    conditions: Sequence[Mapping[str, object]],
    repetitions: int,
    study_wall_s: float,
    source_repo: Path | str = manifest_mod.REPO_ROOT,
    source_provenance: manifest_mod.SourceProvenance | None = None,
    runtime_identity: RuntimeIdentity | None = None,
) -> StudyPlan:
    """Create a balanced plan without launching a browser or provider."""

    if not isinstance(study_id, str) or not _STUDY_ID_RE.fullmatch(study_id):
        raise PlanError("study_id must be a path-safe ASCII identifier up to 96 characters")
    if isinstance(seed, bool) or not isinstance(seed, (str, int)) or not str(seed):
        raise PlanError("seed must be a non-empty string or integer")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise PlanError("repetitions must be positive")
    if not _positive(study_wall_s):
        raise PlanError("study_wall_s must be finite and positive")
    if not task_ids or not conditions:
        raise PlanError("at least one task and condition are required")
    if len(set(task_ids)) != len(task_ids):
        raise PlanError("task IDs must be unique")
    source = _source_binding(
        source_repo=source_repo,
        source_provenance=source_provenance,
    )
    runtime = runtime_identity or RuntimeIdentity.current()
    tasks = sorted(
        (TaskBinding.from_definition(task_id, task_runner.load_definition(task_id)) for task_id in task_ids),
        key=lambda item: item.task_id,
    )
    condition_values = [Condition.from_mapping(item) for item in conditions]
    if len({item.key for item in condition_values}) != len(condition_values):
        raise PlanError("conditions must be unique after normalization")
    condition_values.sort(key=lambda item: item.key)
    return _validated_plan(
        _plan_document(
            study_id,
            str(seed),
            repetitions,
            float(study_wall_s),
            source,
            runtime,
            tasks,
            condition_values,
        )
    )


def _validated_plan(value: object) -> StudyPlan:
    """Fail closed unless the serialized matrix exactly rebuilds from its identity."""

    if not isinstance(value, Mapping):
        raise PlanError("plan must be a JSON object")
    if set(value) != _PLAN_FIELDS:
        raise PlanError("plan fields do not match the repetition-plan schema")
    study_id, seed, repetitions, wall_s = (
        value.get("study_id"), value.get("seed"), value.get("repetitions"), value.get("study_wall_s")
    )
    if (
        not isinstance(study_id, str)
        or not _STUDY_ID_RE.fullmatch(study_id)
        or not isinstance(seed, str)
        or not seed
        or isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions < 1
        or not _positive(wall_s)
    ):
        raise PlanError("plan identity fields are invalid")
    source = PlanSource.from_plan(value.get("source"))
    runtime = RuntimeIdentity.from_plan(value.get("runtime"))
    raw_tasks, raw_conditions = value.get("tasks"), value.get("conditions")
    if not isinstance(raw_tasks, list) or not raw_tasks or not isinstance(raw_conditions, list) or not raw_conditions:
        raise PlanError("plan tasks/conditions must be non-empty lists")
    tasks = [TaskBinding.from_plan(item) for item in raw_tasks]
    if [item.task_id for item in tasks] != sorted(item.task_id for item in tasks):
        raise PlanError("plan task bindings must be sorted")
    conditions: list[Condition] = []
    for item in raw_conditions:
        if not isinstance(item, Mapping) or set(item) != {
            "key", "baseline", "provider", "model", "max_steps", "framework", "framework_version"
        }:
            raise PlanError("plan condition is invalid")
        condition = Condition.from_plan_mapping(
            {name: item.get(name) for name in (
                "baseline", "provider", "model", "max_steps", "framework", "framework_version"
            )}
        )
        if item.get("key") != condition.key:
            raise PlanError("plan condition key does not bind its content")
        conditions.append(condition)
    if [item.key for item in conditions] != sorted(item.key for item in conditions):
        raise PlanError("plan conditions must be sorted")
    expected = _plan_document(
        study_id,
        seed,
        repetitions,
        float(wall_s),
        source,
        runtime,
        tasks,
        conditions,
    )
    canonical = json.loads(manifest_mod.canonical_json_bytes(dict(value)))
    if canonical != expected:
        raise PlanError("plan content/hash/order does not match its normalized identity")
    return StudyPlan(canonical)


def load_plan(path: Path | str) -> StudyPlan:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return _validated_plan(json.load(handle))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read plan {path}: {exc}") from exc


def write_plan(path: Path | str, plan: StudyPlan) -> StudyPlan:
    """Atomically publish once; equal replays work, a mismatched overwrite fails."""

    plan = _validated_plan(plan.to_dict())
    target = Path(path)
    payload = manifest_mod.canonical_json_bytes(plan.to_dict()) + b"\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing = load_plan(target)
            if existing.to_dict() != plan.to_dict():
                raise PlanError(f"refusing to overwrite a different plan at {target}")
            return existing
    finally:
        temporary.unlink(missing_ok=True)
    return plan


def create_plan(path: Path | str, **kwargs: object) -> StudyPlan:
    return write_plan(path, build_plan(**kwargs))  # type: ignore[arg-type]


def _coerce_plan(plan: StudyPlan | Mapping[str, object] | Path | str) -> StudyPlan:
    if isinstance(plan, StudyPlan):
        return _validated_plan(plan.to_dict())
    return load_plan(plan) if isinstance(plan, (str, Path)) else _validated_plan(plan)


def _read_receipt(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read receipt {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"receipt {path} is not an object")
    return value


def _task_and_condition(plan: StudyPlan, run: PlannedRun) -> tuple[TaskBinding, Condition]:
    task = next((item for item in plan.tasks if item.task_id == run.task_id), None)
    if task is None or task.definition_sha256 != run.task_sha256:
        raise PlanError("run task does not bind the plan")
    try:
        return task, plan.conditions[run.condition_key]
    except KeyError as exc:
        raise PlanError("run condition does not bind the plan") from exc


def _directory_entries(root: Path) -> dict[str, os.stat_result]:
    """Boundedly enumerate one real directory through a no-follow descriptor."""

    try:
        root_status = root.lstat()
    except OSError as exc:
        raise ReceiptError(f"cannot lstat receipt directory: {root}") from exc
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        raise ReceiptError(f"receipt directory is not a real directory: {root}")
    try:
        descriptor = browser_use_sandbox._open_directory(root, expected=root_status)
        names = browser_use_sandbox._bounded_scandir_names(descriptor, remaining_slots=4096)
        result = {
            name: os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            for name in names
        }
    except (OSError, browser_use_sandbox.SandboxError) as exc:
        raise ReceiptError("cannot safely enumerate receipt directory") from exc
    finally:
        try:
            os.close(descriptor)
        except UnboundLocalError:
            pass
    return result


@dataclass
class _ReceiptDirectory:
    """One validated receipt directory held open through publication/rollback."""

    path: Path
    descriptor: int
    expected: os.stat_result
    artifacts_descriptor: int | None = None
    artifacts_expected: os.stat_result | None = None
    closed: bool = False

    def verify(self) -> None:
        """Reject a path/inode replacement before or after each material write."""

        if self.closed:
            raise ReceiptError("receipt directory descriptor is closed")
        try:
            path_status = self.path.lstat()
            descriptor_status = os.fstat(self.descriptor)
        except OSError as exc:
            raise ReceiptError("receipt directory changed during publication") from exc
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISDIR(path_status.st_mode)
            or not browser_use_sandbox._same_entry(path_status, self.expected)
            or not stat.S_ISDIR(descriptor_status.st_mode)
            or not browser_use_sandbox._same_entry(descriptor_status, self.expected)
        ):
            raise ReceiptError("receipt directory changed during publication")
        if self.artifacts_descriptor is not None:
            try:
                artifacts_status = os.stat(
                    "artifacts", dir_fd=self.descriptor, follow_symlinks=False
                )
                artifacts_actual = os.fstat(self.artifacts_descriptor)
            except OSError as exc:
                raise ReceiptError("receipt artifact directory changed during publication") from exc
            if (
                self.artifacts_expected is None
                or stat.S_ISLNK(artifacts_status.st_mode)
                or not stat.S_ISDIR(artifacts_status.st_mode)
                or not browser_use_sandbox._same_entry(
                    artifacts_status, self.artifacts_expected
                )
                or not browser_use_sandbox._same_entry(
                    artifacts_actual, self.artifacts_expected
                )
            ):
                raise ReceiptError("receipt artifact directory changed during publication")

    def artifacts(self, *, create: bool) -> int | None:
        """Open and retain the real artifact directory relative to ``descriptor``."""

        self.verify()
        try:
            expected = os.stat("artifacts", dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir("artifacts", mode=0o700, dir_fd=self.descriptor)
                expected = os.stat(
                    "artifacts", dir_fd=self.descriptor, follow_symlinks=False
                )
            except OSError as exc:
                raise ReceiptError("cannot create receipt artifact directory") from exc
        except OSError as exc:
            raise ReceiptError("cannot inspect receipt artifact directory") from exc
        if not stat.S_ISDIR(expected.st_mode) or stat.S_ISLNK(expected.st_mode):
            raise ReceiptError("canonical artifact directory is unsafe")
        if self.artifacts_descriptor is None:
            try:
                descriptor = os.open(
                    "artifacts",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=self.descriptor,
                )
            except OSError as exc:
                raise ReceiptError("cannot safely open receipt artifact directory") from exc
            actual = os.fstat(descriptor)
            if not browser_use_sandbox._same_entry(actual, expected):
                os.close(descriptor)
                raise ReceiptError("receipt artifact directory changed during publication")
            self.artifacts_descriptor = descriptor
            self.artifacts_expected = expected
        else:
            actual = os.fstat(self.artifacts_descriptor)
            if self.artifacts_expected is None or not browser_use_sandbox._same_entry(
                actual, self.artifacts_expected
            ) or not browser_use_sandbox._same_entry(expected, self.artifacts_expected):
                raise ReceiptError("receipt artifact directory changed during publication")
        self.verify()
        return self.artifacts_descriptor

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.artifacts_descriptor is not None:
            os.close(self.artifacts_descriptor)
            self.artifacts_descriptor = None
        os.close(self.descriptor)


def _open_receipt_directory(
    root: Path,
    *,
    create: bool,
    expected: os.stat_result | None = None,
) -> _ReceiptDirectory:
    expected_binding = expected
    if create and expected_binding is None:
        _ensure_receipt_directory(root)
    try:
        expected = root.lstat()
    except OSError as exc:
        raise ReceiptError(f"cannot lstat receipt directory: {root}") from exc
    if (
        not stat.S_ISDIR(expected.st_mode)
        or stat.S_ISLNK(expected.st_mode)
        or (
            expected_binding is not None
            and not browser_use_sandbox._same_entry(expected, expected_binding)
        )
    ):
        raise ReceiptError(f"receipt directory is not a real directory: {root}")
    try:
        descriptor = browser_use_sandbox._open_directory(root, expected=expected)
    except (OSError, browser_use_sandbox.SandboxError) as exc:
        raise ReceiptError("cannot safely open receipt directory") from exc
    held = _ReceiptDirectory(root, descriptor, expected)
    try:
        held.verify()
    except Exception:
        held.close()
        raise
    return held


def _ensure_receipt_directory(root: Path) -> None:
    try:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ReceiptError(f"cannot create receipt directory: {root}") from exc
    _directory_entries(root)


def _read_receipt_payload(root: Path, name: str, expected: os.stat_result) -> bytes:
    if not stat.S_ISREG(expected.st_mode) or stat.S_ISLNK(expected.st_mode):
        raise ReceiptError(f"receipt is not a regular file: {name}")
    if expected.st_size > _MAX_RECEIPT_BYTES:
        raise ReceiptError(f"receipt exceeds bounded size: {name}")
    root_status = root.lstat()
    descriptor: int | None = browser_use_sandbox._open_directory(root, expected=root_status)
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            actual = os.fstat(handle)
            if (
                not stat.S_ISREG(actual.st_mode)
                or not browser_use_sandbox._same_entry(actual, expected)
                or actual.st_size != expected.st_size
            ):
                raise ReceiptError(f"receipt changed while being read: {name}")
            payload = bytearray()
            while len(payload) <= _MAX_RECEIPT_BYTES:
                chunk = os.read(handle, min(1024 * 1024, _MAX_RECEIPT_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > _MAX_RECEIPT_BYTES or os.read(handle, 1):
                raise ReceiptError(f"receipt exceeds bounded size: {name}")
        finally:
            os.close(handle)
    except OSError as exc:
        raise ReceiptError(f"cannot safely read receipt: {name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return bytes(payload)


def _read_receipt_entry(root: Path, name: str, expected: os.stat_result) -> dict[str, object]:
    try:
        value = json.loads(_read_receipt_payload(root, name, expected).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot parse receipt: {name}") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"receipt is not an object: {name}")
    return value


def _artifact_names(receipt: Mapping[str, object]) -> set[str]:
    names: set[str] = set()
    for artifact_metadata in (receipt.get("trace"), (receipt.get("framework_filesystem") or {}).get("inventory") if isinstance(receipt.get("framework_filesystem"), Mapping) else None):
        if artifact_metadata is None:
            continue
        if not isinstance(artifact_metadata, Mapping) or not isinstance(artifact_metadata.get("path"), str):
            raise ReceiptError("receipt artifact metadata is invalid")
        path = Path(artifact_metadata["path"])
        if path.parts[:1] != ("artifacts",) or len(path.parts) != 2 or path.name != path.parts[1]:
            raise ReceiptError("receipt artifact path is unsafe")
        names.add(path.name)
    return names


def validated_receipts(
    plan: StudyPlan,
    receipt_dir: Path,
    source_repo: Path,
    *,
    require_complete: bool,
) -> dict[str, dict[str, object]]:
    """Validate the exact no-follow receipt/artifact directory layout and bindings."""

    if not receipt_dir.exists():
        if require_complete:
            raise ReceiptError(f"receipt directory is not a directory: {receipt_dir}")
        return {}
    entries = _directory_entries(receipt_dir)
    planned = {run.run_id: run for run in plan.runs}
    receipt_entries: dict[str, os.stat_result] = {}
    artifact_status: os.stat_result | None = None
    for name, entry in entries.items():
        if name == "artifacts":
            if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                raise ReceiptError("artifacts must be one real directory")
            artifact_status = entry
        elif name.endswith(".json"):
            run_id = name.removesuffix(".json")
            if run_id not in planned or not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                raise ReceiptError(
                    f"foreign receipt extras filename/run_id mismatch or unsafe entry: {name}"
                )
            receipt_entries[run_id] = entry
        else:
            raise ReceiptError(f"unexpected receipt-directory entry: {name}")
    if require_complete and set(receipt_entries) != set(planned):
        raise ReceiptError("receipt set is missing planned receipts or does not exactly match plan")
    result: dict[str, dict[str, object]] = {}
    required_artifacts: set[str] = set()
    for run_id, entry in receipt_entries.items():
        raw = _read_receipt_entry(receipt_dir, f"{run_id}.json", entry)
        if raw.get("run_id") != run_id:
            raise ReceiptError(f"receipt filename/run_id mismatch: {run_id}.json")
        result[run_id] = _validate_receipt_binding(
            receipt_dir / f"{run_id}.json",
            plan,
            planned[run_id],
            source_repo,
            receipt=raw,
            expected_stat=entry,
        )
        required_artifacts.update(_artifact_names(result[run_id]))
    if artifact_status is None:
        if required_artifacts:
            raise ReceiptError("receipt artifacts are missing their directory")
        return result
    artifact_entries = _directory_entries(receipt_dir / "artifacts")
    if set(artifact_entries) != required_artifacts:
        raise ReceiptError("receipt artifacts are orphaned, missing, or unexpected")
    for name, entry in artifact_entries.items():
        if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
            raise ReceiptError(f"artifact is not a regular file: {name}")
    return result


def _validate_receipt_binding(
    path: Path,
    plan: StudyPlan,
    run: PlannedRun,
    source_repo: Path,
    *,
    receipt: dict[str, object] | None = None,
    expected_stat: os.stat_result | None = None,
) -> dict[str, object]:
    def verify_entry() -> None:
        if expected_stat is None:
            return
        try:
            actual = path.lstat()
        except OSError as exc:
            raise ReceiptError(f"receipt vanished during validation: {path.name}") from exc
        if (
            stat.S_ISLNK(actual.st_mode)
            or not stat.S_ISREG(actual.st_mode)
            or not browser_use_sandbox._same_entry(actual, expected_stat)
            or actual.st_size != expected_stat.st_size
        ):
            raise ReceiptError(f"receipt changed during validation: {path.name}")

    verify_entry()
    errors = (
        validate_manifest.validate_loaded_receipt(
            receipt,
            receipt_directory=path.parent,
            source_repo=source_repo,
        )
        if receipt is not None
        else validate_manifest.validate_file(path, source_repo=source_repo)
    )
    verify_entry()
    if errors:
        raise ReceiptError("independent validation rejected " + path.name + ": " + "; ".join(map(str, errors)))
    receipt = receipt or _read_receipt(path)
    if receipt.get("canonical") is not True:
        raise ReceiptError(f"receipt {path.name} is exploratory/noncanonical")
    receipt_source = receipt.get("source")
    if (
        receipt.get("release") != plan.source.release
        or not isinstance(receipt_source, Mapping)
        or dict(receipt_source) != plan.source.receipt_source()
    ):
        raise ReceiptError(f"receipt {path.name} source/release differs from the plan")
    task, condition = _task_and_condition(plan, run)
    execution, embedded_task, baseline = (
        receipt.get("execution"), receipt.get("task"), receipt.get("baseline")
    )
    if (
        receipt.get("run_id") != run.run_id
        or not isinstance(execution, Mapping)
        or execution.get("requested_canonical") is not True
        or not isinstance(embedded_task, Mapping)
        or not isinstance(embedded_task.get("definition"), Mapping)
        or embedded_task.get("sha256") != task.definition_sha256
        or embedded_task["definition"].get("id") != task.task_id
        or manifest_mod.canonical_json_sha256(dict(embedded_task["definition"])) != task.definition_sha256
        or receipt.get("freeze") != task.freeze
        or embedded_task["definition"].get("freeze") != task.freeze
        or not isinstance(baseline, Mapping)
    ):
        raise ReceiptError(f"receipt {path.name} is not exactly bound to its planned run")
    framework = baseline.get("framework")
    observed = {
        "baseline": baseline.get("name"),
        "provider": baseline.get("provider"),
        "model": baseline.get("model"),
        "max_steps": execution.get("configured_steps"),
        "framework": framework.get("name") if isinstance(framework, Mapping) else None,
        "framework_version": (
            framework.get("installed_version") if isinstance(framework, Mapping) else None
        ),
    }
    if observed != condition.to_dict() or execution.get("configured_wall_s") != task.wall_s:
        raise ReceiptError(f"receipt {path.name} condition/budget differs from the plan")
    return receipt


def _receipt_duration(receipt: Mapping[str, object], path: Path) -> float:
    """Return a schema-validated terminal duration for cumulative study budget."""

    duration = receipt.get("duration_s")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration < 0
    ):
        raise ReceiptError(f"receipt {path.name} has an invalid duration_s")
    return float(duration)


def _current_task(binding: TaskBinding) -> dict:
    task = task_runner.load_definition(binding.task_id)
    if TaskBinding.from_definition(binding.task_id, task) != binding:
        raise StudyExecutionError(f"frozen task changed after planning: {binding.task_id}")
    return task


def _require_current_framework(condition: Condition) -> None:
    observed = _framework_identity(condition.baseline)
    expected = (condition.framework, condition.framework_version)
    if observed != expected:
        raise StudyExecutionError(
            f"planned framework differs from installed framework: expected={expected!r}, "
            f"observed={observed!r}"
        )


def _execution_source(
    plan: StudyPlan,
    *,
    source_repo: Path,
    source_provenance: manifest_mod.SourceProvenance | None,
) -> manifest_mod.SourceProvenance:
    """Fail before resume or launch if the executable source differs from the plan."""

    try:
        observed = _source_binding(
            source_repo=source_repo,
            source_provenance=source_provenance,
        )
    except PlanError as exc:
        raise StudyExecutionError(f"cannot execute with an unverified source: {exc}") from exc
    if observed != plan.source:
        raise StudyExecutionError("current executable source differs from the frozen plan source")
    return observed.to_provenance()


def _execution_runtime(plan: StudyPlan) -> RuntimeIdentity:
    """Refuse execution when imported package bytes differ from the plan."""

    observed = RuntimeIdentity.current()
    if observed != plan.runtime:
        raise StudyExecutionError("current executable runtime differs from the frozen plan")
    return observed


@dataclass(frozen=True)
class _WorkerProcessResult:
    timed_out: bool
    return_code: int | None
    teardown_error: bool
    process_group: int | None


def _worker_environment(
    provider: str | None, *, work_root: Path | None = None
) -> dict[str, str]:
    """Pass only runtime routing plus the one selected provider credential."""

    work_root = work_root or Path(tempfile.gettempdir())
    package_parent = str(Path(_BTB_PACKAGE_FILE).resolve().parent.parent)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": package_parent,
        "PYTHONNOUSERSITE": "1",
        "HOME": str(work_root),
        "TMPDIR": str(work_root),
        "TMP": str(work_root),
        "TEMP": str(work_root),
        "XDG_CONFIG_HOME": str(work_root / "config"),
        "XDG_CACHE_HOME": str(work_root / "cache"),
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "PLAYWRIGHT_BROWSERS_PATH"):
        if value := os.environ.get(name):
            environment[name] = value
    credential_name = {
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider)
    if credential_name and (value := os.environ.get(credential_name)):
        environment[credential_name] = value
    return environment


def _worker_command() -> list[str]:
    return [sys.executable, "-m", "btb.harness.repetition_worker"]


def _run_worker_process(
    request: dict[str, object],
    *,
    timeout_s: float,
    provider: str | None,
    work_root: Path | None = None,
) -> _WorkerProcessResult:
    """Bound one production run to a reaped process group and hard deadline."""

    if not _positive(timeout_s):
        raise StudyExecutionError("worker timeout must be finite and positive")
    work_root = work_root or Path(tempfile.gettempdir())
    try:
        process = subprocess.Popen(
            _worker_command(),
            cwd=work_root,
            env=_worker_environment(provider, work_root=work_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return _WorkerProcessResult(False, None, False, None)
    timed_out = False
    teardown_error = False
    try:
        process.communicate(
            manifest_mod.canonical_json_bytes(request).decode("utf-8"),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            browser_use_sandbox._quiesce_process_group(process)
        except browser_use_sandbox.SandboxError:
            teardown_error = True
    return _WorkerProcessResult(
        timed_out,
        process.returncode,
        teardown_error,
        process.pid,
    )


def _parent_builder(
    plan: StudyPlan,
    task: dict,
    run: PlannedRun,
    condition: Condition,
    receipt_dir: Path,
) -> manifest_mod.ReceiptBuilder:
    return engine.receipt_builder_for(
        task=task,
        run_id=run.run_id,
        baseline=condition.baseline,
        provider=condition.provider,
        model=condition.model,
        max_steps=condition.max_steps,
        options=engine.ReceiptOptions(mode="canonical", out_dir=receipt_dir),
        source=plan.source.to_provenance(),
        release=plan.source.release,
    )


def _worker_request(
    plan: StudyPlan,
    task: dict,
    run: PlannedRun,
    condition: Condition,
    receipt_dir: Path,
) -> dict[str, object]:
    return {
        "baseline": condition.baseline,
        "provider": condition.provider,
        "model": condition.model,
        "max_steps": condition.max_steps,
        "run_id": run.run_id,
        "task": task,
        "receipt_dir": str(receipt_dir.resolve()),
        "source": plan.source.to_dict(),
        "runtime": plan.runtime.to_dict(),
    }


def _new_work_root(*, prefix: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=tempfile.gettempdir())).resolve(strict=True)
    try:
        browser_use_sandbox._require_outside_repo_and_home(root)
        root.chmod(0o700)
        (root / "config").mkdir(mode=0o700)
        (root / "cache").mkdir(mode=0o700)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def _new_staging_root() -> tuple[Path, Path]:
    root = _new_work_root(prefix="btb-repeat-")
    stage = root / "receipts"
    stage.mkdir(mode=0o700)
    return root, stage


def _cleanup_root(
    root: Path,
) -> tuple[dict[str, object] | None, Exception | None, Exception | None]:
    """Inventory then remove one parent-created work root without following links."""

    inventory: dict[str, object] | None = None
    inventory_error: Exception | None = None
    try:
        inventory = browser_use_sandbox.inventory_sandbox(root)
    except Exception as exc:  # noqa: BLE001 - retained by a parent failure receipt
        inventory_error = exc
    try:
        shutil.rmtree(root)
        if os.path.lexists(root):
            raise RuntimeError("parent work root remains after cleanup")
    except Exception as exc:  # noqa: BLE001 - terminal lifecycle evidence
        return inventory, inventory_error, exc
    return inventory, inventory_error, None


def _artifact_metadata(receipt: Mapping[str, object]) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for artifact_metadata in (
        receipt.get("trace"),
        (receipt.get("framework_filesystem") or {}).get("inventory")
        if isinstance(receipt.get("framework_filesystem"), Mapping)
        else None,
    ):
        if artifact_metadata is None:
            continue
        if not isinstance(artifact_metadata, Mapping):
            raise ReceiptError("receipt artifact metadata is invalid")
        path, digest, size = (
            artifact_metadata.get("path"),
            artifact_metadata.get("sha256"),
            artifact_metadata.get("size_bytes"),
        )
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ReceiptError("receipt artifact digest metadata is invalid")
        name = Path(path).name
        result[name] = (digest, size)
    return result


@dataclass
class _Publication:
    directory: _ReceiptDirectory
    run_id: str
    receipt_payload: bytes
    artifacts: list[tuple[str, str, os.stat_result]]


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing receipt data")
        view = view[written:]


def _owned_file_stat(
    directory_descriptor: int,
    name: str,
    *,
    creation_descriptor: int | None = None,
) -> os.stat_result:
    expected: os.stat_result | None = None
    if creation_descriptor is not None:
        try:
            expected = os.fstat(creation_descriptor)
        except OSError as exc:
            raise ReceiptError(f"publication temporary disappeared: {name}") from exc
    try:
        status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise ReceiptError(f"publication temporary disappeared: {name}") from exc
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ReceiptError(f"publication temporary is not a regular file: {name}")
    if expected is not None and not browser_use_sandbox._same_entry(status, expected):
        raise ReceiptError(f"publication temporary changed: {name}")
    return expected or status


def _unlink_owned(
    directory_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> bool:
    """Remove a publication entry only while it still names our inode.

    Publication is one-writer in a parent-owned mode-0700 directory; portable
    POSIX cannot make the final path unlink atomic against an arbitrary
    same-UID writer.
    """

    try:
        actual = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(actual.st_mode)
        or stat.S_ISLNK(actual.st_mode)
        or not browser_use_sandbox._same_entry(actual, expected)
    ):
        return False
    # ponytail: POSIX has no portable unlink-by-inode; quarantine the checked
    # name so a replacement at the original name cannot be deleted.
    quarantine = f".btb-repeat-cleanup-{uuid.uuid4().hex}.tmp"
    try:
        os.rename(
            name,
            quarantine,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        moved = os.stat(quarantine, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (
        stat.S_ISREG(moved.st_mode)
        and not stat.S_ISLNK(moved.st_mode)
        and browser_use_sandbox._same_entry(moved, expected)
    ):
        # Re-check after the quarantine move.  A drifted/missing entry is
        # never unlinked; portable POSIX has no unlink-by-inode primitive.
        try:
            current = os.stat(
                quarantine,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not browser_use_sandbox._same_entry(current, expected)
        ):
            return False
        os.unlink(quarantine, dir_fd=directory_descriptor)
        return True
    # A foreign inode won the rename race. Restore it only when the original
    # name is still free; never overwrite a replacement that arrived there.
    try:
        os.link(
            quarantine,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError:
        return False
    except OSError:
        return False
    # Do not unlink a foreign inode: the original name is restored when it is
    # still free, while the quarantine entry remains as fail-closed residue if
    # a replacement won that race.
    return False


def _write_bytes_to_temp(
    directory: _ReceiptDirectory,
    *,
    name: str,
    payload: bytes,
) -> tuple[str, str, os.stat_result]:
    """Create one durable target-directory temp using only held descriptors."""

    artifacts_descriptor = directory.artifacts(create=True)
    if artifacts_descriptor is None:  # pragma: no cover - create=True guarantees this
        raise ReceiptError("receipt artifact directory is unavailable")
    temporary_name = f".btb-repeat-{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=artifacts_descriptor,
        )
    except OSError as exc:
        raise ReceiptError("cannot create receipt artifact temporary") from exc
    expected: os.stat_result | None = None
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        expected = _owned_file_stat(
            artifacts_descriptor,
            temporary_name,
            creation_descriptor=descriptor,
        )
        directory.verify()
    except Exception:
        if expected is None:
            expected = os.fstat(descriptor)
        os.close(descriptor)
        _unlink_owned(artifacts_descriptor, temporary_name, expected)
        raise
    os.close(descriptor)
    if expected is None:  # pragma: no cover - the try block always binds it
        raise ReceiptError("publication temporary was not captured")
    return temporary_name, name, expected


def _copy_artifact_to_temp(
    source: Path,
    directory: _ReceiptDirectory,
    *,
    name: str,
    digest: str,
    size: int,
) -> tuple[str, str, os.stat_result]:
    """Stream one validated artifact into a target-filesystem temporary file."""

    source_status = source.lstat()
    if not stat.S_ISREG(source_status.st_mode) or stat.S_ISLNK(source_status.st_mode):
        raise ReceiptError(f"staged artifact is not regular: {name}")
    if source_status.st_size != size:
        raise ReceiptError(f"staged artifact size drift: {name}")
    artifacts_descriptor = directory.artifacts(create=True)
    if artifacts_descriptor is None:  # pragma: no cover - create=True guarantees this
        raise ReceiptError("receipt artifact directory is unavailable")
    temporary_name = f".btb-repeat-{uuid.uuid4().hex}.tmp"
    handle = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    output_descriptor: int | None = None
    expected: os.stat_result | None = None
    try:
        actual = os.fstat(handle)
        if not browser_use_sandbox._same_entry(actual, source_status) or actual.st_size != size:
            raise ReceiptError(f"staged artifact changed while copying: {name}")
        hasher = hashlib.sha256()
        remaining = size
        output_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=artifacts_descriptor,
        )
        while remaining:
            chunk = os.read(handle, min(1024 * 1024, remaining))
            if not chunk:
                raise ReceiptError(f"staged artifact truncated while copying: {name}")
            _write_all(output_descriptor, chunk)
            hasher.update(chunk)
            remaining -= len(chunk)
        if os.read(handle, 1) or hasher.hexdigest() != digest:
            raise ReceiptError(f"staged artifact digest drift: {name}")
        os.fsync(output_descriptor)
        expected = _owned_file_stat(
            artifacts_descriptor,
            temporary_name,
            creation_descriptor=output_descriptor,
        )
        directory.verify()
    except Exception:
        if expected is None and output_descriptor is not None:
            expected = os.fstat(output_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
            output_descriptor = None
        if expected is not None:
            _unlink_owned(artifacts_descriptor, temporary_name, expected)
        raise
    finally:
        os.close(handle)
        if output_descriptor is not None:
            os.close(output_descriptor)
    if expected is None:  # pragma: no cover - the try block always binds it
        raise ReceiptError("publication temporary was not captured")
    return temporary_name, name, expected


def _prepare_publication(
    stage: Path,
    directory: Path,
    *,
    run_id: str,
    receipt: Mapping[str, object],
    expected_directory: os.stat_result | None = None,
) -> _Publication:
    held = _open_receipt_directory(
        directory,
        create=True,
        expected=expected_directory,
    )
    payload = (
        json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > _MAX_RECEIPT_BYTES:
        held.close()
        raise ReceiptError("staged receipt exceeds bounded size")
    artifacts: list[tuple[str, str, os.stat_result]] = []
    try:
        for name, (digest, size) in sorted(_artifact_metadata(receipt).items()):
            artifacts.append(
                _copy_artifact_to_temp(
                    stage / "artifacts" / name,
                    held,
                    name=name,
                    digest=digest,
                    size=size,
                )
            )
    except Exception:
        _discard_publication(_Publication(held, run_id, payload, artifacts))
        raise
    return _Publication(held, run_id, payload, artifacts)


def _discard_publication(publication: _Publication) -> None:
    artifacts_descriptor = publication.directory.artifacts_descriptor
    try:
        if artifacts_descriptor is None:
            artifacts_descriptor = publication.directory.artifacts(create=False)
        if artifacts_descriptor is not None:
            for temporary, _target, expected in publication.artifacts:
                _unlink_owned(artifacts_descriptor, temporary, expected)
            os.fsync(artifacts_descriptor)
    finally:
        publication.directory.close()


def _commit_publication(publication: _Publication) -> None:
    """Publish target-filesystem temps, with receipt JSON as the final marker."""

    committed: list[tuple[str, os.stat_result]] = []
    receipt_temp: str | None = None
    receipt_temp_expected: os.stat_result | None = None
    receipt_committed = False
    receipt_expected: os.stat_result | None = None
    receipt_target = f"{publication.run_id}.json"
    root_descriptor = publication.directory.descriptor
    artifacts_descriptor = publication.directory.artifacts_descriptor
    try:
        if artifacts_descriptor is None:
            artifacts_descriptor = publication.directory.artifacts(create=False)
        publication.directory.verify()
        for temporary, target, temporary_expected in publication.artifacts:
            if artifacts_descriptor is None:
                raise ReceiptError("receipt artifact directory is unavailable")
            publication.directory.verify()
            current_temporary = _owned_file_stat(artifacts_descriptor, temporary)
            if not browser_use_sandbox._same_entry(current_temporary, temporary_expected):
                raise ReceiptError(f"publication temporary changed: {temporary}")
            os.link(
                temporary,
                target,
                src_dir_fd=artifacts_descriptor,
                dst_dir_fd=artifacts_descriptor,
                follow_symlinks=False,
            )
            target_expected = _owned_file_stat(artifacts_descriptor, target)
            if not browser_use_sandbox._same_entry(target_expected, temporary_expected):
                raise ReceiptError(f"publication target changed: {target}")
            committed.append((target, target_expected))
            current_temporary = _owned_file_stat(artifacts_descriptor, temporary)
            if not browser_use_sandbox._same_entry(current_temporary, temporary_expected):
                raise ReceiptError(f"publication temporary changed: {temporary}")
            if not _unlink_owned(artifacts_descriptor, temporary, temporary_expected):
                raise ReceiptError(f"publication temporary changed: {temporary}")
            current_target = _owned_file_stat(artifacts_descriptor, target)
            if not browser_use_sandbox._same_entry(current_target, target_expected):
                raise ReceiptError(f"publication target changed: {target}")
            os.fsync(artifacts_descriptor)
            publication.directory.verify()
        publication.directory.verify()
        receipt_temp = f".btb-repeat-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            receipt_temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        try:
            _write_all(descriptor, publication.receipt_payload)
            os.fsync(descriptor)
            receipt_temp_expected = _owned_file_stat(
                root_descriptor,
                receipt_temp,
                creation_descriptor=descriptor,
            )
            publication.directory.verify()
        except Exception:
            if receipt_temp_expected is None:
                receipt_temp_expected = os.fstat(descriptor)
            raise
        finally:
            os.close(descriptor)
        os.link(
            receipt_temp,
            receipt_target,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        receipt_committed = True
        # Rollback ownership stays bound to the writer descriptor.  The target
        # pathname is only an independent observation, never an ownership source.
        receipt_expected = receipt_temp_expected
        current_receipt_target = _owned_file_stat(root_descriptor, receipt_target)
        if not browser_use_sandbox._same_entry(
            current_receipt_target, receipt_expected
        ):
            raise ReceiptError("receipt target changed during publication")
        current_receipt_temp = _owned_file_stat(root_descriptor, receipt_temp)
        if not browser_use_sandbox._same_entry(current_receipt_temp, receipt_temp_expected):
            raise ReceiptError("receipt temporary changed during publication")
        if not _unlink_owned(root_descriptor, receipt_temp, receipt_temp_expected):
            raise ReceiptError("receipt temporary changed during publication")
        os.fsync(root_descriptor)
        publication.directory.verify()
    except Exception:
        if receipt_temp is not None and receipt_temp_expected is not None:
            _unlink_owned(root_descriptor, receipt_temp, receipt_temp_expected)
        if receipt_committed and receipt_expected is not None:
            _unlink_owned(root_descriptor, receipt_target, receipt_expected)
        if artifacts_descriptor is not None:
            for path, expected in reversed(committed):
                _unlink_owned(artifacts_descriptor, path, expected)
            for temporary, _target, expected in publication.artifacts:
                _unlink_owned(artifacts_descriptor, temporary, expected)
            os.fsync(artifacts_descriptor)
        os.fsync(root_descriptor)
        raise
    finally:
        publication.directory.close()


def _stage_receipt(
    stage: Path,
    plan: StudyPlan,
    run: PlannedRun,
    source_repo: Path,
) -> dict[str, object]:
    indexed = validated_receipts(plan, stage, source_repo, require_complete=False)
    if set(indexed) != {run.run_id}:
        raise ReceiptError("staged worker output must contain exactly its planned receipt")
    return indexed[run.run_id]


def _bind_parent_filesystem_descriptor(
    builder: manifest_mod.ReceiptBuilder,
    condition: Condition,
    directory: _ReceiptDirectory,
    *,
    inventory: dict[str, object] | None,
    cleanup_error: Exception | None,
) -> list[tuple[str, str, os.stat_result]]:
    """Bind cleanup evidence while staging any inventory artifact by descriptor."""

    if condition.baseline not in {"browser-use", "browser-use-full"}:
        return []
    state = "cleanup_failed" if cleanup_error is not None else "cleaned"
    value: dict[str, object] = {
        "state": state,
        "cleanup_verified": state == "cleaned",
        "inventory": None,
        "cleanup_error": None,
    }
    artifacts: list[tuple[str, str, os.stat_result]] = []
    if inventory is not None:
        payload = manifest_mod.canonical_json_bytes(inventory) + b"\n"
        name = f"{builder.run_id}.browser-use-sandbox-inventory.json"
        temporary, target, expected = _write_bytes_to_temp(
            directory,
            name=name,
            payload=payload,
        )
        artifacts.append((temporary, target, expected))
        value["inventory"] = {
            "path": f"artifacts/{name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "entry_count": inventory.get("entry_count"),
            "file_count": inventory.get("file_count"),
            "total_bytes": inventory.get("total_bytes"),
            "inventory_sha256": inventory.get("inventory_sha256"),
        }
    if cleanup_error is not None:
        message, _redacted = manifest_mod.redact_text(
            str(cleanup_error), sensitive_values=builder.redaction_values
        )
        value["cleanup_error"] = {
            "type": cleanup_error.__class__.__name__,
            "message": message,
        }
    builder.framework_filesystem = value
    return artifacts


def _failure_payload(
    builder: manifest_mod.ReceiptBuilder,
    exc: Exception,
    *,
    stage: str,
    status: str,
) -> bytes:
    """Build the same terminal failure JSON as ``ReceiptBuilder.write_failure``."""

    builder._ensure_learned_filesystem_state()
    message, _redacted = manifest_mod.redact_text(
        str(exc), sensitive_values=builder.redaction_values
    )
    builder.evaluation = None
    builder.outcome = None
    payload = builder._receipt(
        status=status,  # type: ignore[arg-type]
        failure={
            "type": exc.__class__.__name__,
            "message": message,
            "stage": stage,
        },
    )
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )


def _write_parent_failure(
    builder: manifest_mod.ReceiptBuilder,
    condition: Condition,
    *,
    directory: Path,
    expected_directory: os.stat_result | None = None,
    exc: Exception,
    stage: str,
    status: str,
    inventory: dict[str, object] | None,
    inventory_error: Exception | None,
    cleanup_error: Exception | None,
) -> None:
    if inventory_error is not None:
        builder.record_evidence_failure(
            inventory_error, stage="parent_work_root_inventory"
        )
    if cleanup_error is not None:
        builder.record_evidence_failure(
            cleanup_error, stage="parent_work_root_cleanup"
        )
    held: _ReceiptDirectory | None = None
    publication: _Publication | None = None
    artifacts: list[tuple[str, str, os.stat_result]] = []
    try:
        held = _open_receipt_directory(
            directory,
            create=True,
            expected=expected_directory,
        )
        artifacts = _bind_parent_filesystem_descriptor(
            builder,
            condition,
            held,
            inventory=inventory,
            cleanup_error=cleanup_error,
        )
        payload = _failure_payload(builder, exc, stage=stage, status=status)
        if len(payload) > _MAX_RECEIPT_BYTES:
            raise ReceiptError("parent failure receipt exceeds bounded size")
        publication = _Publication(held, builder.run_id, payload, artifacts)
        _commit_publication(publication)
    except Exception as write_exc:
        if publication is not None and not publication.directory.closed:
            _discard_publication(publication)
        elif held is not None and not held.closed:
            _discard_publication(_Publication(held, builder.run_id, b"", artifacts))
        raise StudyExecutionError("parent could not publish its sole terminal receipt") from write_exc


def execute_plan(
    plan: StudyPlan | Mapping[str, object] | Path | str,
    *,
    receipt_dir: Path | str,
    executor: Executor | None = None,
    source_repo: Path | str = manifest_mod.REPO_ROOT,
    source_provenance: manifest_mod.SourceProvenance | None = None,
    clock: Clock = time.monotonic,
    _worker_timeout_cap_s: float | None = None,
) -> ExecutionReport:
    """Run/resume a plan; never start after the outer study deadline expires.

    Every attempt writes only to a parent-created system-temporary staging root.
    The parent validates staged output, verifies work-root cleanup, then links
    artifacts and the canonical receipt JSON last. A deadline or teardown fault
    discards staged success and receives one parent-owned terminal receipt.
    """

    plan, directory, source = _coerce_plan(plan), Path(receipt_dir), Path(source_repo)
    _execution_source(
        plan,
        source_repo=source,
        source_provenance=source_provenance,
    )
    _execution_runtime(plan)
    _ensure_receipt_directory(directory)
    try:
        directory_binding = directory.lstat()
    except OSError as exc:
        raise ReceiptError("cannot bind receipt directory") from exc
    runs = plan.runs
    existing = validated_receipts(plan, directory, source, require_complete=False)
    teardown_receipts = {
        run_id
        for run_id, receipt in existing.items()
        if isinstance(receipt.get("execution"), Mapping)
        and isinstance(receipt["execution"].get("failure"), Mapping)
        and receipt["execution"]["failure"].get("stage") == "worker_process_group_teardown"
    }
    if teardown_receipts:
        return ExecutionReport(
            plan.study_id,
            plan.plan_sha256,
            (),
            (),
            tuple(run.run_id for run in runs if run.run_id in existing),
            tuple(run.run_id for run in runs if run.run_id not in existing),
            "worker_process_group_teardown_failed",
        )
    consumed_s = sum(
        _receipt_duration(existing[run.run_id], directory / f"{run.run_id}.json")
        for run in runs
        if run.run_id in existing
    )
    # Completed receipts consume their recorded duration across resumes. The
    # current invocation also accounts for setup time before every new launch.
    deadline = clock() + (plan.study_wall_s - consumed_s)
    attempted: list[str] = []
    completed: list[str] = []
    skipped: list[str] = []
    for index, run in enumerate(runs):
        if run.run_id in existing:
            skipped.append(run.run_id)
            continue
        binding, condition = _task_and_condition(plan, run)
        # Do not even prepare an engine lifecycle when its full recorded wall
        # budget cannot fit.  Check again immediately before invocation because
        # provenance setup itself can consume study time.
        if deadline - clock() < binding.wall_s:
            return ExecutionReport(
                plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed),
                tuple(skipped),
                tuple(item.run_id for item in runs[index:] if item.run_id not in existing),
                "study_wall_deadline_exhausted",
            )
        # Detect any concurrent/foreign layout change before creating either a
        # builder or a child work root. A newly published planned receipt is a
        # separate resume invocation, never an implicit concurrent handoff.
        if set(validated_receipts(plan, directory, source, require_complete=False)) != set(existing):
            raise ReceiptError("receipt directory changed during execution")
        task = _current_task(binding)
        _require_current_framework(condition)
        builder = _parent_builder(plan, task, run, condition, directory)
        if deadline - clock() < binding.wall_s:
            return ExecutionReport(
                plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed),
                tuple(skipped),
                tuple(item.run_id for item in runs[index:] if item.run_id not in existing),
                "study_wall_deadline_exhausted",
            )
        attempted.append(run.run_id)
        worker_timed_out = False
        worker_teardown_failed = False
        cause: Exception | None = None
        work_root, stage = _new_staging_root()
        staged_receipt: dict[str, object] | None = None
        publication: _Publication | None = None
        try:
            staged_builder = _parent_builder(plan, task, run, condition, stage)
            if executor is None:
                timeout_s = min(binding.wall_s, deadline - clock())
                if _worker_timeout_cap_s is not None:
                    timeout_s = min(timeout_s, _worker_timeout_cap_s)
                result = _run_worker_process(
                    _worker_request(plan, task, run, condition, stage),
                    timeout_s=timeout_s,
                    provider=condition.provider,
                    work_root=work_root,
                )
                worker_teardown_failed = result.teardown_error
                worker_timed_out = result.timed_out and not worker_teardown_failed
                if result.teardown_error:
                    cause = RuntimeError("repetition worker process group did not quiesce")
                elif result.return_code not in {0, None}:
                    cause = RuntimeError("repetition worker exited without a receipt")
                elif result.return_code is None:
                    cause = RuntimeError("repetition worker could not start")
            else:
                try:
                    executor(
                        baseline=condition.baseline,
                        task=task,
                        run_id=run.run_id,
                        provider=condition.provider,
                        model=condition.model,
                        max_steps=condition.max_steps,
                        receipt_options=engine.ReceiptOptions(mode="canonical", out_dir=stage),
                        receipt_builder=staged_builder,
                    )
                except Exception as exc:  # noqa: BLE001 - injected offline test seam
                    cause = exc
            if (directory / f"{run.run_id}.json").exists():
                raise StudyExecutionError("worker or injected executor wrote directly to canonical receipt_dir")
            worker_timed_out = worker_timed_out or deadline - clock() < 0
            if not worker_timed_out and not worker_teardown_failed:
                try:
                    staged_receipt = _stage_receipt(stage, plan, run, source)
                    publication = _prepare_publication(
                        stage,
                        directory,
                        run_id=run.run_id,
                        receipt=staged_receipt,
                        expected_directory=directory_binding,
                    )
                except Exception as exc:  # noqa: BLE001 - parent owns fallback receipt
                    cause = cause or exc
                    staged_receipt = None
                worker_timed_out = deadline - clock() < 0
            inventory, inventory_error, cleanup_error = _cleanup_root(work_root)
            work_root = None  # cleanup is required before any canonical publication
        except Exception:
            if work_root is not None:
                _cleanup_root(work_root)
            if publication is not None:
                _discard_publication(publication)
            raise
        if cleanup_error is not None:
            if publication is not None:
                _discard_publication(publication)
            _write_parent_failure(
                builder,
                condition,
                directory=directory,
                expected_directory=directory_binding,
                exc=cleanup_error,
                stage="parent_work_root_cleanup",
                status="setup_error",
                inventory=inventory,
                inventory_error=inventory_error,
                cleanup_error=cleanup_error,
            )
            completed.append(run.run_id)
            return ExecutionReport(
                plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed), tuple(skipped),
                tuple(item.run_id for item in runs[index + 1 :] if item.run_id not in existing),
                "parent_work_root_cleanup_failed",
            )
        if inventory_error is not None:
            if publication is not None:
                _discard_publication(publication)
            _write_parent_failure(
                builder,
                condition,
                directory=directory,
                expected_directory=directory_binding,
                exc=inventory_error,
                stage="parent_work_root_inventory",
                status="setup_error",
                inventory=inventory,
                inventory_error=inventory_error,
                cleanup_error=None,
            )
            completed.append(run.run_id)
            return ExecutionReport(
                plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed), tuple(skipped),
                tuple(item.run_id for item in runs[index + 1 :] if item.run_id not in existing),
                "parent_work_root_inventory_failed",
            )
        if worker_timed_out:
            if publication is not None:
                _discard_publication(publication)
            _write_parent_failure(
                builder,
                condition,
                directory=directory,
                expected_directory=directory_binding,
                exc=TimeoutError("repetition worker exceeded the outer study deadline"),
                stage="outer_study_deadline",
                status="timeout",
                inventory=inventory,
                inventory_error=inventory_error,
                cleanup_error=cleanup_error,
            )
            completed.append(run.run_id)
            return ExecutionReport(
                plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed), tuple(skipped),
                tuple(item.run_id for item in runs[index + 1 :] if item.run_id not in existing),
                "study_wall_deadline_exhausted",
            )
        if worker_teardown_failed or cleanup_error is not None:
            if publication is not None:
                _discard_publication(publication)
            _write_parent_failure(
                builder,
                condition,
                directory=directory,
                expected_directory=directory_binding,
                exc=cause or RuntimeError("repetition worker process group did not quiesce"),
                stage="worker_process_group_teardown",
                status="setup_error",
                inventory=inventory,
                inventory_error=inventory_error,
                cleanup_error=cleanup_error,
            )
            completed.append(run.run_id)
            return ExecutionReport(
                plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed), tuple(skipped),
                tuple(item.run_id for item in runs[index + 1 :] if item.run_id not in existing),
                "worker_process_group_teardown_failed",
            )
        if staged_receipt is None or publication is None:
            _write_parent_failure(
                builder,
                condition,
                directory=directory,
                expected_directory=directory_binding,
                exc=cause or RuntimeError("worker returned without a staged terminal receipt"),
                stage="repetition_executor_unhandled",
                status="setup_error",
                inventory=inventory,
                inventory_error=inventory_error,
                cleanup_error=None,
            )
            completed.append(run.run_id)
            continue
        try:
            _commit_publication(publication)
        except Exception as exc:  # noqa: BLE001 - publication fallback is terminal
            _write_parent_failure(
                builder,
                condition,
                directory=directory,
                expected_directory=directory_binding,
                exc=exc,
                stage="parent_receipt_publication",
                status="setup_error",
                inventory=inventory,
                inventory_error=inventory_error,
                cleanup_error=None,
            )
            completed.append(run.run_id)
            continue
        completed.append(run.run_id)
        existing[run.run_id] = staged_receipt
    return ExecutionReport(
        plan.study_id, plan.plan_sha256, tuple(attempted), tuple(completed), tuple(skipped), (), None
    )


def _parse_condition(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, Mapping):
            raise PlanError("condition must be a JSON object")
        return Condition.from_mapping(parsed).input_dict()
    except (json.JSONDecodeError, PlanError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="create or verify an immutable canonical plan")
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--study-id", required=True)
    plan.add_argument("--seed", required=True)
    plan.add_argument("--task", action="append", required=True, choices=task_runner.PILOT_TASKS)
    plan.add_argument("--condition", action="append", required=True, type=_parse_condition)
    plan.add_argument("--repetitions", type=int, required=True)
    plan.add_argument("--study-wall-s", type=float, required=True)
    plan.add_argument("--source-repo", type=Path, default=manifest_mod.REPO_ROOT)
    run = commands.add_parser("run", help="execute or resume an immutable plan")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--receipt-dir", type=Path, required=True)
    run.add_argument("--source-repo", type=Path, default=manifest_mod.REPO_ROOT)
    summary = commands.add_parser("summarize", help="validate and summarize an exact complete plan")
    summary.add_argument("--plan", type=Path, required=True)
    summary.add_argument("--receipt-dir", type=Path, required=True)
    summary.add_argument("--csv", type=Path, required=True)
    summary.add_argument("--markdown", type=Path, required=True)
    summary.add_argument("--source-repo", type=Path, default=manifest_mod.REPO_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan = create_plan(
                args.plan, study_id=args.study_id, seed=args.seed, task_ids=args.task,
                conditions=args.condition, repetitions=args.repetitions, study_wall_s=args.study_wall_s,
                source_repo=args.source_repo,
            )
            print(json.dumps({"plan": str(args.plan), "plan_sha256": plan.plan_sha256}, sort_keys=True))
        elif args.command == "run":
            report = execute_plan(args.plan, receipt_dir=args.receipt_dir, source_repo=args.source_repo)
            print(json.dumps(report.to_dict(), sort_keys=True))
            return int(report.stopped_reason is not None)
        else:
            result = __getattr__("aggregate_study")(
                args.plan, receipt_dir=args.receipt_dir, csv_path=args.csv,
                markdown_path=args.markdown, source_repo=args.source_repo,
            )
            print(json.dumps({"csv": str(result.csv_path), "markdown": str(result.markdown_path), "summary_sha256": result.data["summary_sha256"]}, sort_keys=True))
        return 0
    except RepetitionError as exc:
        print(f"btb-repeat: {exc}", file=sys.stderr)
        return 1


def __getattr__(name: str):
    if name in {
        "AggregationResult",
        "aggregate_receipts",
        "aggregate_study",
        "write_summaries",
        "wilson_interval",
        "WILSON_METHOD",
        "WILSON_CONFIDENCE_LEVEL",
        "WILSON_Z",
        "SUMMARY_DECIMAL_PLACES",
    }:
        from btb.harness import repetition_summary

        return getattr(repetition_summary, name)
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
