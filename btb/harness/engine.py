"""Harness engine: orchestrate one isolated, receipted benchmark run."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from btb.baselines import play as play_baseline
from btb.harness import browser_use_policy, browser_use_sandbox, runtime
from btb.harness import inject as inject_mod
from btb.harness import manifest as manifest_mod
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
_BROWSER_USE_VERSION = browser_use_policy.BROWSER_USE_VERSION
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
_BROWSER_USE_FULL_EXCLUDED_ACTIONS = (
    "evaluate",
    "read_file",
    "replace_file",
    "save_as_pdf",
    "upload_file",
    "write_file",
)
_BROWSER_USE_FULL_ALLOWED_ACTIONS = (
    "click",
    "close",
    "done",
    "dropdown_options",
    "extract",
    "find_elements",
    "find_text",
    "go_back",
    "input",
    "navigate",
    "screenshot",
    "scroll",
    "search",
    "search_page",
    "select_dropdown",
    "send_keys",
    "switch",
    "wait",
)
_BROWSER_USE_FULL_PROVIDER_MODELS = frozenset(
    {
        ("anthropic", "claude-sonnet-4-0"),
        ("openai", "gpt-4.1-mini"),
    }
)

_FIXTURE_ACTION_CALLBACKS = browser_use_policy.FIXTURE_ACTION_CALLBACKS


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
    name: str = "browser-use"
    temperature: float = 0.0
    max_output_tokens: int = 4096
    provider_retries: int = 0
    excluded_actions: tuple[str, ...] = _BROWSER_USE_EXCLUDED_ACTIONS
    allowed_actions: tuple[str, ...] = _BROWSER_USE_ALLOWED_ACTIONS
    use_vision: bool = False
    use_judge: bool = False
    max_actions_per_step: int | None = None


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
    name: str = "browser-use",
) -> BrowserUseConfig:
    if name not in {"browser-use", "browser-use-full"}:
        raise ValueError(f"unsupported Browser Use baseline: {name}")
    if name == "browser-use-full" and (provider is None or model is None):
        raise ValueError(
            "browser-use-full requires an explicit --provider and --model from "
            "its statically allowlisted pairs"
        )
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
    if (
        name == "browser-use-full"
        and (resolved_provider, resolved_model) not in _BROWSER_USE_FULL_PROVIDER_MODELS
    ):
        allowed = ", ".join(
            f"{allowed_provider}/{allowed_model}"
            for allowed_provider, allowed_model in sorted(
                _BROWSER_USE_FULL_PROVIDER_MODELS
            )
        )
        raise ValueError(
            "browser-use-full provider/model is not statically allowlisted: "
            f"{resolved_provider}/{resolved_model}; allowed: {allowed}"
        )
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
    if name == "browser-use-full":
        policy = {
            "excluded_actions": _BROWSER_USE_FULL_EXCLUDED_ACTIONS,
            "allowed_actions": _BROWSER_USE_FULL_ALLOWED_ACTIONS,
            "use_vision": True,
            "use_judge": False,
            "max_actions_per_step": 8,
        }
    else:
        policy = {
            "excluded_actions": _BROWSER_USE_EXCLUDED_ACTIONS,
            "allowed_actions": _BROWSER_USE_ALLOWED_ACTIONS,
            "use_vision": False,
            "use_judge": False,
            "max_actions_per_step": None,
        }
    return BrowserUseConfig(
        name=name,
        provider=resolved_provider,
        model=resolved_model,
        max_steps=resolved_steps,
        wall_s=float(wall_s),
        **policy,
    )


def _browser_use_provenance(
    config: BrowserUseConfig,
    *,
    effective_policy: dict[str, object] | None = None,
) -> manifest_mod.BaselineProvenance:
    parameters: dict[str, object] = {
        "max_steps": config.max_steps,
        "wall_s": config.wall_s,
        "wall_budget_enforced": True,
        "headless": True,
        "browser_profile": {
            "accept_downloads": False,
            "auto_download_pdfs": False,
            "allowed_origins": "run_fixture_only",
            "captcha_solver": False,
            "cross_origin_iframes": False,
            "default_extensions": False,
            "deterministic_rendering": True,
            "downloads_path": "sandbox_root_only",
            "har": False,
            "storage_state": False,
            "traces": False,
            "user_data_dir": "sandbox_root_only",
            "video": False,
        },
        "use_judge": config.use_judge,
        "use_vision": config.use_vision,
        "directly_open_url": True,
        "excluded_actions": list(config.excluded_actions),
        "llm_generation": {
            "temperature": config.temperature,
            "max_output_tokens": config.max_output_tokens,
            "provider_retries": config.provider_retries,
            "top_p": None,
            "seed": None,
        },
        "framework_filesystem": {
            "child_process": True,
            "inventory": "bounded_lstat_sha256",
            "parent_process_group_timeout": True,
            "root": "unique_system_temp_outside_repository_and_home",
            "terminal_cleanup_receipt": True,
        },
        "effective_policy": (
            effective_policy
            if effective_policy is not None
            else {"status": "unobserved"}
        ),
    }
    if config.max_actions_per_step is not None:
        parameters["max_actions_per_step"] = config.max_actions_per_step
    if config.use_vision:
        parameters["framework_compatibility"] = {
            "constructor_use_vision": "auto",
            "effective_use_vision_bound_after_construction": True,
            "post_agent_semantic_audit": True,
        }
    return manifest_mod.BaselineProvenance(
        name=config.name,
        framework_name="browser-use",
        framework_version=_installed_version("browser-use"),
        provider=config.provider,
        model=config.model,
        parameters=parameters,
        modality_policy={"dom": True, "vision": config.use_vision},
        capability_policy={
            "visible_page_controls_only": True,
            "javascript_console": False,
            "direct_api": False,
            "filesystem": {
                "agent_arbitrary_paths": False,
                "framework_owned": True,
                "mode": "isolated_ephemeral_per_run",
                "os_mandatory_access_control": False,
            },
            "database": False,
            "enforcement": {
                "agent_tools_allowed": list(config.allowed_actions),
                "agent_tools_excluded": list(config.excluded_actions),
                "fixture_write_api_requires_ui_token": True,
                "navigation": {
                    "action": _FIXTURE_ACTION_CALLBACKS["navigate"]["identity"],
                    "pre_dispatch": "resolve_http_https_and_require_exact_fixture_origin",
                    "redirects": {
                        "browser_profile": "allowed_domains_configured",
                        "watchdog": "http_https_defense_in_depth_after_dispatch",
                        "post_dispatch": "detect_off_fixture_recover_and_stop",
                    },
                    "fixture_click_controls": (
                        "controlled_fixture_has_no_link_or_"
                        "navigation_producing_click_controls"
                    ),
                },
                "external_search": {
                    "action": (
                        _FIXTURE_ACTION_CALLBACKS["search"]["identity"]
                        if config.name == "browser-use-full"
                        else "excluded"
                    ),
                    "behavior": (
                        _FIXTURE_ACTION_CALLBACKS["search"]["behavior"]
                        if config.name == "browser-use-full"
                        else "excluded"
                    ),
                },
                "telemetry": False,
                "screenshot_action": (
                    "benchmark_no_file_next_observation"
                    if config.name == "browser-use-full"
                    else "excluded"
                ),
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
    source: manifest_mod.SourceProvenance | None = None,
    release: str = manifest_mod.RELEASE_VERSION,
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
        source=source,
        release=release,
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
    source: manifest_mod.SourceProvenance | None = None,
    release: str = manifest_mod.RELEASE_VERSION,
) -> manifest_mod.ReceiptBuilder:
    """Resolve one CLI baseline into the provenance used by the engine."""

    if baseline in {"browser-use", "browser-use-full"}:
        config = resolve_browser_use_config(
            task,
            provider=provider,
            model=model,
            max_steps=max_steps,
            name=baseline,
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
        source=source,
        release=release,
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


@dataclass(frozen=True)
class _FixtureOrigin:
    """Canonical HTTP(S) origin used by the fixture-only navigate action."""

    scheme: str
    host: str
    port: int
    is_ipv6: bool

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if self.is_ipv6 else self.host
        default_port = 80 if self.scheme == "http" else 443
        return host if self.port == default_port else f"{host}:{self.port}"


def _strict_urlsplit(value: object, *, label: str):
    """Parse one URL without accepting browser/parser-normalized ambiguity."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty URL")
    if "\\" in value or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError(f"{label} contains unsafe whitespace or backslashes")
    try:
        return urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"{label} is malformed") from exc


