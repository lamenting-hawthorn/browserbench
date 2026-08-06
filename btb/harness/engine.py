"""Harness engine: orchestrate one isolated, receipted benchmark run."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from btb.baselines import play as play_baseline
from btb.harness import inject as inject_mod
from btb.harness import manifest as manifest_mod
from btb.harness import runtime
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner

DEFAULT_BASE_URL = "http://127.0.0.1:7788"
PROXY_QUIESCENCE_TIMEOUT_S = 10.0
RunMode = Literal["exploratory", "canonical"]

_PROVIDER_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-0",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4.1-mini",
}
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


@dataclass(frozen=True)
class ReceiptOptions:
    """Receipt destination and provenance strictness for one run."""

    mode: RunMode = "exploratory"
    out_dir: Path | None = None

    @property
    def canonical(self) -> bool:
        return self.mode == "canonical"


@dataclass(frozen=True)
class BrowserUseConfig:
    """One resolved configuration used for construction and provenance."""

    provider: str
    model: str
    max_steps: int
    wall_s: float
    temperature: float = 0.0
    max_output_tokens: int = 4096
    provider_retries: int = 0
    excluded_actions: tuple[str, ...] = _BROWSER_USE_EXCLUDED_ACTIONS


@dataclass(frozen=True)
class _CompletedRun:
    claim: claim_mod.Claim
    after: score_mod.OracleSnapshot
    evaluation: score_mod.Evaluation


class _RunExecutionError(RuntimeError):
    """Internal wrapper retaining the exact failed stage and original error."""

    def __init__(self, cause: Exception, *, stage: str) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.stage = stage


def _canonical_database_path(db_path: Path | str) -> Path:
    return Path(db_path).expanduser().resolve()


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _playwright_provenance(
    behavior: str,
    *,
    action_timeout_ms: float,
) -> manifest_mod.BaselineProvenance:
    return manifest_mod.BaselineProvenance(
        name="playwright-exact" if behavior == "exact" else "playwright-naive",
        framework_name="playwright",
        framework_version=_installed_version("playwright"),
        provider=None,
        model="deterministic-playwright",
        parameters={
            "behavior": behavior,
            "headless": True,
            "action_timeout_ms": action_timeout_ms,
            "global_wall_budget_enforced": False,
        },
        modality_policy={"dom": True, "vision": False},
        capability_policy={
            "visible_page_controls_only": True,
            "javascript_console": False,
            "direct_api": False,
            "filesystem": False,
            "database": False,
            "enforcement": {
                "fixture_write_api_requires_ui_token": True,
                "playwright_driver": "fixed_procedure",
            },
        },
    )


def resolve_browser_use_config(
    task: dict,
    *,
    provider: str | None,
    model: str | None,
    max_steps: int | None,
) -> BrowserUseConfig:
    resolved_provider = (provider or os.environ.get("BTB_LLM", "deepseek")).strip().lower()
    if resolved_provider not in _PROVIDER_DEFAULT_MODELS:
        raise ValueError(f"unsupported Browser Use LLM provider: {resolved_provider}")
    resolved_model = (
        model
        or os.environ.get("BTB_MODEL")
        or _PROVIDER_DEFAULT_MODELS[resolved_provider]
    )
    if not isinstance(resolved_model, str) or not resolved_model.strip():
        raise ValueError("Browser Use model must be a non-empty string")
    resolved_model = resolved_model.strip()
    budget = task.get("budget") or {}
    resolved_steps = max_steps if max_steps is not None else budget.get("steps", 20)
    wall_s = budget.get("wall_s", 120)
    if isinstance(resolved_steps, bool) or not isinstance(resolved_steps, int):
        raise TypeError("max_steps must be an integer")
    if resolved_steps < 1:
        raise ValueError("max_steps must be positive")
    if isinstance(wall_s, bool) or not isinstance(wall_s, (int, float)):
        raise TypeError("task budget.wall_s must be numeric")
    if wall_s <= 0:
        raise ValueError("task budget.wall_s must be positive")
    return BrowserUseConfig(
        provider=resolved_provider,
        model=resolved_model,
        max_steps=resolved_steps,
        wall_s=float(wall_s),
    )


def _browser_use_provenance(
    config: BrowserUseConfig,
) -> manifest_mod.BaselineProvenance:
    return manifest_mod.BaselineProvenance(
        name="browser-use",
        framework_name="browser-use",
        framework_version=_installed_version("browser-use"),
        provider=config.provider,
        model=config.model,
        parameters={
            "max_steps": config.max_steps,
            "wall_s": config.wall_s,
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
            "excluded_actions": list(config.excluded_actions),
            "llm_generation": {
                "temperature": config.temperature,
                "max_output_tokens": config.max_output_tokens,
                "provider_retries": config.provider_retries,
                "top_p": None,
                "seed": None,
            },
        },
        modality_policy={"dom": True, "vision": False},
        capability_policy={
            "visible_page_controls_only": True,
            "javascript_console": False,
            "direct_api": False,
            "filesystem": False,
            "database": False,
            "enforcement": {
                "agent_tools_allowed": list(_BROWSER_USE_ALLOWED_ACTIONS),
                "agent_tools_excluded": list(config.excluded_actions),
                "fixture_write_api_requires_ui_token": True,
                "navigation": "run_fixture_origin_only",
                "telemetry": False,
            },
        },
    )


def make_receipt_builder(
    *,
    task: dict,
    run_id: str,
    baseline: manifest_mod.BaselineProvenance,
    options: ReceiptOptions | None = None,
    configured_steps: int | None,
) -> manifest_mod.ReceiptBuilder:
    """Create provenance before setup so setup failures can still be receipted."""

    receipt_options = options or ReceiptOptions()
    budget = task.get("budget") or {}
    return manifest_mod.ReceiptBuilder(
        run_id=run_id,
        freeze=str(task.get("freeze", "unknown")),
        baseline=baseline,
        configured_steps=configured_steps,
        configured_wall_s=budget.get("wall_s"),
        canonical_requested=receipt_options.canonical,
        task_definition=task,
        prompt_text=None,
        out_dir=receipt_options.out_dir,
    )


def receipt_builder_for(
    *,
    task: dict,
    run_id: str,
    baseline: str,
    provider: str | None,
    model: str | None,
    max_steps: int | None,
    options: ReceiptOptions | None = None,
) -> manifest_mod.ReceiptBuilder:
    """Resolve one CLI baseline into the provenance used by the engine."""

    if baseline == "browser-use":
        config = resolve_browser_use_config(
            task,
            provider=provider,
            model=model,
            max_steps=max_steps,
        )
        configured_steps = config.max_steps
        provenance = _browser_use_provenance(config)
    else:
        configured_steps = None
        behavior = "exact" if baseline.endswith("exact") else "naive_retry"
        provenance = _playwright_provenance(
            behavior,
            action_timeout_ms=float((task.get("budget") or {})["wall_s"]) * 1_000,
        )
    return make_receipt_builder(
        task=task,
        run_id=run_id,
        baseline=provenance,
        options=options,
        configured_steps=configured_steps,
    )


def _set_prompt_once(builder: manifest_mod.ReceiptBuilder, prompt: str) -> None:
    if builder.prompt_text is None:
        builder.set_prompt(prompt)
    elif builder.prompt_text != prompt:
        raise ValueError("receipt prompt does not match the executed prompt")


def _make_proxy(task: dict, base_url: str) -> inject_mod.InjectProxy | None:
    injection = task.get("failure_injection") or {}
    if injection.get("kind") != "disconnect_after_possible_send":
        return None
    return inject_mod.InjectProxy(
        base_url,
        inject_send=True,
        inject_after_committed=int(injection.get("after_nth_committed", 1)),
        upstream_timeout=float(injection.get("upstream_timeout_ms", 10_000)) / 1_000,
    ).start()


def _target_url(proxy: inject_mod.InjectProxy | None, base_url: str) -> str:
    return proxy.url if proxy is not None else base_url


def _fixture_origin_pattern(base_url: str) -> str:
    """Return the exact HTTP(S) origin pattern accepted by Browser Use."""

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.netloc:
        raise ValueError(f"fixture URL must identify an HTTP(S) origin: {base_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}/"


def _disable_browser_use_telemetry() -> None:
    """Enforce the benchmark's no-framework-telemetry capability boundary."""

    os.environ["ANONYMIZED_TELEMETRY"] = "false"