def _canonical_host(host: str, *, label: str) -> tuple[str, bool]:
    """Normalize IPv6 and IDNA host forms before exact-origin comparison."""

    if not host or "%" in host:
        raise ValueError(f"{label} has an invalid host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            canonical = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError(f"{label} has an invalid IDNA host") from exc
        if (
            not canonical
            or len(canonical) > 253
            or any(not part for part in canonical.split("."))
        ):
            raise ValueError(f"{label} has an invalid host")
        return canonical, False
    return address.compressed.lower(), address.version == 6


def _fixture_origin_from_parts(parts, *, label: str) -> _FixtureOrigin:
    """Return an exact normalized HTTP(S) origin or reject the URL."""

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.netloc or not parts.hostname:
        raise ValueError(f"{label} must identify an absolute HTTP(S) origin")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{label} must not include URL credentials")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    host, is_ipv6 = _canonical_host(parts.hostname, label=label)
    default_port = 80 if scheme == "http" else 443
    return _FixtureOrigin(
        scheme=scheme,
        host=host,
        port=explicit_port if explicit_port is not None else default_port,
        is_ipv6=is_ipv6,
    )


@dataclass(frozen=True)
class _FixtureNavigationPolicy:
    """One per-run, exact-origin navigation policy bound into the callback."""

    target: str
    origin: _FixtureOrigin

    @classmethod
    def from_target(cls, target: str) -> _FixtureNavigationPolicy:
        parts = _strict_urlsplit(target, label="fixture target")
        origin = _fixture_origin_from_parts(parts, label="fixture target")
        canonical_target = urlunsplit(
            (
                origin.scheme,
                origin.authority,
                parts.path or "/",
                parts.query,
                "",
            )
        )
        return cls(target=canonical_target, origin=origin)

    def resolve(self, requested: object) -> str:
        """Resolve one allowed request against the run fixture target."""

        parts = _strict_urlsplit(requested, label="navigation URL")
        if parts.scheme:
            if not parts.netloc:
                raise ValueError("navigation URL with a scheme must be absolute")
            resolved = parts
        elif parts.netloc or (isinstance(requested, str) and requested.startswith("//")):
            raise ValueError("scheme-relative navigation URLs are not allowed")
        else:
            resolved = _strict_urlsplit(
                urljoin(self.target, requested),
                label="resolved navigation URL",
            )
        if _fixture_origin_from_parts(resolved, label="navigation URL") != self.origin:
            raise ValueError("navigation URL is outside the exact fixture origin")
        return urlunsplit(
            (
                self.origin.scheme,
                self.origin.authority,
                resolved.path,
                resolved.query,
                resolved.fragment,
            )
        )

    def accepts_observed_url(self, observed: object) -> bool:
        """Check an already-loaded URL without treating it as a relative request."""

        try:
            parts = _strict_urlsplit(observed, label="observed navigation URL")
            return (
                bool(parts.scheme and parts.netloc)
                and _fixture_origin_from_parts(
                    parts,
                    label="observed navigation URL",
                )
                == self.origin
            )
        except ValueError:
            return False


def _fixture_origin_pattern(base_url: str) -> str:
    """Return the exact HTTP(S) origin pattern accepted by Browser Use."""

    origin = _FixtureNavigationPolicy.from_target(base_url).origin
    return f"{origin.scheme}://{origin.authority}/"


def _reject_agent_overrides(agent_kwargs: dict) -> None:
    if agent_kwargs:
        raise ValueError(
            "agent_kwargs are not accepted because unreceipted overrides would "
            "change the learned-baseline condition: "
            + ", ".join(sorted(agent_kwargs))
        )


def _require_browser_use_version() -> None:
    """Fail closed unless the executable framework is the frozen release."""

    installed = _installed_version("browser-use")
    if installed != _BROWSER_USE_VERSION:
        raise RuntimeError(
            "Browser Use learned baselines require exactly "
            f"browser-use=={_BROWSER_USE_VERSION}; installed={installed!r}"
        )


def _browser_use_action_names(tools: object) -> tuple[str, ...]:
    """Read the effective action names from Browser Use's auditable registry."""

    actions = getattr(
        getattr(getattr(tools, "registry", None), "registry", None),
        "actions",
        None,
    )
    if not isinstance(actions, dict) or not all(
        isinstance(name, str) for name in actions
    ):
        raise RuntimeError("Browser Use tools do not expose an auditable action registry")
    return tuple(sorted(actions))


def _validate_browser_use_action_registry(
    tools: object,
    config: BrowserUseConfig,
) -> None:
    """Fail closed unless the effective registry equals the frozen policy."""

    effective = _browser_use_action_names(tools)
    expected = tuple(sorted(config.allowed_actions))
    if effective != expected:
        added = sorted(set(effective) - set(expected))
        missing = sorted(set(expected) - set(effective))
        raise RuntimeError(
            "Browser Use action registry differs from the frozen capability policy: "
            f"unexpected={added!r}, missing={missing!r}"
        )


async def _benchmark_no_file_screenshot():
    """Request the next vision image without accepting or writing a path."""

    from browser_use import ActionResult

    return ActionResult(
        extracted_content="Screenshot requested for the next observation",
        metadata={"include_screenshot": True},
    )


_benchmark_no_file_screenshot.__name__ = "screenshot"


def _install_benchmark_screenshot(tools: object) -> None:
    """Replace Browser Use's file-capable screenshot with a no-file action.

    The action uses Browser Use's public custom-action and ``ActionResult``
    contracts.  It requests a screenshot in the next vision observation and
    exposes no agent-controlled path or filename.
    """

    register = getattr(tools, "action", None)
    if not callable(register):
        raise RuntimeError("Browser Use tools do not expose public custom actions")
    register(
        "Request a screenshot in the next browser observation; no file is saved."
    )(_benchmark_no_file_screenshot)


async def _dispatch_browser_use_event(browser_session: object, event: object) -> None:
    """Dispatch one already-validated Browser Use event and await its result."""

    event_bus = getattr(browser_session, "event_bus", None)
    dispatch = getattr(event_bus, "dispatch", None)
    if not callable(dispatch):
        raise TypeError("Browser Use session does not expose an event dispatcher")
    dispatched = dispatch(event)
    event_result = getattr(dispatched, "event_result", None)
    if not callable(event_result):
        raise TypeError("Browser Use event is not auditable")
    await dispatched
    await event_result(raise_if_any=True, raise_if_none=False)


async def _dispatch_browser_use_navigation(
    browser_session: object,
    *,
    url: str,
    new_tab: bool,
) -> None:
    """Dispatch one already-validated Browser Use navigation event."""

    from browser_use.browser.events import NavigateToUrlEvent

    await _dispatch_browser_use_event(
        browser_session,
        NavigateToUrlEvent(url=url, new_tab=new_tab),
    )


async def _observed_browser_url(browser_session: object) -> str:
    """Read the focused URL after a Browser Use navigation completes."""

    current_url = getattr(browser_session, "get_current_page_url", None)
    if not callable(current_url):
        raise TypeError("Browser Use session does not expose its focused URL")
    value = await current_url()
    if not isinstance(value, str) or not value:
        raise RuntimeError("Browser Use session returned no focused URL")
    return value


async def _recover_fixture_navigation(
    browser_session: object,
    policy: _FixtureNavigationPolicy,
) -> bool:
    """Return focus to the known-safe fixture target after redirect detection."""

    try:
        recovery_url = policy.resolve(policy.target)
        await _dispatch_browser_use_navigation(
            browser_session,
            url=recovery_url,
            new_tab=False,
        )
        return policy.accepts_observed_url(
            await _observed_browser_url(browser_session)
        )
    except Exception:  # noqa: BLE001 - recovery must not mask the redirect escape.
        return False


async def _fixture_transition_failure(
    browser_session: object,
    policy: _FixtureNavigationPolicy,
    *,
    action: str,
    reason: str,
) -> object:
    """Recover to the fixture target and make an unverified transition terminal."""

    from browser_use import ActionResult

    recovered = await _recover_fixture_navigation(browser_session, policy)
    suffix = "fixture recovery succeeded" if recovered else "fixture recovery failed"
    return ActionResult(
        is_done=True,
        success=False,
        error=f"{action} {reason}; {suffix}",
    )


async def _complete_fixture_transition(
    browser_session: object,
    policy: _FixtureNavigationPolicy,
    *,
    action: str,
    dispatch,
    memory: str,
) -> object:
    """Observe a history/tab transition and stop after an unsafe outcome."""

    from browser_use import ActionResult

    try:
        await dispatch()
    except Exception as exc:  # noqa: BLE001 - normalize third-party event errors.
        return await _fixture_transition_failure(
            browser_session,
            policy,
            action=action,
            reason=f"did not complete safely: {exc}",
        )
    try:
        observed = await _observed_browser_url(browser_session)
    except Exception as exc:  # noqa: BLE001 - refuse an unobservable outcome.
        return await _fixture_transition_failure(
            browser_session,
            policy,
            action=action,
            reason=f"result could not be observed after dispatch: {exc}",
        )
    if not policy.accepts_observed_url(observed):
        return await _fixture_transition_failure(
            browser_session,
            policy,
            action=action,
            reason="ended outside the fixture origin after dispatch",
        )
    return ActionResult(extracted_content=memory, long_term_memory=memory)


async def _browser_use_tab_target_id(browser_session: object, tab_id: object) -> object:
    """Resolve the framework's four-character tab identifier before dispatch."""

    get_target_id = getattr(browser_session, "get_target_id_from_tab_id", None)
    if not callable(get_target_id):
        raise TypeError("Browser Use session does not expose tab identifiers")
    target_id = await get_target_id(tab_id)
    if not target_id:
        raise RuntimeError("Browser Use tab identifier did not resolve")
    return target_id


class _FixtureNavigationAction:
    """Benchmark-owned fixture-bound navigation, history, and tab callbacks."""

    def __init__(self, policy: _FixtureNavigationPolicy) -> None:
        self.policy = policy

    async def navigate(self, params, browser_session):
        """Navigate only within one fixture origin and expose redirect escape."""

        try:
            destination = self.policy.resolve(getattr(params, "url", None))
        except ValueError as exc:
            from browser_use import ActionResult

            return ActionResult(error=f"Navigation rejected: {exc}")

        return await _complete_fixture_transition(
            browser_session,
            self.policy,
            action="Navigation",
            dispatch=lambda: _dispatch_browser_use_navigation(
                browser_session,
                url=destination,
                new_tab=bool(getattr(params, "new_tab", False)),
            ),
            memory=(
                "Opened a new fixture tab"
                if bool(getattr(params, "new_tab", False))
                else "Navigated within the fixture"
            ),
        )

    async def go_back(self, _params, browser_session):
        """Go back only when the observed result remains at the fixture origin."""

        from browser_use.browser.events import GoBackEvent

        return await _complete_fixture_transition(
            browser_session,
            self.policy,
            action="Go back",
            dispatch=lambda: _dispatch_browser_use_event(browser_session, GoBackEvent()),
            memory="Navigated back within the fixture",
        )

    async def switch(self, params, browser_session):
        """Switch tabs only when the focused result remains at the fixture origin."""

        from browser_use.browser.events import SwitchTabEvent

        async def dispatch() -> None:
            target_id = await _browser_use_tab_target_id(
                browser_session,
                getattr(params, "tab_id", None),
            )
            await _dispatch_browser_use_event(
                browser_session,
                SwitchTabEvent(target_id=target_id),
            )

        return await _complete_fixture_transition(
            browser_session,
            self.policy,
            action="Tab switch",
            dispatch=dispatch,
            memory="Switched to a fixture tab",
        )

    async def close(self, params, browser_session):
        """Close a tab only when the resulting focus remains at the fixture origin."""

        from browser_use.browser.events import CloseTabEvent

        async def dispatch() -> None:
            target_id = await _browser_use_tab_target_id(
                browser_session,
                getattr(params, "tab_id", None),
            )
            await _dispatch_browser_use_event(
                browser_session,
                CloseTabEvent(target_id=target_id),
            )

        return await _complete_fixture_transition(
            browser_session,
            self.policy,
            action="Tab close",
            dispatch=dispatch,
            memory="Closed a tab and remained in the fixture",
        )


_FIXTURE_EXTERNAL_SEARCH_ERROR = (
    "External web search is unavailable in the fixture-only benchmark."
)


class _FixtureExternalSearchAction:
    """Reject Browser Use web search before it can dispatch an external URL."""

    async def search(self, _params, browser_session):
        from browser_use import ActionResult

        del browser_session
        return ActionResult(error=_FIXTURE_EXTERNAL_SEARCH_ERROR)


def _install_fixture_navigation(
    tools: object,
    *,
    target: str,
) -> _FixtureNavigationAction:
    """Replace Browser Use's navigate callback through its public action API."""

    from browser_use.tools.views import NavigateAction

    register = getattr(tools, "action", None)
    if not callable(register):
        raise TypeError("Browser Use tools do not expose public custom actions")
    callback = _FixtureNavigationAction(
        _FixtureNavigationPolicy.from_target(target)
    ).navigate
    register(
        "Navigate only to an HTTP(S) URL at this benchmark fixture origin. "
        "Relative URLs are resolved against the fixture target.",
        param_model=NavigateAction,
        terminates_sequence=True,
    )(callback)
    return callback.__self__


def _install_fixture_tab_actions(
    tools: object,
    owner: _FixtureNavigationAction,
    config: BrowserUseConfig,
) -> None:
    """Replace history/tab callbacks that can otherwise focus about:blank."""

    from browser_use.tools.views import CloseTabAction, NoParamsAction, SwitchTabAction

    register = getattr(tools, "action", None)
    if not callable(register):
        raise TypeError("Browser Use tools do not expose public custom actions")
    register(
        "Go back, then continue only if the focused page remains at the fixture origin.",
        param_model=NoParamsAction,
        terminates_sequence=True,
    )(owner.go_back)
    if config.name != "browser-use-full":
        return
    register(
        "Switch to a tab, then continue only if its focused page remains at the fixture origin.",
        param_model=SwitchTabAction,
        terminates_sequence=True,
    )(owner.switch)
    register(
        "Close a tab, then continue only if the focused page remains at the fixture origin.",
        param_model=CloseTabAction,
    )(owner.close)


def _install_fixture_search_action(tools: object) -> _FixtureExternalSearchAction:
    """Replace external Browser Use search through its public action API."""

    from browser_use.tools.views import SearchAction

    register = getattr(tools, "action", None)
    if not callable(register):
        raise TypeError("Browser Use tools do not expose public custom actions")
    callback = _FixtureExternalSearchAction().search
    register(
        "External web search is unavailable in this fixture-only benchmark.",
        param_model=SearchAction,
        terminates_sequence=True,
    )(callback)
    return callback.__self__


def _make_browser_tools(
    tools_class,
    config: BrowserUseConfig,
    *,
    target: str,
):
    """Construct tools, replace unsafe callbacks, and audit action names."""

    tools = tools_class(
        exclude_actions=list(config.excluded_actions),
        display_files_in_done_text=False,
    )
    fixture_actions = _install_fixture_navigation(tools, target=target)
    _install_fixture_tab_actions(tools, fixture_actions, config)
    if config.name == "browser-use-full":
        _install_fixture_search_action(tools)
        _install_benchmark_screenshot(tools)
    _validate_browser_use_action_registry(tools, config)
    return tools


def _browser_use_agent_options(config: BrowserUseConfig) -> dict[str, object]:
    """Return the complete non-file Agent contract for the frozen condition."""

    options: dict[str, object] = {
        "available_file_paths": [],
        "directly_open_url": True,
        "display_files_in_done_text": False,
        "fallback_llm": None,
        "generate_gif": False,
        "message_compaction": False,
        "save_conversation_path": None,
        "use_judge": config.use_judge,
        "use_vision": config.use_vision,
    }
    if config.max_actions_per_step is not None:
        options["max_actions_per_step"] = config.max_actions_per_step
    return options


def _require_browser_use_model_capabilities(config: BrowserUseConfig) -> None:
    """Reject full-condition models outside the frozen static allowlist."""

    if not config.use_vision:
        return
    provider_model = (config.provider, config.model)
    if provider_model not in _BROWSER_USE_FULL_PROVIDER_MODELS:
        supported = ", ".join(
            f"{provider}/{model}"
            for provider, model in sorted(_BROWSER_USE_FULL_PROVIDER_MODELS)
        )
        raise RuntimeError(
            f"{config.name} requires use_vision=true and a statically allowlisted "
            f"provider/model; got {config.provider}/{config.model}; supported: "
            f"{supported}"
        )


def _browser_use_agent_constructor_options(
    config: BrowserUseConfig,
) -> dict[str, object]:
    """Return Browser Use 0.13.x-compatible constructor options.

    Browser Use 0.13.6 removes the ``screenshot`` action whenever the constructor
    receives an explicit vision boolean.  ``auto`` preserves the frozen registry;
    the effective boolean is bound and audited immediately after construction,
    before browser launch or provider invocation.
    """

    options = dict(_browser_use_agent_options(config))
    if config.use_vision:
        options["use_vision"] = "auto"
    return options


def _bind_browser_use_agent_vision(agent: object, config: BrowserUseConfig) -> None:
    """Bind the public ``auto`` compatibility setting to effective vision=true."""

    settings = getattr(agent, "settings", None)
    if settings is None:
        raise RuntimeError("Browser Use Agent does not expose auditable settings")

    if config.use_vision:
        constructor_vision = getattr(settings, "use_vision", None)
        if constructor_vision != "auto":
            raise RuntimeError(
                "Browser Use Agent changed the vision compatibility setting during "
                f"construction: expected 'auto', got {constructor_vision!r}"
            )
        settings.use_vision = True


def _normalized_schema(model: object, *, label: str) -> dict[str, object]:
    schema_method = getattr(model, "model_json_schema", None)
    if not callable(schema_method):
        raise RuntimeError(f"{label} does not expose model_json_schema()")
    schema = schema_method()
    if not isinstance(schema, dict):
        raise RuntimeError(f"{label} produced a non-object JSON schema")
    return json.loads(manifest_mod.canonical_json_bytes(schema))


def _schema_digest(schema: dict[str, object]) -> str:
    """Return the canonical semantic digest recorded for one JSON schema."""

    return manifest_mod.canonical_json_sha256(schema)


def _frozen_schema_digests(config: BrowserUseConfig) -> dict[str, object]:
    """Read the exact per-provider/model schema identity for this condition."""

    try:
        frozen = browser_use_policy.schema_digests_for(
            config.name,
            config.provider,
            config.model,
        )
    except ValueError as exc:
        raise RuntimeError("Browser Use schema condition is not frozen") from exc
    action_digests = frozen.get("actions")
    action_model_digest = frozen.get("action_model")
    if not isinstance(action_digests, dict) or not isinstance(
        action_model_digest,
        str,
    ):
        raise RuntimeError("Browser Use frozen schema digests are malformed")
    return {
        "condition": browser_use_policy.schema_condition(
            config.name,
            config.provider,
            config.model,
        ),
        "actions": action_digests,
        "action_model": action_model_digest,
    }


def _action_model_names(schema: dict[str, object]) -> tuple[str, ...]:
    definitions = schema.get("$defs")
    variants = schema.get("anyOf")
    if not isinstance(definitions, dict) or not isinstance(variants, list):
        raise RuntimeError("Browser Use ActionModel does not expose auditable variants")
    names: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict) or not isinstance(variant.get("$ref"), str):
            raise RuntimeError("Browser Use ActionModel contains a non-reference variant")
        definition_name = variant["$ref"].removeprefix("#/$defs/")
        definition = definitions.get(definition_name)
        properties = definition.get("properties") if isinstance(definition, dict) else None
        if not isinstance(properties, dict) or len(properties) != 1:
            raise RuntimeError("Browser Use ActionModel variant is not one exact action")
        names.extend(properties)
    return tuple(sorted(names))


def _unwrap_registered_callback(function: object) -> object:
    """Follow public decorator wrappers without source inspection or hashing."""

    current = function
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        wrapped = getattr(current, "__wrapped__", None)
        if not callable(wrapped):
            return current
        current = wrapped
    raise RuntimeError("Browser Use action callback wrapper cycle")


def _same_bound_method(
    value: object,
    *,
    function: object,
    receiver: object,
) -> bool:
    """Compare a bound method without relying on ephemeral method objects."""

    return (
        getattr(value, "__func__", None) is function
        and getattr(value, "__self__", None) is receiver
    )


def _fixture_action_models() -> dict[str, object]:
    """Return the exact Browser Use 0.13.6 parameter model for each wrapper."""

    from browser_use.tools.views import (
        CloseTabAction,
        NavigateAction,
        NoParamsAction,
        SearchAction,
        SwitchTabAction,
    )

    return {
        "navigate": NavigateAction,
        "go_back": NoParamsAction,
        "switch": SwitchTabAction,
        "close": CloseTabAction,
        "search": SearchAction,
    }


def _audit_fixture_action(
    action: object,
    *,
    name: str,
    target: str,
    schema: dict[str, object],
) -> dict[str, str]:
    """Verify the registry wrapper and bound fixture callback semantically."""

    expected_callback = _FIXTURE_ACTION_CALLBACKS.get(name)
    expected_model = _fixture_action_models().get(name)
    expected_method, expected_owner_type = {
        "navigate": (_FixtureNavigationAction.navigate, _FixtureNavigationAction),
        "go_back": (_FixtureNavigationAction.go_back, _FixtureNavigationAction),
        "switch": (_FixtureNavigationAction.switch, _FixtureNavigationAction),
        "close": (_FixtureNavigationAction.close, _FixtureNavigationAction),
        "search": (_FixtureExternalSearchAction.search, _FixtureExternalSearchAction),
    }.get(name, (None, None))
    if (
        not isinstance(expected_callback, dict)
        or expected_model is None
        or expected_method is None
    ):
        raise RuntimeError(f"Browser Use {name!r} has no frozen fixture callback")
    param_model = getattr(action, "param_model", None)
    expected_schema = _normalized_schema(
        expected_model,
        label=f"Browser Use {name} parameter model",
    )
    if param_model is not expected_model or schema != expected_schema:
        raise RuntimeError(
            f"Browser Use {name} no longer exposes its exact parameter schema"
        )

    wrapper = getattr(action, "function", None)
    callback = _unwrap_registered_callback(wrapper)
    owner = getattr(callback, "__self__", None)
    if (
        type(owner) is not expected_owner_type
        or not _same_bound_method(
            callback,
            function=expected_method,
            receiver=owner,
        )
        or not _same_bound_method(
            getattr(wrapper, "__wrapped__", None),
            function=expected_method,
            receiver=owner,
        )
        or (
            type(owner) is _FixtureNavigationAction
            and owner.policy != _FixtureNavigationPolicy.from_target(target)
        )
    ):
        raise RuntimeError(
            f"Browser Use {name} is not the benchmark fixture-only callback"
        )
    return {
        "callback_identity": expected_callback["identity"],
        "callback_behavior": expected_callback["behavior"],
    }