def _reject_agent_overrides(agent_kwargs: dict) -> None:
    if agent_kwargs:
        raise ValueError(
            "agent_kwargs are not accepted because unreceipted overrides would "
            "change the learned-baseline condition: "
            + ", ".join(sorted(agent_kwargs))
        )


def _make_browser_tools(tools_class, config: BrowserUseConfig):
    """Construct tools and fail closed if the framework action set drifts."""

    tools = tools_class(exclude_actions=list(config.excluded_actions))
    actions = getattr(
        getattr(getattr(tools, "registry", None), "registry", None),
        "actions",
        None,
    )
    if not isinstance(actions, dict):
        raise RuntimeError("Browser Use tools do not expose an auditable action registry")
    effective = tuple(sorted(actions))
    expected = tuple(sorted(_BROWSER_USE_ALLOWED_ACTIONS))
    if effective != expected:
        added = sorted(set(effective) - set(expected))
        missing = sorted(set(expected) - set(effective))
        raise RuntimeError(
            "Browser Use action registry differs from the frozen capability policy: "
            f"unexpected={added!r}, missing={missing!r}"
        )
    return tools


def _injection_report(proxy: inject_mod.InjectProxy | None) -> dict:
    if proxy is None:
        return {"injection": "none", "treatment_delivered": False}
    if not proxy.wait_for_quiescence(timeout=PROXY_QUIESCENCE_TIMEOUT_S):
        raise TimeoutError(
            "injection proxy did not become quiescent within "
            f"{PROXY_QUIESCENCE_TIMEOUT_S:.1f}s"
        )
    report, _redacted = manifest_mod.redact_value(proxy.report())
    if not isinstance(report, dict):
        raise TypeError("sanitized injection report must remain an object")
    return report