def _audit_fixture_navigation_action(
    action: object,
    *,
    target: str,
    schema: dict[str, object],
) -> dict[str, str]:
    """Compatibility wrapper for the navigate-specific semantic audit."""

    return _audit_fixture_action(
        action,
        name="navigate",
        target=target,
        schema=schema,
    )


def _observe_browser_use_agent_policy(
    agent: object,
    config: BrowserUseConfig,
    *,
    sandbox_root: Path,
    target: str,
) -> dict[str, object]:
    """Observe post-construction semantic policy without trusting names alone."""

    settings = getattr(agent, "settings", None)
    tools = getattr(agent, "tools", None)
    action_model = getattr(agent, "ActionModel", None)
    if settings is None or tools is None or action_model is None:
        raise RuntimeError(
            "Browser Use Agent does not expose settings, tools, and ActionModel"
        )

    expected_settings = {
        "generate_gif": False,
        "save_conversation_path": None,
        "use_judge": config.use_judge,
        "use_vision": config.use_vision,
    }
    if config.max_actions_per_step is not None:
        expected_settings["max_actions_per_step"] = config.max_actions_per_step
    drift = {
        name: {"expected": expected, "actual": getattr(settings, name, None)}
        for name, expected in expected_settings.items()
        if getattr(settings, name, None) != expected
    }
    if drift:
        raise RuntimeError(
            "Browser Use Agent settings differ from the frozen condition: "
            f"{drift!r}"
        )
    message_compaction = getattr(settings, "message_compaction", None)
    file_system = getattr(agent, "file_system", None)
    screenshot_service = getattr(agent, "screenshot_service", None)
    path_values = {
        "agent_directory": getattr(agent, "agent_directory", None),
        "file_system_base": getattr(file_system, "base_dir", None),
        "file_system_data": getattr(file_system, "data_dir", None),
        "file_system_path": getattr(agent, "file_system_path", None),
        "screenshot_storage": getattr(screenshot_service, "screenshots_dir", None),
    }
    try:
        framework_paths_confined = all(
            Path(value).resolve().is_relative_to(sandbox_root.resolve())
            for value in path_values.values()
        )
    except (TypeError, ValueError):
        framework_paths_confined = False
    extras = {
        "available_file_paths": getattr(agent, "available_file_paths", None),
        "directly_open_url": getattr(agent, "directly_open_url", None),
        "display_files_in_done_text": getattr(tools, "display_files_in_done_text", None),
        "fallback_llm": getattr(agent, "_fallback_llm", None),
        "framework_paths_confined": framework_paths_confined,
        "judge_llm": getattr(agent, "judge_llm", None),
        "message_compaction_enabled": getattr(message_compaction, "enabled", None),
        "page_extraction_llm": getattr(settings, "page_extraction_llm", None),
    }
    if (
        extras["available_file_paths"] != []
        or extras["directly_open_url"] is not True
        or extras["display_files_in_done_text"] is not False
        or extras["fallback_llm"] is not None
        or extras["framework_paths_confined"] is not True
        or extras["message_compaction_enabled"] is not False
        or _llm_role(extras["page_extraction_llm"])
        != {"provider": config.provider, "model": config.model}
        or _llm_role(getattr(agent, "llm", None))
        != {"provider": config.provider, "model": config.model}
        or _llm_role(extras["judge_llm"])
        != {"provider": config.provider, "model": config.model}
    ):
        raise RuntimeError("Browser Use Agent roles or non-file settings drifted")
    _validate_browser_use_action_registry(tools, config)

    registered = tools.registry.registry.actions
    frozen_schemas = _frozen_schema_digests(config)
    expected_action_digests = frozen_schemas["actions"]
    expected_names = tuple(sorted(config.allowed_actions))
    if tuple(sorted(expected_action_digests)) != expected_names:
        raise RuntimeError(
            "Browser Use frozen action schema digest set differs from the "
            "frozen capability policy"
        )
    actions: list[dict[str, object]] = []
    for name in sorted(registered):
        action = registered[name]
        param_model = getattr(action, "param_model", None)
        schema = _normalized_schema(param_model, label=f"action {name!r}")
        schema_digest = _schema_digest(schema)
        if schema_digest != expected_action_digests.get(name):
            raise RuntimeError(
                f"Browser Use action {name!r} schema differs from its frozen "
                "semantic identity"
            )
        action_policy: dict[str, object] = {
            "name": name,
            "parameter_schema": schema,
            "parameter_schema_sha256": schema_digest,
        }
        if name in _FIXTURE_ACTION_CALLBACKS:
            action_policy.update(
                _audit_fixture_action(
                    action,
                    name=name,
                    target=target,
                    schema=schema,
                )
            )
        if name == "screenshot" and config.name == "browser-use-full":
            function = getattr(action, "function", None)
            if _unwrap_registered_callback(function) is not _benchmark_no_file_screenshot:
                raise RuntimeError(
                    "browser-use-full screenshot is not the benchmark no-file action"
                )
            if schema.get("properties") != {}:
                raise RuntimeError(
                    "browser-use-full screenshot schema exposes agent parameters"
                )
            action_policy["callback_identity"] = "btb.no_file_screenshot.v1"
            action_policy["callback_behavior"] = (
                "request_next_observation_without_file_write"
            )
        actions.append(action_policy)

    action_model_schema = _normalized_schema(action_model, label="Agent.ActionModel")
    action_model_digest = _schema_digest(action_model_schema)
    if action_model_digest != frozen_schemas["action_model"]:
        raise RuntimeError(
            "Browser Use Agent ActionModel differs from its frozen semantic identity"
        )
    model_names = _action_model_names(action_model_schema)
    if model_names != expected_names:
        raise RuntimeError(
            "Browser Use Agent ActionModel differs from the frozen capability policy: "
            f"actual={list(model_names)!r}, expected={list(expected_names)!r}"
        )
    return {
        "status": "observed",
        "browser_use_version": _installed_version("browser-use"),
        "settings": {
            "available_file_paths": getattr(agent, "available_file_paths", None),
            "directly_open_url": getattr(agent, "directly_open_url", None),
            "display_files_in_done_text": getattr(tools, "display_files_in_done_text", None),
            "generate_gif": getattr(settings, "generate_gif", None),
            "use_judge": getattr(settings, "use_judge"),
            "use_vision": getattr(settings, "use_vision"),
            "max_actions_per_step": getattr(settings, "max_actions_per_step"),
            "message_compaction": getattr(
                getattr(settings, "message_compaction", None), "enabled", None
            ),
            "save_conversation_path": getattr(settings, "save_conversation_path", None),
        },
        "framework_paths": {
            "agent_directory": "sandbox_root_only",
            "file_system_base": "sandbox_root_only",
            "file_system_data": "sandbox_root_only",
            "screenshot_storage": "sandbox_root_only",
        },
        "llm_roles": {
            "primary": _llm_role(getattr(agent, "llm", None)),
            "page_extraction": _llm_role(
                getattr(settings, "page_extraction_llm", None)
            ),
            "judge": {
                "active": False,
                "provider": config.provider,
                "model": config.model,
            },
            "fallback": None,
        },
        "action_model_names": list(model_names),
        "action_model_schema": action_model_schema,
        "action_model_schema_sha256": action_model_digest,
        "schema_condition": frozen_schemas["condition"],
        "actions": actions,
    }