def _manifest_claim(
    claim: claim_mod.Claim,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict:
    result = claim.to_dict()
    # ``Claim.to_dict`` retains the unmodified model output for in-memory
    # diagnostics.  Persist only its digest and sanitized representation.
    result.pop("raw", None)
    detail, redacted = manifest_mod.redact_text(
        claim.raw,
        sensitive_values=sensitive_values,
    )
    result["detail"] = detail
    result["detail_sha256"] = manifest_mod.prompt_sha256(claim.raw)
    result["detail_redacted"] = redacted
    return result


def _success_result(
    *,
    run_id: str,
    task: dict,
    claim: claim_mod.Claim,
    after: score_mod.OracleSnapshot,
    evaluation: score_mod.Evaluation,
    receipt_path: Path,
) -> dict:
    axes = evaluation.to_dict()
    public_claim = claim.to_dict()
    public_claim.pop("raw", None)
    return {
        "status": "success",
        "run_id": run_id,
        "task": task["id"],
        "outcome": evaluation.headline_outcome,
        "claim": public_claim,
        "claimed_send": claim.claimed_send,
        "db_state": after.to_dict(),
        "evaluation": axes,
        "functional_status": axes["functional_status"],
        "effect_state": axes["effect_state"],
        "authorization_violations": axes["authorization_violations"],
        "duplicate_attempt_count": axes["duplicate_attempt_count"],
        "belief": axes["belief"],
        "treatment_delivered": axes["treatment_delivered"],
        "receipt_path": str(receipt_path),
    }


def _capture_failure_evidence(
    *,
    builder: manifest_mod.ReceiptBuilder,
    database_path: Path,
    proxy: inject_mod.InjectProxy | None,
) -> None:
    if builder.injection_report is None:
        try:
            builder.injection_report = _injection_report(proxy)
        except Exception as evidence_error:  # evidence error is retained, not hidden
            builder.record_evidence_failure(evidence_error, stage="injection_report")
    if builder.before_snapshot is not None and builder.after_snapshot is None:
        try:
            builder.after_snapshot = score_mod.snapshot(database_path).to_dict()
        except Exception as evidence_error:  # evidence error is retained, not hidden
            builder.record_evidence_failure(evidence_error, stage="after_snapshot")


def _failure_result(
    *,
    builder: manifest_mod.ReceiptBuilder,
    exc: Exception,
    stage: str,
) -> dict:
    message, _redacted = manifest_mod.redact_text(
        str(exc),
        sensitive_values=builder.redaction_values,
    )
    receipt_path: Path | None = None
    receipt_error: dict[str, str] | None = None
    try:
        receipt_path = builder.write_failure(exc, stage=stage)
    except Exception as write_error:
        write_message, _write_redacted = manifest_mod.redact_text(
            str(write_error),
            sensitive_values=builder.redaction_values,
        )
        receipt_error = {
            "type": write_error.__class__.__name__,
            "message": write_message,
            "stage": "receipt_write",
        }
    return {
        "status": "failure",
        "run_id": builder.run_id,
        "task": (
            builder.task_definition.get("id")
            if isinstance(builder.task_definition, dict)
            else None
        ),
        "outcome": None,
        "error": {
            "type": exc.__class__.__name__,
            "message": message,
            "stage": stage,
        },
        "receipt_path": str(receipt_path) if receipt_path is not None else None,
        "receipt_error": receipt_error,
    }


def _evaluate_success(
    *,
    builder: manifest_mod.ReceiptBuilder,
    task: dict,
    before: score_mod.OracleSnapshot,
    after: score_mod.OracleSnapshot,
    claim: claim_mod.Claim,
    manifest_claim: dict,
    injection_report: dict,
) -> score_mod.Evaluation:
    evaluation = score_mod.evaluate(
        task,
        before,
        after,
        claim,
        treatment_delivered=injection_report.get("treatment_delivered") is True,
        injection_report=injection_report,
    )
    builder.before_snapshot = before.to_dict()
    builder.after_snapshot = after.to_dict()
    builder.agent_claim = manifest_claim
    builder.injection_report = injection_report
    builder.evaluation = evaluation.to_dict()
    builder.outcome = evaluation.headline_outcome
    return evaluation


class _ProxyScope:
    """Own proxy startup/teardown without replacing a primary exception."""

    def __init__(
        self,
        *,
        task: dict,
        base_url: str,
        builder: manifest_mod.ReceiptBuilder,
    ) -> None:
        self.task = task
        self.base_url = base_url
        self.builder = builder
        self.proxy: inject_mod.InjectProxy | None = None

    def __enter__(self) -> inject_mod.InjectProxy | None:
        self.proxy = _make_proxy(self.task, self.base_url)
        return self.proxy

    def __exit__(self, exc_type, exc_value, traceback) -> Literal[False]:
        del traceback
        if self.proxy is None:
            return False
        try:
            self.proxy.shutdown()
        except Exception as cleanup_error:
            if exc_type is None:
                raise
            self.builder.record_evidence_failure(
                cleanup_error,
                stage="proxy_teardown",
            )
            if exc_value is not None and hasattr(exc_value, "add_note"):
                exc_value.add_note(f"proxy teardown also failed: {cleanup_error}")
        return False


def _execute_playwright(
    *,
    task: dict,
    base_url: str,
    database_path: Path,
    behavior: str,
    builder: manifest_mod.ReceiptBuilder,
) -> _CompletedRun:
    from playwright.sync_api import sync_playwright

    stage = "initial_state"
    scope = _ProxyScope(task=task, base_url=base_url, builder=builder)
    try:
        task_runner.prepare_initial_state(database_path, task=task)
        before = score_mod.snapshot(database_path)
        builder.before_snapshot = before.to_dict()

        stage = "injection_setup"
        with scope as proxy:
            target = _target_url(proxy, base_url)
            procedure = (
                f"deterministic-control-v1 behavior={behavior}; "
                f"task={task['id']}; visible UI at {target}/"
            )
            _set_prompt_once(builder, procedure)

            stage = "baseline"
            with tempfile.TemporaryDirectory(prefix="btb-playwright-trace-") as raw_dir:
                raw_trace_path = Path(raw_dir) / "trace.zip"
                errors: list[tuple[str, Exception]] = []
                trace_stopped = False
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    try:
                        context.tracing.start(
                            screenshots=True,
                            snapshots=True,
                            sources=False,
                        )
                        page = context.new_page()
                        action_timeout_ms = float(task["budget"]["wall_s"]) * 1_000
                        page.set_default_timeout(action_timeout_ms)
                        page.set_default_navigation_timeout(action_timeout_ms)
                        control_report = play_baseline.run_control(
                            page,
                            target,
                            task,
                            behavior=behavior,
                        )
                    except Exception as exc:
                        errors.append(("baseline", exc))
                    finally:
                        try:
                            context.tracing.stop(path=str(raw_trace_path))
                            trace_stopped = True
                        except Exception as exc:
                            errors.append(("playwright_trace", exc))
                        for cleanup_stage, cleanup in (
                            ("playwright_context_close", context.close),
                            ("playwright_browser_close", browser.close),
                        ):
                            try:
                                cleanup()
                            except Exception as exc:
                                errors.append((cleanup_stage, exc))
                if trace_stopped:
                    try:
                        trace_redacted = manifest_mod.redact_playwright_trace(
                            raw_trace_path,
                            sensitive_values=builder.redaction_values,
                        )
                        trace_path = manifest_mod.write_artifact(
                            raw_trace_path.read_bytes(),
                            run_id=builder.run_id,
                            suffix="playwright-trace.zip",
                            out_dir=builder.artifact_directory(),
                        )
                        builder.bind_binary_trace(
                            trace_path,
                            kind="playwright",
                            format_name="playwright-trace-zip",
                            redacted=trace_redacted,
                        )
                    except Exception as exc:
                        errors.append(("playwright_trace_publish", exc))
                if errors:
                    primary_stage, primary_error = errors[0]
                    for secondary_stage, secondary_error in errors[1:]:
                        builder.record_evidence_failure(
                            secondary_error,
                            stage=secondary_stage,
                        )
                        if hasattr(primary_error, "add_note"):
                            primary_error.add_note(
                                f"{secondary_stage} also failed: {secondary_error}"
                            )
                    if primary_stage != "baseline":
                        stage = primary_stage
                    raise primary_error

            stage = "injection_quiescence"
            injection_report = _injection_report(proxy)
            stage = "after_snapshot"
            after = score_mod.snapshot(database_path)
            stage = "evaluation"
            claim = claim_mod.claim_from_mapping(control_report)
            evaluation = _evaluate_success(
                builder=builder,
                task=task,
                before=before,
                after=after,
                claim=claim,
                manifest_claim=_manifest_claim(
                    claim,
                    sensitive_values=builder.redaction_values,
                ),
                injection_report=injection_report,
            )
            stage = "proxy_teardown"
        return _CompletedRun(claim=claim, after=after, evaluation=evaluation)
    except Exception as exc:
        _capture_failure_evidence(
            builder=builder,
            database_path=database_path,
            proxy=scope.proxy,
        )
        raise _RunExecutionError(exc, stage=stage) from exc


def _history_payload(history: object) -> object:
    dump = getattr(history, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError("Browser Use history does not expose complete model_dump data")


def _execute_browser_use(
    *,
    task: dict,
    base_url: str,
    database_path: Path,
    config: BrowserUseConfig,
    builder: manifest_mod.ReceiptBuilder,
    agent_kwargs: dict,
) -> _CompletedRun:
    _reject_agent_overrides(agent_kwargs)
    _disable_browser_use_telemetry()
    from browser_use import Agent, Browser, Tools

    stage = "initial_state"
    scope = _ProxyScope(task=task, base_url=base_url, builder=builder)
    try:
        task_runner.prepare_initial_state(database_path, task=task)
        before = score_mod.snapshot(database_path)
        builder.before_snapshot = before.to_dict()

        stage = "injection_setup"
        with scope as proxy:
            target = _target_url(proxy, base_url)
            instruction = _augment_instruction(task, target)
            _set_prompt_once(builder, instruction)

            stage = "baseline"
            llm = _make_llm(config)

            async def execute_agent() -> tuple[str, object]:
                browser = Browser(
                    headless=True,
                    accept_downloads=False,
                    allowed_domains=[_fixture_origin_pattern(target)],
                    captcha_solver=False,
                    cross_origin_iframes=False,
                    deterministic_rendering=True,
                    enable_default_extensions=False,
                    permissions=[],
                )
                primary_error: BaseException | None = None
                try:
                    tools = _make_browser_tools(Tools, config)
                    agent = Agent(
                        task=instruction,
                        browser=browser,
                        llm=llm,
                        tools=tools,
                        directly_open_url=True,
                        available_file_paths=[],
                        use_judge=False,
                        use_vision=False,
                        **agent_kwargs,
                    )
                    history = await asyncio.wait_for(
                        agent.run(max_steps=config.max_steps),
                        timeout=config.wall_s,
                    )
                    final_result = getattr(history, "final_result", None)
                    final_answer = final_result() if callable(final_result) else None
                    return (
                        "" if final_answer is None else str(final_answer),
                        _history_payload(history),
                    )
                except BaseException as exc:
                    primary_error = exc
                    raise
                finally:
                    try:
                        await browser.close()
                    except Exception as cleanup_error:
                        if primary_error is None:
                            raise
                        builder.record_evidence_failure(
                            cleanup_error,
                            stage="browser_use_browser_close",
                        )
                        if hasattr(primary_error, "add_note"):
                            primary_error.add_note(
                                f"browser_use_browser_close also failed: {cleanup_error}"
                            )

            raw, history_payload = asyncio.run(execute_agent())
            builder.write_json_trace(history_payload, kind="browser-use-history")

            stage = "injection_quiescence"
            injection_report = _injection_report(proxy)
            stage = "after_snapshot"
            after = score_mod.snapshot(database_path)
            stage = "evaluation"
            claim = claim_mod.parse_claim(raw)
            evaluation = _evaluate_success(
                builder=builder,
                task=task,
                before=before,
                after=after,
                claim=claim,
                manifest_claim=_manifest_claim(
                    claim,
                    sensitive_values=builder.redaction_values,
                ),
                injection_report=injection_report,
            )
            stage = "proxy_teardown"
        return _CompletedRun(claim=claim, after=after, evaluation=evaluation)
    except Exception as exc:
        _capture_failure_evidence(
            builder=builder,
            database_path=database_path,
            proxy=scope.proxy,
        )
        raise _RunExecutionError(exc, stage=stage) from exc


def _execute_baseline(
    *,
    baseline: str,
    task: dict,
    base_url: str,
    database_path: Path,
    builder: manifest_mod.ReceiptBuilder,
    provider: str | None,
    model: str | None,
    max_steps: int | None,
    agent_kwargs: dict | None = None,
) -> _CompletedRun:
    if baseline == "browser-use":
        config = resolve_browser_use_config(
            task,
            provider=provider,
            model=model,
            max_steps=max_steps,
        )
        return _execute_browser_use(
            task=task,
            base_url=base_url,
            database_path=database_path,
            config=config,
            builder=builder,
            agent_kwargs=dict(agent_kwargs or {}),
        )
    behavior = "exact" if baseline == "playwright-exact" else "naive_retry"
    return _execute_playwright(
        task=task,
        base_url=base_url,
        database_path=database_path,
        behavior=behavior,
        builder=builder,
    )


def _finalize_run(
    *,
    builder: manifest_mod.ReceiptBuilder,
    task: dict,
    completed: _CompletedRun | None,
    failure: tuple[Exception, str] | None,
) -> dict:
    if failure is not None:
        exc, stage = failure
        return _failure_result(builder=builder, exc=exc, stage=stage)
    if completed is None:
        return _failure_result(
            builder=builder,
            exc=RuntimeError("run ended without a result"),
            stage="orchestration",
        )
    try:
        receipt_path = builder.write_success()
    except Exception as exc:
        return _failure_result(builder=builder, exc=exc, stage="receipt_write")
    return _success_result(
        run_id=builder.run_id,
        task=task,
        claim=completed.claim,
        after=completed.after,
        evaluation=completed.evaluation,
        receipt_path=receipt_path,
    )


def run_external(
    *,
    baseline: str,
    task: dict,
    run_id: str,
    base_url: str,
    db_path: Path | str,
    model: str | None = None,
    provider: str | None = None,
    max_steps: int | None = None,
    receipt_options: ReceiptOptions | None = None,
    receipt_builder: manifest_mod.ReceiptBuilder | None = None,
    agent_kwargs: dict | None = None,
) -> dict:
    """Run against an explicitly identified external fixture and receipt once."""

    builder = receipt_builder or receipt_builder_for(
        task=task,
        run_id=run_id,
        baseline=baseline,
        provider=provider,
        model=model,
        max_steps=max_steps,
        options=receipt_options,
    )
    database_path = _canonical_database_path(db_path)
    completed: _CompletedRun | None = None
    failure: tuple[Exception, str] | None = None
    try:
        builder.ensure_canonical_source()
        runtime.verify_fixture_identity(
            base_url,
            database_path,
            verify_run_id=False,
        )
        try:
            completed = _execute_baseline(
                baseline=baseline,
                task=task,
                base_url=base_url,
                database_path=database_path,
                builder=builder,
                provider=provider,
                model=model,
                max_steps=max_steps,
                agent_kwargs=agent_kwargs,
            )
        except _RunExecutionError as exc:
            failure = (exc.cause, exc.stage)
        except Exception as exc:
            failure = (exc, "baseline")
    except _RunExecutionError as exc:
        failure = (exc.cause, exc.stage)
    except Exception as exc:
        failure = (exc, "fixture_identity")
    return _finalize_run(
        builder=builder,
        task=task,
        completed=completed,
        failure=failure,
    )


def run_managed(
    *,
    baseline: str,
    task: dict,
    run_id: str,
    model: str | None = None,
    provider: str | None = None,
    max_steps: int | None = None,
    receipt_options: ReceiptOptions | None = None,
    receipt_builder: manifest_mod.ReceiptBuilder | None = None,
    agent_kwargs: dict | None = None,
) -> dict:
    """Own fixture, proxy, execution, teardown, and exactly one final receipt."""

    builder = receipt_builder or receipt_builder_for(
        task=task,
        run_id=run_id,
        baseline=baseline,
        provider=provider,
        model=model,
        max_steps=max_steps,
        options=receipt_options,
    )
    completed: _CompletedRun | None = None
    execution_failure: tuple[Exception, str] | None = None
    fixture_failure: tuple[Exception, str] | None = None
    entered = False

    try:
        builder.ensure_canonical_source()
    except Exception as exc:
        fixture_failure = (exc, "source")

    if fixture_failure is None:
        try:
            environment = runtime.managed_run_environment(run_id=run_id)
            with environment:
                entered = True
                builder.register_sensitive_value(environment.ui_token)
                try:
                    completed = _execute_baseline(
                        baseline=baseline,
                        task=task,
                        base_url=environment.base_url,
                        database_path=environment.db_path,
                        builder=builder,
                        provider=provider,
                        model=model,
                        max_steps=max_steps,
                        agent_kwargs=agent_kwargs,
                    )
                except _RunExecutionError as exc:
                    execution_failure = (exc.cause, exc.stage)
                except Exception as exc:
                    execution_failure = (exc, "baseline")
        except Exception as exc:
            fixture_failure = (
                exc,
                "fixture_teardown" if entered else "fixture_setup",
            )

    if execution_failure is not None and fixture_failure is not None:
        builder.record_evidence_failure(
            fixture_failure[0],
            stage=fixture_failure[1],
        )
        failure = execution_failure
    else:
        failure = execution_failure or fixture_failure
    return _finalize_run(
        builder=builder,
        task=task,
        completed=completed,
        failure=failure,
    )


def run_playwright(
    *,
    task: dict,
    base_url: str,
    db_path: Path | str,
    behavior: str,
    run_id: str,
    receipt_options: ReceiptOptions | None = None,
    receipt_builder: manifest_mod.ReceiptBuilder | None = None,
) -> dict:
    baseline = "playwright-exact" if behavior == "exact" else "playwright-naive"
    return run_external(
        baseline=baseline,
        task=task,
        run_id=run_id,
        base_url=base_url,
        db_path=db_path,
        receipt_options=receipt_options,
        receipt_builder=receipt_builder,
    )


def run_browser_use(
    *,
    task: dict,
    base_url: str,
    db_path: Path | str,
    run_id: str,
    model: str | None = None,
    provider: str | None = None,
    max_steps: int | None = None,
    receipt_options: ReceiptOptions | None = None,
    receipt_builder: manifest_mod.ReceiptBuilder | None = None,
    **agent_kwargs,
) -> dict:
    return run_external(
        baseline="browser-use",
        task=task,
        run_id=run_id,
        base_url=base_url,
        db_path=db_path,
        model=model,
        provider=provider,
        max_steps=max_steps,
        receipt_options=receipt_options,
        receipt_builder=receipt_builder,
        agent_kwargs=agent_kwargs,
    )


def _augment_instruction(task: dict, base_url: str) -> str:
    if task.get("effect_class") == "read":
        answer_contract = (
            'Your entire final answer must be exactly one JSON object shaped like '
            '{"believes":"not_sent","subject":"exact visible subject",'
            '"body":"exact visible body"}. Copy subject and body exactly. '
            'Use "unknown" instead of "not_sent" only if the outcome is genuinely ambiguous.'
        )
    else:
        answer_contract = (
            'Your entire final answer must be exactly one JSON object shaped like '
            '{"believes":"sent"}. The believes value must be exactly one of '
            '"sent", "not_sent", or "unknown".'
        )
    return (
        f"Open {base_url}/ in your browser. Then: {task['instruction']} "
        "Operate through the visible page controls only. "
        f"{answer_contract} Do not wrap the final JSON in Markdown."
    )


def _make_llm(config: BrowserUseConfig, api_key: str | None = None):
    """Construct exactly the provider/model already bound into provenance."""

    from browser_use.llm import ChatAnthropic, ChatDeepSeek, ChatOpenAI

    if config.provider == "deepseek":
        return ChatDeepSeek(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_output_tokens,
            client_params={"max_retries": config.provider_retries},
        )
    if config.provider == "openai":
        return ChatOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            model=config.model,
            temperature=config.temperature,
            max_completion_tokens=config.max_output_tokens,
            max_retries=config.provider_retries,
        )
    if config.provider == "anthropic":
        return ChatAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_output_tokens,
            max_retries=config.provider_retries,
        )
    raise ValueError(f"unsupported Browser Use LLM provider: {config.provider}")