def _llm_role(llm: object) -> dict[str, object]:
    return {
        "provider": getattr(llm, "provider", None),
        "model": getattr(llm, "model", None),
    }


def _audit_browser_use_agent(
    agent: object,
    config: BrowserUseConfig,
    *,
    sandbox_root: Path,
    target: str,
) -> dict[str, object]:
    """Audit and freeze the effective semantic policy immediately before run."""

    policy = _observe_browser_use_agent_policy(
        agent,
        config,
        sandbox_root=sandbox_root,
        target=target,
    )
    return policy


def _make_browser_use_agent(
    agent_class,
    *,
    instruction: str,
    browser: object,
    llm: object,
    tools: object,
    config: BrowserUseConfig,
    file_system_path: Path,
):
    """Construct and audit one Agent before ``Agent.run()``."""

    _require_browser_use_model_capabilities(config)
    agent = agent_class(
        task=instruction,
        browser=browser,
        llm=llm,
        page_extraction_llm=llm,
        tools=tools,
        file_system_path=str(file_system_path),
        **_browser_use_agent_constructor_options(config),
    )
    _bind_browser_use_agent_vision(agent, config)
    return agent


def _audit_browser_use_browser(
    browser: object,
    sandbox_root: Path,
    *,
    target: str,
) -> None:
    """Fail closed if Browser Use changed a root-confined profile setting."""

    profile = getattr(browser, "browser_profile", None)
    if profile is None:
        raise RuntimeError("Browser Use Browser does not expose its profile")
    for name in ("user_data_dir", "downloads_path"):
        value = getattr(profile, name, None)
        try:
            Path(value).resolve().relative_to(sandbox_root.resolve())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Browser Use {name} escaped the sandbox") from exc
    expected = {
        "accept_downloads": False,
        "allowed_domains": [_fixture_origin_pattern(target)],
        "auto_download_pdfs": False,
        "captcha_solver": False,
        "cross_origin_iframes": False,
        "deterministic_rendering": True,
        "enable_default_extensions": False,
        "record_har_path": None,
        "record_video_dir": None,
        "storage_state": None,
        "traces_dir": None,
        "use_cloud": False,
    }
    drift = {
        name: {"expected": expected_value, "actual": getattr(profile, name, None)}
        for name, expected_value in expected.items()
        if getattr(profile, name, None) != expected_value
    }
    if drift:
        raise RuntimeError(f"Browser Use Browser profile differs from frozen policy: {drift!r}")


def _worker_config(config: BrowserUseConfig) -> dict[str, object]:
    return {
        "name": config.name,
        "provider": config.provider,
        "model": config.model,
        "max_steps": config.max_steps,
        "wall_s": config.wall_s,
        "temperature": config.temperature,
        "max_output_tokens": config.max_output_tokens,
        "provider_retries": config.provider_retries,
        "excluded_actions": list(config.excluded_actions),
        "allowed_actions": list(config.allowed_actions),
        "use_vision": config.use_vision,
        "use_judge": config.use_judge,
        "max_actions_per_step": config.max_actions_per_step,
    }


def _run_browser_use_worker(
    *,
    config: BrowserUseConfig,
    instruction: str,
    target: str,
    kind: str,
    builder: manifest_mod.ReceiptBuilder | None,
) -> dict[str, object]:
    """Run and then inventory/remove one child Browser Use process."""

    lifecycle = browser_use_sandbox.SandboxLifecycle(
        on_root_created=(
            builder.register_framework_sandbox if builder is not None else None
        )
    )
    worker_result: browser_use_sandbox.WorkerResult | None = None
    inventory: dict | None = None
    inventory_error: Exception | None = None
    try:
        paths = lifecycle.create()
        worker_result = browser_use_sandbox.run_worker(
            paths,
            {
                "kind": kind,
                "config": _worker_config(config),
                "instruction": instruction,
                "target": target,
            },
            timeout_s=config.wall_s,
            provider=config.provider,
        )
    finally:
        if lifecycle.root is not None:
            try:
                if lifecycle.has_real_root():
                    inventory = lifecycle.capture_inventory()
            except Exception as exc:
                inventory_error = exc
            lifecycle.cleanup()
        if builder is not None:
            builder.bind_framework_filesystem(
                state=lifecycle.receipt_state(),
                inventory=inventory,
                cleanup_error=lifecycle.cleanup_error,
            )
            if inventory_error is not None:
                builder.record_evidence_failure(inventory_error, stage="sandbox_inventory")
    if inventory_error is not None:
        raise RuntimeError("Browser Use sandbox inventory failed") from inventory_error
    if lifecycle.cleanup_error is not None:
        raise RuntimeError("Browser Use sandbox cleanup failed") from lifecycle.cleanup_error
    if worker_result is None:
        raise RuntimeError("Browser Use worker did not start")
    if worker_result.teardown_error is not None:
        raise RuntimeError("Browser Use worker process group teardown failed")
    if worker_result.timed_out:
        raise TimeoutError("Browser Use worker exceeded the parent wall timeout")
    payload = worker_result.payload
    if worker_result.return_code != 0 or not isinstance(payload, dict):
        raise RuntimeError("Browser Use worker did not return a valid result")
    if payload.get("status") != "ok":
        error_type = payload.get("error_type")
        raise RuntimeError(f"Browser Use worker failed: {error_type}")
    return payload


def audit_browser_use_installation() -> dict[str, dict[str, object]]:
    """Child-process constructor audit for legacy and every full model pair."""

    _require_browser_use_version()
    results: dict[str, dict[str, object]] = {}
    configurations = [
        resolve_browser_use_config(
            # Construction imports the framework and validates generated action
            # models, so it needs a modest local-only allowance independent of
            # benchmark task budgets.  It never starts navigation or a provider
            # request.
            {"budget": {"steps": 1, "wall_s": 30}},
            provider="deepseek",
            model="deepseek-chat",
            max_steps=1,
            name="browser-use",
        )
    ]
    configurations.extend(
        resolve_browser_use_config(
            {"budget": {"steps": 1, "wall_s": 30}},
            provider=provider,
            model=model,
            max_steps=1,
            name="browser-use-full",
        )
        for provider, model in sorted(_BROWSER_USE_FULL_PROVIDER_MODELS)
    )
    for config in configurations:
        payload = _run_browser_use_worker(
            config=config,
            instruction="Static constructor-only policy audit.",
            target="http://127.0.0.1:7788/",
            kind="audit",
            builder=None,
        )
        policy = payload.get("policy")
        if not isinstance(policy, dict):
            raise RuntimeError("Browser Use constructor audit did not return a policy")
        results[f"{config.name}:{config.provider}/{config.model}"] = policy
    return results


def audit_browser_use_full_installation() -> dict[str, dict[str, object]]:
    """Compatibility view of the full-condition constructor audit."""

    return {
        key.removeprefix("browser-use-full:"): value
        for key, value in audit_browser_use_installation().items()
        if key.startswith("browser-use-full:")
    }


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
    _require_browser_use_version()

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
            _require_browser_use_model_capabilities(config)
            worker_payload = _run_browser_use_worker(
                config=config,
                instruction=instruction,
                target=target,
                kind="run",
                builder=builder,
            )
            effective_policy = worker_payload.get("policy")
            if not isinstance(effective_policy, dict):
                raise RuntimeError("Browser Use worker did not return an effective policy")
            builder.bind_effective_baseline(
                _browser_use_provenance(config, effective_policy=effective_policy)
            )
            raw = worker_payload.get("raw")
            history_payload = worker_payload.get("history")
            if not isinstance(raw, str) or history_payload is None:
                raise RuntimeError("Browser Use worker returned an incomplete result")
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
    if baseline in {"browser-use", "browser-use-full"}:
        config = resolve_browser_use_config(
            task,
            provider=provider,
            model=model,
            max_steps=max_steps,
            name=baseline,
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


def run_browser_use_full(
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
    """Run the clean-room expanded ``browser-use-full`` learned condition."""
    return run_external(
        baseline="browser-use-full",
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
