"""Top-level run lifecycle and receipt ownership tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest

from btb.harness import engine, manifest
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner


def _options(tmp_path: Path) -> engine.ReceiptOptions:
    return engine.ReceiptOptions(mode="exploratory", out_dir=tmp_path)


def _completed(builder) -> engine._CompletedRun:
    claim = claim_mod.parse_claim(
        '{"believes":"not_sent","subject":"Quarterly Plan",'
        '"body":"Align hiring with roadmap."}'
    )
    snapshot = score_mod.OracleSnapshot()
    evaluation = score_mod.Evaluation(
        functional_status="pass",
        effect_state="not_applicable",
        authorization_violations=[],
        duplicate_attempt_count=0,
        belief="not_sent",
        treatment_delivered=False,
        headline_outcome="read_only_ok",
    )
    builder.before_snapshot = snapshot.to_dict()
    builder.set_prompt("test executed prompt")
    builder.after_snapshot = snapshot.to_dict()
    builder.agent_claim = engine._manifest_claim(claim)
    builder.injection_report = {"injection": "none", "treatment_delivered": False}
    builder.evaluation = evaluation.to_dict()
    builder.outcome = evaluation.headline_outcome
    builder.write_json_trace({"steps": []}, kind="test-history")
    return engine._CompletedRun(claim=claim, after=snapshot, evaluation=evaluation)


def test_receipt_builder_for_binds_explicit_canonical_source_and_release(
    tmp_path: Path,
) -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        cwd=manifest.REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    from btb.harness import validate_manifest

    digest = validate_manifest._commit_source_sha256(manifest.REPO_ROOT, commit)
    assert digest is not None
    source = manifest.SourceProvenance(
        git_commit=commit,
        git_dirty=False,
        source_tree_sha256=digest,
    )
    builder = engine.receipt_builder_for(
        task=task_runner.load_definition("msg_read_01"),
        run_id="explicit-source-release",
        baseline="playwright-exact",
        provider=None,
        model="deterministic-playwright",
        max_steps=None,
        options=engine.ReceiptOptions(mode="canonical", out_dir=tmp_path),
        source=source,
        release=manifest.RELEASE_VERSION,
    )
    receipt_path = builder.write_failure(RuntimeError("offline source binding"), stage="test")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["source"] == source.to_dict()
    assert receipt["release"] == manifest.RELEASE_VERSION
    assert validate_manifest.validate_file(receipt_path, source_repo=manifest.REPO_ROOT) == []


def test_managed_setup_failure_emits_one_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenSetup:
        def __enter__(self):
            raise RuntimeError("fixture setup failed")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(engine.runtime, "managed_run_environment", lambda **_: BrokenSetup())
    result = engine.run_managed(
        baseline="playwright-exact",
        task=task_runner.load_definition("msg_read_01"),
        run_id="setup-failure",
        receipt_options=_options(tmp_path),
    )
    assert result["status"] == "failure"
    assert result["error"]["stage"] == "fixture_setup"
    receipts = list(tmp_path.glob("*.json"))
    assert [path.name for path in receipts] == ["setup-failure.json"]
    assert json.loads(receipts[0].read_text())["status"] == "setup_error"


def test_managed_environment_factory_failure_emits_one_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_factory(**_kwargs):
        raise RuntimeError("fixture factory failed")

    monkeypatch.setattr(engine.runtime, "managed_run_environment", fail_factory)
    result = engine.run_managed(
        baseline="playwright-exact",
        task=task_runner.load_definition("msg_read_01"),
        run_id="factory-failure",
        receipt_options=_options(tmp_path),
    )
    assert result["error"]["stage"] == "fixture_setup"
    receipt = json.loads((tmp_path / "factory-failure.json").read_text())
    assert receipt["status"] == "setup_error"


def test_success_receipt_is_written_only_after_fixture_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Fixture:
        base_url = "http://fixture.invalid"
        db_path = tmp_path / "fixture.db"
        ui_token = "fixture-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert not (tmp_path / "lifecycle-success.json").exists()
            return False

    monkeypatch.setattr(engine.runtime, "managed_run_environment", lambda **_: Fixture())
    monkeypatch.setattr(
        engine,
        "_execute_baseline",
        lambda **kwargs: _completed(kwargs["builder"]),
    )
    result = engine.run_managed(
        baseline="playwright-exact",
        task=task_runner.load_definition("msg_read_01"),
        run_id="lifecycle-success",
        receipt_options=_options(tmp_path),
    )
    assert result["status"] == "success"
    assert (tmp_path / "lifecycle-success.json").is_file()


def test_teardown_failure_replaces_success_with_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenTeardown:
        base_url = "http://fixture.invalid"
        db_path = tmp_path / "fixture.db"
        ui_token = "fixture-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise RuntimeError("fixture teardown failed")

    monkeypatch.setattr(
        engine.runtime,
        "managed_run_environment",
        lambda **_: BrokenTeardown(),
    )
    monkeypatch.setattr(
        engine,
        "_execute_baseline",
        lambda **kwargs: _completed(kwargs["builder"]),
    )
    result = engine.run_managed(
        baseline="playwright-exact",
        task=task_runner.load_definition("msg_read_01"),
        run_id="teardown-failure",
        receipt_options=_options(tmp_path),
    )
    assert result["status"] == "failure"
    assert result["error"]["stage"] == "fixture_teardown"
    receipt = json.loads((tmp_path / "teardown-failure.json").read_text())
    assert receipt["status"] == "setup_error"
    assert receipt["evaluation"] is None
    assert receipt["outcome"] is None


def test_primary_execution_failure_survives_teardown_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenTeardown:
        base_url = "http://fixture.invalid"
        db_path = tmp_path / "fixture.db"
        ui_token = "fixture-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise RuntimeError("teardown secondary")

    def fail_execution(**kwargs):
        raise engine._RunExecutionError(ValueError("baseline primary"), stage="baseline")

    monkeypatch.setattr(
        engine.runtime,
        "managed_run_environment",
        lambda **_: BrokenTeardown(),
    )
    monkeypatch.setattr(engine, "_execute_baseline", fail_execution)
    result = engine.run_managed(
        baseline="playwright-exact",
        task=task_runner.load_definition("msg_read_01"),
        run_id="dual-failure",
        receipt_options=_options(tmp_path),
    )
    assert result["error"] == {
        "type": "ValueError",
        "message": "baseline primary",
        "stage": "baseline",
    }
    receipt = json.loads((tmp_path / "dual-failure.json").read_text())
    assert receipt["execution"]["evidence_failures"][0]["stage"] == "fixture_teardown"


def test_unwrapped_execution_error_is_receipted_as_baseline_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Fixture:
        base_url = "http://fixture.invalid"
        db_path = tmp_path / "fixture.db"
        ui_token = "fixture-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fail_execution(**kwargs):
        del kwargs
        raise ImportError("optional baseline missing")

    monkeypatch.setattr(engine.runtime, "managed_run_environment", lambda **_: Fixture())
    monkeypatch.setattr(engine, "_execute_baseline", fail_execution)
    result = engine.run_managed(
        baseline="playwright-exact",
        task=task_runner.load_definition("msg_read_01"),
        run_id="unwrapped-baseline-failure",
        receipt_options=_options(tmp_path),
    )
    assert result["error"]["stage"] == "baseline"
    receipt = json.loads((tmp_path / "unwrapped-baseline-failure.json").read_text())
    assert receipt["status"] == "baseline_error"


def test_manifest_claim_never_persists_unsanitized_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-model-output")
    claim = claim_mod.parse_claim(
        '{"believes":"sent"} api_key=sk-private-model-output',
        allow_legacy=True,
    )
    persisted = engine._manifest_claim(claim)
    assert "raw" not in persisted
    assert "sk-private-model-output" not in persisted["detail"]
    assert persisted["detail_redacted"] is True


def test_manifest_claim_redacts_registered_runtime_secret() -> None:
    token = "fixture-runtime-capability-token"
    claim = claim_mod.parse_claim(
        '{"believes":"sent"} ' + token,
        allow_legacy=True,
    )
    persisted = engine._manifest_claim(
        claim,
        sensitive_values=(token,),
    )
    assert token not in persisted["detail"]
    assert persisted["detail_redacted"] is True


def test_browser_use_configuration_is_fully_resolved_and_receipted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BTB_LLM", " OPENAI ")
    monkeypatch.setenv("BTB_MODEL", "  gpt-4.1-mini-test  ")
    task = task_runner.load_definition("msg_send_01")
    config = engine.resolve_browser_use_config(
        task,
        provider=None,
        model=None,
        max_steps=7,
    )
    assert config == engine.BrowserUseConfig(
        provider="openai",
        model="gpt-4.1-mini-test",
        max_steps=7,
        wall_s=120.0,
    )
    provenance = engine._browser_use_provenance(config).to_dict()
    assert provenance["provider"] == "openai"
    assert provenance["model"] == "gpt-4.1-mini-test"
    assert provenance["parameters"]["llm_generation"] == {
        "temperature": 0.0,
        "max_output_tokens": 4096,
        "provider_retries": 0,
        "top_p": None,
        "seed": None,
    }


def test_make_llm_uses_the_same_resolved_generation_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict] = {}
    llm_module = ModuleType("browser_use.llm")

    def constructor(name: str):
        def build(**kwargs):
            captured[name] = kwargs
            return kwargs

        return build

    llm_module.ChatAnthropic = constructor("anthropic")
    llm_module.ChatDeepSeek = constructor("deepseek")
    llm_module.ChatOpenAI = constructor("openai")
    browser_use_module = ModuleType("browser_use")
    browser_use_module.llm = llm_module
    monkeypatch.setitem(sys.modules, "browser_use", browser_use_module)
    monkeypatch.setitem(sys.modules, "browser_use.llm", llm_module)

    config = engine.BrowserUseConfig(
        provider="deepseek",
        model="deepseek-chat",
        max_steps=5,
        wall_s=10,
    )
    engine._make_llm(config, api_key="test-key")
    assert captured["deepseek"] == {
        "api_key": "test-key",
        "model": "deepseek-chat",
        "temperature": 0.0,
        "max_tokens": 4096,
        "client_params": {"max_retries": 0},
    }


def test_browser_use_prompt_requires_an_exact_json_only_final_answer() -> None:
    prompt = engine._augment_instruction(
        task_runner.load_definition("msg_send_01"),
        "http://fixture.invalid",
    )
    assert "entire final answer must be exactly one JSON object" in prompt
    assert "Do not wrap the final JSON in Markdown" in prompt


def test_browser_use_navigation_is_origin_restricted_not_self_disabled() -> None:
    config = engine.BrowserUseConfig(
        provider="deepseek",
        model="deepseek-chat",
        max_steps=5,
        wall_s=10,
    )
    assert "navigate" not in config.excluded_actions
    assert engine._fixture_origin_pattern("http://127.0.0.1:4321") == (
        "http://127.0.0.1:4321/"
    )
    provenance = engine._browser_use_provenance(config).to_dict()
    assert provenance["capability_policy"]["enforcement"]["navigation"] == (
        {
            "action": "btb.fixture_only_navigate.v1",
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
        }
    )
    assert provenance["capability_policy"]["enforcement"]["external_search"] == {
        "action": "excluded",
        "behavior": "excluded",
    }


class _FakeActionRegistry:
    def __init__(self, action_names: set[str]) -> None:
        self.registry = type(
            "Registry",
            (),
            {"actions": {name: object() for name in action_names}},
        )()


class _FakeTools:
    action_names: ClassVar[set[str]] = set(engine._BROWSER_USE_ALLOWED_ACTIONS)

    def __init__(
        self, *, exclude_actions: list[str], display_files_in_done_text: bool
    ) -> None:
        self.exclude_actions = exclude_actions
        self.display_files_in_done_text = display_files_in_done_text
        self.registry = _FakeActionRegistry(self.action_names)

    def action(self, _description: str, **kwargs):
        def register(function):
            self.registry.registry.actions[function.__name__] = type(
                "RegisteredAction",
                (),
                {
                    "function": function,
                    "param_model": kwargs.get("param_model"),
                },
            )()
            return function

        return register


def _browser_config() -> engine.BrowserUseConfig:
    return engine.BrowserUseConfig(
        provider="deepseek",
        model="deepseek-chat",
        max_steps=5,
        wall_s=10,
    )


def test_browser_use_tools_match_the_exact_frozen_allowlist() -> None:
    tools = engine._make_browser_tools(
        _FakeTools,
        _browser_config(),
        target="http://127.0.0.1:7788/",
    )
    assert tools.exclude_actions == list(engine._BROWSER_USE_EXCLUDED_ACTIONS)


class _NavigationEvent:
    def __await__(self):
        async def completed():
            return None

        return completed().__await__()

    async def event_result(self, **_kwargs):
        return None


class _FailingNavigationEvent(_NavigationEvent):
    async def event_result(self, **_kwargs):
        raise RuntimeError("controlled transition dispatch failure")


class _NavigationBus:
    def __init__(self, session, observed_urls: list[str]) -> None:
        self.session = session
        self.observed_urls = observed_urls
        self.events: list[object] = []

    def dispatch(self, event):
        self.events.append(event)
        self.session.current_url = (
            self.observed_urls.pop(0)
            if self.observed_urls
            else getattr(event, "url", self.session.current_url)
        )
        return _NavigationEvent()


class _NavigationSession:
    def __init__(self, *, initial_url: str, observed_urls: list[str] | None = None) -> None:
        self.current_url = initial_url
        self.cdp_client = None
        self.event_bus = _NavigationBus(self, list(observed_urls or []))
        self.tab_id_requests: list[object] = []

    async def get_current_page_url(self) -> str:
        return self.current_url

    async def get_target_id_from_tab_id(self, tab_id: object) -> str:
        self.tab_id_requests.append(tab_id)
        return f"target-{tab_id}"


class _FailsFirstNavigationBus(_NavigationBus):
    def __init__(self, session) -> None:
        super().__init__(session, [])
        self.failed = False

    def dispatch(self, event):
        self.events.append(event)
        if not self.failed:
            self.failed = True
            return _FailingNavigationEvent()
        self.session.current_url = getattr(event, "url", self.session.current_url)
        return _NavigationEvent()


class _UnobservableOnceNavigationSession(_NavigationSession):
    def __init__(self, *, initial_url: str) -> None:
        super().__init__(initial_url=initial_url)
        self._failed_post_dispatch_observation = False

    async def get_current_page_url(self) -> str:
        if (
            len(self.event_bus.events) == 1
            and not self._failed_post_dispatch_observation
        ):
            self._failed_post_dispatch_observation = True
            raise RuntimeError("controlled missing focused URL")
        return await super().get_current_page_url()


def _runtime_navigation_tools(
    config: engine.BrowserUseConfig,
    *,
    target: str,
):
    from browser_use import Tools

    return engine._make_browser_tools(Tools, config, target=target)


@pytest.mark.parametrize(
    ("condition", "action_name"),
    [
        ("legacy", "go_back"),
        ("full", "go_back"),
        ("full", "switch"),
        ("full", "close"),
    ],
)
def test_browser_use_0136_builtin_transition_can_focus_about_blank(
    condition: str,
    action_name: str,
) -> None:
    """Pin the controlled topology seam that requires the custom wrappers."""

    import asyncio

    from browser_use import Tools

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = Tools(
        exclude_actions=list(config.excluded_actions),
        display_files_in_done_text=False,
    )
    session = _NavigationSession(initial_url=target, observed_urls=["about:blank"])
    kwargs = {"browser_session": session}
    if action_name in {"switch", "close"}:
        kwargs["tab_id"] = "abcd"

    asyncio.run(getattr(tools, action_name)(**kwargs))

    assert session.current_url == "about:blank"


@pytest.mark.parametrize(
    ("engine_name", "expected_url"),
    [
        ("duckduckgo", "https://duckduckgo.com/?q=fixture+safety"),
        (
            "google",
            "https://www.google.com/search?q=fixture+safety&udm=14",
        ),
        ("bing", "https://www.bing.com/search?q=fixture+safety"),
    ],
)
def test_browser_use_0136_builtin_search_dispatches_external_url_before_override(
    engine_name: str,
    expected_url: str,
) -> None:
    """Pin the exact upstream pre-watchdog external-search dispatch seam."""

    import asyncio

    from browser_use import Tools
    from browser_use.browser.events import NavigateToUrlEvent
    from browser_use.tools.views import SearchAction

    config = _browser_full_config()
    tools = Tools(
        exclude_actions=list(config.excluded_actions),
        display_files_in_done_text=False,
    )
    action = tools.registry.registry.actions["search"]
    session = _NavigationSession(initial_url="http://127.0.0.1:7788/inbox/")

    assert action.param_model is SearchAction
    assert action.terminates_sequence is True
    asyncio.run(
        tools.search(
            query="fixture safety",
            engine=engine_name,
            browser_session=session,
        )
    )

    assert len(session.event_bus.events) == 1
    event = session.event_bus.events[0]
    assert isinstance(event, NavigateToUrlEvent)
    assert event.url == expected_url


@pytest.mark.parametrize("engine_name", ["duckduckgo", "google", "bing"])
@pytest.mark.parametrize("query", ["fixture safety", "message status & profile"])
def test_browser_use_0136_fixture_search_rejects_without_dispatch(
    engine_name: str,
    query: str,
) -> None:
    """The retained full search schema has no fixture-external behavior."""

    import asyncio

    from browser_use.tools.views import SearchAction

    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(_browser_full_config(), target=target)
    action = tools.registry.registry.actions["search"]
    callback = action.function.__wrapped__
    session = _NavigationSession(initial_url=target)

    assert tuple(sorted(tools.registry.registry.actions)) == tuple(
        sorted(_browser_full_config().allowed_actions)
    )
    assert action.param_model is SearchAction
    assert action.param_model.model_json_schema() == SearchAction.model_json_schema()
    assert action.terminates_sequence is True
    assert callback.__func__ is engine._FixtureExternalSearchAction.search
    assert type(callback.__self__) is engine._FixtureExternalSearchAction

    result = asyncio.run(
        tools.search(
            query=query,
            engine=engine_name,
            browser_session=session,
        )
    )

    assert result.error == engine._FIXTURE_EXTERNAL_SEARCH_ERROR
    assert session.event_bus.events == []
    assert session.current_url == target


@pytest.mark.parametrize(
    "condition",
    ["legacy", "full"],
    ids=["legacy", "full"],
)
def test_browser_use_0136_replaces_navigate_with_exact_schema(
    condition: str,
) -> None:
    from browser_use.tools.views import NavigateAction

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    action = tools.registry.registry.actions["navigate"]
    callback = action.function.__wrapped__

    assert tuple(sorted(tools.registry.registry.actions)) == tuple(
        sorted(config.allowed_actions)
    )
    assert action.param_model is NavigateAction
    assert action.param_model.model_json_schema() == NavigateAction.model_json_schema()
    assert callback.__func__ is engine._FixtureNavigationAction.navigate
    assert isinstance(callback.__self__, engine._FixtureNavigationAction)
    assert callback.__self__.policy == engine._FixtureNavigationPolicy.from_target(target)


@pytest.mark.parametrize(
    ("condition", "action_name"),
    [
        ("legacy", "go_back"),
        ("full", "go_back"),
        ("full", "switch"),
        ("full", "close"),
        ("full", "search"),
    ],
)
def test_browser_use_0136_replaces_transition_actions_with_exact_schemas(
    condition: str,
    action_name: str,
) -> None:
    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    action = tools.registry.registry.actions[action_name]
    callback = action.function.__wrapped__
    expected_model = engine._fixture_action_models()[action_name]

    assert tuple(sorted(tools.registry.registry.actions)) == tuple(
        sorted(config.allowed_actions)
    )
    assert action.param_model is expected_model
    assert action.param_model.model_json_schema() == expected_model.model_json_schema()
    if action_name == "search":
        assert callback.__func__ is engine._FixtureExternalSearchAction.search
        assert type(callback.__self__) is engine._FixtureExternalSearchAction
    else:
        assert callback.__func__ is getattr(engine._FixtureNavigationAction, action_name)
        assert isinstance(callback.__self__, engine._FixtureNavigationAction)
        assert callback.__self__.policy == engine._FixtureNavigationPolicy.from_target(
            target
        )


@pytest.mark.parametrize(
    "action_name", ["navigate", "go_back", "switch", "close", "search"]
)
def test_fixture_transition_semantic_audit_rejects_rebound_method(
    action_name: str,
) -> None:
    import functools
    from types import MethodType

    target = "http://127.0.0.1:7788/inbox/"
    owner = (
        engine._FixtureExternalSearchAction()
        if action_name == "search"
        else engine._FixtureNavigationAction(
            engine._FixtureNavigationPolicy.from_target(target)
        )
    )

    async def substitute(_self, _params, _browser_session):
        return None

    rebound = MethodType(substitute, owner)

    @functools.wraps(rebound)
    async def wrapper(*args, **kwargs):
        return await rebound(*args, **kwargs)

    action = type(
        "RegisteredAction",
        (),
        {
            "function": wrapper,
            "param_model": engine._fixture_action_models()[action_name],
        },
    )()
    with pytest.raises(RuntimeError, match="fixture-only callback"):
        engine._audit_fixture_action(
            action,
            name=action_name,
            target=target,
            schema=engine._fixture_action_models()[action_name].model_json_schema(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "data:text/html,<script>alert(1)</script>",
        "blob:https://127.0.0.1:7788/value",
        "javascript:alert(1)",
        "file:///tmp/fixture",
        "about:blank",
        "chrome://settings/",
        "//outside.invalid/path",
        "http://user@127.0.0.1:7788/path",
        "http://outside.invalid/path",
        "http://[::1",
    ],
)
def test_fixture_navigate_rejects_hostile_urls_before_event_dispatch(url: str) -> None:
    import asyncio

    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(_browser_config(), target=target)
    session = _NavigationSession(initial_url=target)

    result = asyncio.run(tools.navigate(url=url, browser_session=session))

    assert result.error is not None
    assert session.event_bus.events == []


@pytest.mark.parametrize(
    ("target", "requested", "expected"),
    [
        (
            "http://127.0.0.1:7788/inbox/",
            "drafts?mode=all#saved",
            "http://127.0.0.1:7788/inbox/drafts?mode=all#saved",
        ),
        (
            "http://127.0.0.1:7788/inbox/",
            "/drafts?mode=all#saved",
            "http://127.0.0.1:7788/drafts?mode=all#saved",
        ),
        (
            "https://fixture.invalid/inbox/",
            "?mode=all#saved",
            "https://fixture.invalid/inbox/?mode=all#saved",
        ),
        (
            "https://fixture.invalid/inbox/",
            "https://fixture.invalid/drafts#saved",
            "https://fixture.invalid/drafts#saved",
        ),
    ],
)
def test_fixture_navigate_dispatches_allowed_resolved_urls(
    target: str,
    requested: str,
    expected: str,
) -> None:
    import asyncio

    tools = _runtime_navigation_tools(_browser_config(), target=target)
    session = _NavigationSession(initial_url=target)

    result = asyncio.run(tools.navigate(url=requested, browser_session=session))

    assert result.error is None
    assert [event.url for event in session.event_bus.events] == [expected]


@pytest.mark.parametrize(
    ("target", "requested", "expected"),
    [
        (
            "http://[::1]:80/",
            "http://[0:0:0:0:0:0:0:1]:80/path",
            "http://[::1]/path",
        ),
        (
            "http://bücher.example/",
            "http://xn--bcher-kva.example/path",
            "http://xn--bcher-kva.example/path",
        ),
        (
            "https://fixture.invalid:443/",
            "https://fixture.invalid/path",
            "https://fixture.invalid/path",
        ),
    ],
)
def test_fixture_navigation_normalizes_ipv6_idna_and_default_ports(
    target: str,
    requested: str,
    expected: str,
) -> None:
    assert engine._FixtureNavigationPolicy.from_target(target).resolve(requested) == expected


def test_fixture_navigate_detects_and_safely_recovers_an_off_origin_redirect() -> None:
    import asyncio

    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(_browser_config(), target=target)
    session = _NavigationSession(
        initial_url=target,
        observed_urls=["https://outside.invalid/", target],
    )

    result = asyncio.run(tools.navigate(url="/drafts", browser_session=session))

    assert "outside the fixture origin after dispatch" in result.error
    assert result.is_done is True
    assert result.success is False
    assert [event.url for event in session.event_bus.events] == [
        "http://127.0.0.1:7788/drafts",
        target,
    ]
    assert session.current_url == target


@pytest.mark.parametrize("condition", ["legacy", "full"], ids=["legacy", "full"])
def test_fixture_go_back_recovers_and_stops_after_initial_fixture_to_about_blank(
    condition: str,
) -> None:
    import asyncio

    from browser_use.browser.events import GoBackEvent, NavigateToUrlEvent

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    session = _NavigationSession(
        initial_url=target,
        observed_urls=["about:blank", target],
    )

    result = asyncio.run(tools.go_back(browser_session=session))

    assert result.is_done is True
    assert result.success is False
    assert "ended outside the fixture origin after dispatch" in result.error
    assert "fixture recovery succeeded" in result.error
    assert [type(event) for event in session.event_bus.events] == [
        GoBackEvent,
        NavigateToUrlEvent,
    ]
    assert session.current_url == target


@pytest.mark.parametrize("condition", ["legacy", "full"], ids=["legacy", "full"])
def test_fixture_go_back_allows_same_origin_history_result(condition: str) -> None:
    import asyncio

    from browser_use.browser.events import GoBackEvent

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    session = _NavigationSession(initial_url=target, observed_urls=[target])

    result = asyncio.run(tools.go_back(browser_session=session))

    assert result.error is None
    assert [type(event) for event in session.event_bus.events] == [GoBackEvent]
    assert session.current_url == target


@pytest.mark.parametrize("condition", ["legacy", "full"], ids=["legacy", "full"])
def test_fixture_go_back_stops_when_safe_recovery_fails(condition: str) -> None:
    import asyncio

    from browser_use.browser.events import GoBackEvent, NavigateToUrlEvent

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    session = _NavigationSession(
        initial_url=target,
        observed_urls=["about:blank", "about:blank"],
    )

    result = asyncio.run(tools.go_back(browser_session=session))

    assert result.is_done is True
    assert result.success is False
    assert "fixture recovery failed" in result.error
    assert [type(event) for event in session.event_bus.events] == [
        GoBackEvent,
        NavigateToUrlEvent,
    ]
    assert session.current_url == "about:blank"


@pytest.mark.parametrize("condition", ["legacy", "full"], ids=["legacy", "full"])
def test_fixture_go_back_recovers_and_stops_after_dispatch_error(
    condition: str,
) -> None:
    import asyncio

    from browser_use.browser.events import GoBackEvent, NavigateToUrlEvent

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    session = _NavigationSession(initial_url=target)
    session.event_bus = _FailsFirstNavigationBus(session)

    result = asyncio.run(tools.go_back(browser_session=session))

    assert result.is_done is True
    assert result.success is False
    assert "did not complete safely" in result.error
    assert "fixture recovery succeeded" in result.error
    assert [type(event) for event in session.event_bus.events] == [
        GoBackEvent,
        NavigateToUrlEvent,
    ]
    assert session.current_url == target


@pytest.mark.parametrize("condition", ["legacy", "full"], ids=["legacy", "full"])
def test_fixture_go_back_recovers_and_stops_after_unobservable_result(
    condition: str,
) -> None:
    import asyncio

    from browser_use.browser.events import GoBackEvent, NavigateToUrlEvent

    config = _browser_config() if condition == "legacy" else _browser_full_config()
    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(config, target=target)
    session = _UnobservableOnceNavigationSession(initial_url=target)

    result = asyncio.run(tools.go_back(browser_session=session))

    assert result.is_done is True
    assert result.success is False
    assert "result could not be observed after dispatch" in result.error
    assert "fixture recovery succeeded" in result.error
    assert [type(event) for event in session.event_bus.events] == [
        GoBackEvent,
        NavigateToUrlEvent,
    ]
    assert session.current_url == target


@pytest.mark.parametrize("action_name", ["switch", "close"])
def test_fixture_full_tab_transition_recovers_and_stops_after_about_blank(
    action_name: str,
) -> None:
    import asyncio

    from browser_use.browser.events import CloseTabEvent, NavigateToUrlEvent, SwitchTabEvent

    target = "http://127.0.0.1:7788/inbox/"
    tools = _runtime_navigation_tools(_browser_full_config(), target=target)
    session = _NavigationSession(
        initial_url=target,
        observed_urls=["about:blank", target],
    )

    result = asyncio.run(
        getattr(tools, action_name)(tab_id="abcd", browser_session=session)
    )

    expected_event = SwitchTabEvent if action_name == "switch" else CloseTabEvent
    assert result.is_done is True
    assert result.success is False
    assert "fixture recovery succeeded" in result.error
    assert session.tab_id_requests == ["abcd"]
    assert [type(event) for event in session.event_bus.events] == [
        expected_event,
        NavigateToUrlEvent,
    ]
    assert session.event_bus.events[0].target_id == "target-abcd"
    assert session.current_url == target


def test_benchmark_screenshot_callback_has_no_path_and_requests_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    lifecycle = engine.browser_use_sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    original_cwd = Path.cwd()
    try:
        environment = paths.environment(provider="openai")
        for name, value in environment.items():
            if name != "PATH" and not name.endswith("_API_KEY"):
                monkeypatch.setenv(name, value)
        monkeypatch.chdir(paths.root)
        result = asyncio.run(engine._benchmark_no_file_screenshot())
        assert result.extracted_content == "Screenshot requested for the next observation"
        assert result.metadata == {"include_screenshot": True}
    finally:
        monkeypatch.chdir(original_cwd)
        assert lifecycle.cleanup() is True


@pytest.mark.parametrize(
    ("action_names", "message"),
    [
        (
            set(engine._BROWSER_USE_ALLOWED_ACTIONS) | {"new_framework_action"},
            "unexpected=['new_framework_action']",
        ),
        (
            set(engine._BROWSER_USE_ALLOWED_ACTIONS) - {"click"},
            "missing=['click']",
        ),
    ],
)
def test_browser_use_tools_fail_closed_on_action_registry_drift(
    action_names: set[str],
    message: str,
) -> None:
    class DriftedTools(_FakeTools):
        pass

    DriftedTools.action_names = action_names
    with pytest.raises(RuntimeError, match=re.escape(message)):
        engine._make_browser_tools(
            DriftedTools,
            _browser_config(),
            target="http://127.0.0.1:7788/",
        )


@pytest.mark.parametrize(
    ("worker_result", "expected_error"),
    [
        (
            engine.browser_use_sandbox.WorkerResult(
                payload={"status": "error", "error_type": "RuntimeError"},
                timed_out=False,
                return_code=1,
            ),
            RuntimeError,
        ),
        (
            engine.browser_use_sandbox.WorkerResult(
                payload=None,
                timed_out=True,
                return_code=-15,
            ),
            TimeoutError,
        ),
        (
            engine.browser_use_sandbox.WorkerResult(
                payload={"status": "ok"},
                timed_out=False,
                return_code=0,
                teardown_error="orphan remained",
            ),
            RuntimeError,
        ),
    ],
)
def test_browser_use_child_failure_and_timeout_cleanup_before_raising(
    monkeypatch: pytest.MonkeyPatch,
    worker_result: engine.browser_use_sandbox.WorkerResult,
    expected_error: type[Exception],
) -> None:
    roots: list[Path] = []
    original_create = engine.browser_use_sandbox.SandboxLifecycle.create

    def track_create(self):
        paths = original_create(self)
        roots.append(paths.root)
        return paths

    monkeypatch.setattr(
        engine.browser_use_sandbox.SandboxLifecycle, "create", track_create
    )
    monkeypatch.setattr(
        engine.browser_use_sandbox,
        "run_worker",
        lambda *_args, **_kwargs: worker_result,
    )
    with pytest.raises(expected_error):
        engine._run_browser_use_worker(
            config=_browser_config(),
            instruction="constructor-only test",
            target="http://127.0.0.1:7788/",
            kind="audit",
            builder=None,
        )
    assert roots and all(not root.exists() for root in roots)


def test_browser_use_cleanup_failure_writes_a_redacted_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(engine, "_installed_version", lambda _name: "0.13.6")
    task = task_runner.load_definition("msg_read_01")
    builder = engine.receipt_builder_for(
        task=task,
        run_id="sandbox-cleanup-failure",
        baseline="browser-use",
        provider="deepseek",
        model="deepseek-chat",
        max_steps=1,
        options=_options(tmp_path),
    )
    roots: list[Path] = []

    def fail_cleanup(self) -> bool:
        assert self.paths is not None
        roots.append(self.paths.root)
        self.cleanup_error = PermissionError(f"cleanup denied at {self.paths.root}")
        return False

    monkeypatch.setattr(engine.browser_use_sandbox.SandboxLifecycle, "cleanup", fail_cleanup)
    monkeypatch.setattr(
        engine.browser_use_sandbox,
        "run_worker",
        lambda *_args, **_kwargs: engine.browser_use_sandbox.WorkerResult(
            payload={"status": "ok"}, timed_out=False, return_code=0
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="sandbox cleanup failed"):
            engine._run_browser_use_worker(
                config=_browser_config(),
                instruction="constructor-only test",
                target="http://127.0.0.1:7788/",
                kind="audit",
                builder=builder,
            )
        receipt_path = builder.write_failure(RuntimeError("cleanup failed"), stage="baseline")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        filesystem = receipt["framework_filesystem"]
        assert filesystem["state"] == "cleanup_failed"
        assert filesystem["cleanup_verified"] is False
        assert str(roots[0]) not in receipt_path.read_text(encoding="utf-8")
        from btb.harness import validate_manifest

        assert validate_manifest.validate_file(receipt_path, source_repo=None) == []
    finally:
        for root in roots:
            shutil.rmtree(root)
            assert not os.path.lexists(root)


def test_browser_use_partial_setup_cleanup_failure_is_receipted_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(engine, "_installed_version", lambda _name: "0.13.6")
    task = task_runner.load_definition("msg_read_01")
    builder = engine.receipt_builder_for(
        task=task,
        run_id="sandbox-partial-setup-cleanup-failure",
        baseline="browser-use",
        provider="deepseek",
        model="deepseek-chat",
        max_steps=1,
        options=_options(tmp_path),
    )
    roots: list[Path] = []

    def fail_child_setup(
        _paths: engine.browser_use_sandbox.SandboxPaths,
    ) -> tuple[Path, ...]:
        raise RuntimeError("forced partial setup failure")

    def fail_cleanup(self) -> bool:
        assert self.root is not None
        roots.append(self.root)
        self.cleanup_error = PermissionError(f"cleanup denied at {self.root}")
        return False

    monkeypatch.setattr(
        engine.browser_use_sandbox.SandboxPaths,
        "_children",
        fail_child_setup,
    )
    monkeypatch.setattr(engine.browser_use_sandbox.SandboxLifecycle, "cleanup", fail_cleanup)
    try:
        with pytest.raises(RuntimeError, match="forced partial setup failure"):
            engine._run_browser_use_worker(
                config=_browser_config(),
                instruction="constructor-only test",
                target="http://127.0.0.1:7788/",
                kind="audit",
                builder=builder,
            )
        receipt_path = builder.write_failure(RuntimeError("setup failed"), stage="baseline")
        receipt_text = receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        filesystem = receipt["framework_filesystem"]
        assert filesystem["state"] == "cleanup_failed"
        assert filesystem["cleanup_verified"] is False
        assert filesystem["cleanup_error"]["message"] == (
            "cleanup denied at <redacted:RUNTIME_SECRET>"
        )
        assert filesystem["inventory"] is not None
        assert str(roots[0]) not in receipt_text
        from btb.harness import validate_manifest

        assert validate_manifest.validate_file(receipt_path, source_repo=None) == []
    finally:
        for root in roots:
            shutil.rmtree(root)
            assert not os.path.lexists(root)


def test_browser_use_partial_setup_cleanup_success_is_truthfully_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "_installed_version", lambda _name: "0.13.6")
    task = task_runner.load_definition("msg_read_01")
    builder = engine.receipt_builder_for(
        task=task,
        run_id="sandbox-partial-setup-cleaned",
        baseline="browser-use",
        provider="deepseek",
        model="deepseek-chat",
        max_steps=1,
        options=_options(tmp_path),
    )
    roots: list[Path] = []

    def fail_child_setup(
        _paths: engine.browser_use_sandbox.SandboxPaths,
    ) -> tuple[Path, ...]:
        raise RuntimeError("forced partial setup failure")

    original_cleanup = engine.browser_use_sandbox.SandboxLifecycle.cleanup

    def track_cleanup(self) -> bool:
        assert self.root is not None
        roots.append(self.root)
        return original_cleanup(self)

    monkeypatch.setattr(
        engine.browser_use_sandbox.SandboxPaths,
        "_children",
        fail_child_setup,
    )
    monkeypatch.setattr(
        engine.browser_use_sandbox.SandboxLifecycle,
        "cleanup",
        track_cleanup,
    )
    with pytest.raises(RuntimeError, match="forced partial setup failure"):
        engine._run_browser_use_worker(
            config=_browser_config(),
            instruction="constructor-only test",
            target="http://127.0.0.1:7788/",
            kind="audit",
            builder=builder,
        )
    receipt_path = builder.write_failure(RuntimeError("setup failed"), stage="baseline")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    filesystem = receipt["framework_filesystem"]
    assert filesystem["state"] == "cleaned"
    assert filesystem["cleanup_verified"] is True
    assert filesystem["cleanup_error"] is None
    assert filesystem["inventory"] is not None
    assert roots and all(not root.exists() for root in roots)
    from btb.harness import validate_manifest

    assert validate_manifest.validate_file(receipt_path, source_repo=None) == []


def test_browser_use_root_registration_failure_is_redacted_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(engine, "_installed_version", lambda _name: "0.13.6")
    task = task_runner.load_definition("msg_read_01")
    builder = engine.receipt_builder_for(
        task=task,
        run_id="sandbox-root-registration-failure",
        baseline="browser-use",
        provider="deepseek",
        model="deepseek-chat",
        max_steps=1,
        options=_options(tmp_path),
    )
    roots: list[Path] = []
    inventory_roots: list[Path] = []
    original_register = builder.register_framework_sandbox
    original_inventory = engine.browser_use_sandbox.inventory_sandbox

    def reject_root(root: Path) -> None:
        roots.append(root)
        original_register(root)
        raise RuntimeError(f"forced root registration failure at {root}")

    def track_inventory(root: Path) -> dict:
        inventory_roots.append(root)
        return original_inventory(root)

    monkeypatch.setattr(builder, "register_framework_sandbox", reject_root)
    monkeypatch.setattr(
        engine.browser_use_sandbox,
        "inventory_sandbox",
        track_inventory,
    )
    monkeypatch.setattr(
        engine.browser_use_sandbox,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("worker started after root registration failed"),
    )
    try:
        with pytest.raises(RuntimeError, match="forced root registration failure"):
            engine._run_browser_use_worker(
                config=_browser_config(),
                instruction="constructor-only test",
                target="http://127.0.0.1:7788/",
                kind="audit",
                builder=builder,
            )
        assert roots
        assert inventory_roots == [roots[0]]
        receipt_path = builder.write_failure(
            RuntimeError(f"root registration failed at {roots[0]}"),
            stage="baseline",
        )
        receipt_text = receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        filesystem = receipt["framework_filesystem"]
        assert filesystem["state"] == "cleaned"
        assert filesystem["cleanup_verified"] is True
        assert filesystem["cleanup_error"] is None
        assert filesystem["inventory"] is not None
        assert str(roots[0]) not in receipt_text
        assert all(not os.path.lexists(root) for root in roots)
        from btb.harness import validate_manifest

        assert validate_manifest.validate_file(receipt_path, source_repo=None) == []
    finally:
        for root in roots:
            if os.path.lexists(root):
                shutil.rmtree(root)
            assert not os.path.lexists(root)


def _browser_full_config() -> engine.BrowserUseConfig:
    return engine.resolve_browser_use_config(
        task_runner.load_definition("msg_send_01"),
        provider="openai",
        model="gpt-4.1-mini",
        max_steps=5,
        name="browser-use-full",
    )


def test_browser_use_full_configuration_and_provenance_are_exact() -> None:
    config = _browser_full_config()
    assert config.name == "browser-use-full"
    assert config.allowed_actions == engine._BROWSER_USE_FULL_ALLOWED_ACTIONS
    assert config.excluded_actions == engine._BROWSER_USE_FULL_EXCLUDED_ACTIONS
    assert engine._browser_use_agent_options(config) == {
        "available_file_paths": [],
        "directly_open_url": True,
        "display_files_in_done_text": False,
        "fallback_llm": None,
        "generate_gif": False,
        "message_compaction": False,
        "save_conversation_path": None,
        "use_judge": False,
        "use_vision": True,
        "max_actions_per_step": 8,
    }

    provenance = engine._browser_use_provenance(config).to_dict()
    assert provenance["name"] == "browser-use-full"
    assert provenance["parameters"]["use_vision"] is True
    assert provenance["parameters"]["use_judge"] is False
    assert provenance["parameters"]["max_actions_per_step"] == 8
    assert provenance["parameters"]["framework_compatibility"] == {
        "constructor_use_vision": "auto",
        "effective_use_vision_bound_after_construction": True,
        "post_agent_semantic_audit": True,
    }
    assert provenance["parameters"]["effective_policy"] == {
        "status": "unobserved"
    }
    assert provenance["modality_policy"] == {"dom": True, "vision": True}
    enforcement = provenance["capability_policy"]["enforcement"]
    assert enforcement["agent_tools_allowed"] == list(config.allowed_actions)
    assert enforcement["agent_tools_excluded"] == list(config.excluded_actions)


def test_browser_use_restricted_policy_and_runtime_options_remain_frozen() -> None:
    config = _browser_config()
    provenance = engine._browser_use_provenance(config).to_dict()
    assert config.name == "browser-use"
    assert engine._browser_use_agent_options(config) == {
        "available_file_paths": [],
        "directly_open_url": True,
        "display_files_in_done_text": False,
        "fallback_llm": None,
        "generate_gif": False,
        "message_compaction": False,
        "save_conversation_path": None,
        "use_judge": False,
        "use_vision": False,
    }
    assert "max_actions_per_step" not in provenance["parameters"]
    assert provenance["modality_policy"] == {"dom": True, "vision": False}
    enforcement = provenance["capability_policy"]["enforcement"]
    assert enforcement["agent_tools_allowed"] == list(
        engine._BROWSER_USE_ALLOWED_ACTIONS
    )
    assert enforcement["agent_tools_excluded"] == list(
        engine._BROWSER_USE_EXCLUDED_ACTIONS
    )


@pytest.mark.parametrize(
    ("provider", "model"),
    [(None, None), ("openai", None), (None, "gpt-4.1-mini")],
)
def test_browser_use_full_requires_explicit_provider_and_model(
    provider: str | None,
    model: str | None,
) -> None:
    with pytest.raises(ValueError, match="requires an explicit"):
        engine.resolve_browser_use_config(
            task_runner.load_definition("msg_send_01"),
            provider=provider,
            model=model,
            max_steps=5,
            name="browser-use-full",
        )


def test_browser_use_full_rejects_non_allowlisted_provider_model() -> None:
    with pytest.raises(ValueError, match="not statically allowlisted"):
        engine.resolve_browser_use_config(
            task_runner.load_definition("msg_send_01"),
            provider="deepseek",
            model="deepseek-chat",
            max_steps=5,
            name="browser-use-full",
        )


def test_browser_use_restricted_preserves_deepseek_support() -> None:
    engine._require_browser_use_model_capabilities(_browser_config())


def test_browser_use_full_rejects_agent_vision_override() -> None:
    settings = type("Settings", (), {"use_vision": False})()
    agent = type("OverriddenAgent", (), {"settings": settings})()
    with pytest.raises(RuntimeError, match="changed the vision compatibility setting"):
        engine._bind_browser_use_agent_vision(agent, _browser_full_config())


def test_browser_use_0136_child_constructor_audit_covers_full_and_legacy() -> None:
    from browser_use.tools.views import NavigateAction
    from btb.harness import browser_use_policy

    policies = engine.audit_browser_use_installation()
    assert set(policies) == {
        "browser-use:deepseek/deepseek-chat",
        "browser-use-full:anthropic/claude-sonnet-4-0",
        "browser-use-full:openai/gpt-4.1-mini",
    }
    full = policies["browser-use-full:openai/gpt-4.1-mini"]
    assert full["settings"] == {
        "available_file_paths": [],
        "directly_open_url": True,
        "display_files_in_done_text": False,
        "generate_gif": False,
        "max_actions_per_step": 8,
        "message_compaction": False,
        "save_conversation_path": None,
        "use_judge": False,
        "use_vision": True,
    }
    assert full["framework_paths"] == {
        "agent_directory": "sandbox_root_only",
        "file_system_base": "sandbox_root_only",
        "file_system_data": "sandbox_root_only",
        "screenshot_storage": "sandbox_root_only",
    }
    assert full["llm_roles"]["judge"] == {
        "active": False,
        "provider": "openai",
        "model": "gpt-4.1-mini",
    }
    assert full["action_model_names"] == list(
        engine._BROWSER_USE_FULL_ALLOWED_ACTIONS
    )
    assert {action["name"] for action in full["actions"]} >= {"done", "extract"}
    assert all(
        isinstance(action["parameter_schema"], dict) for action in full["actions"]
    )
    screenshot = next(
        action for action in full["actions"] if action["name"] == "screenshot"
    )
    assert screenshot["parameter_schema"]["properties"] == {}
    assert screenshot["callback_identity"] == "btb.no_file_screenshot.v1"
    legacy = policies["browser-use:deepseek/deepseek-chat"]
    assert legacy["settings"]["use_vision"] is False
    assert legacy["settings"]["max_actions_per_step"] == 5
    assert "done" in legacy["action_model_names"]
    assert "screenshot" not in legacy["action_model_names"]
    for policy in (legacy, full):
        navigate = next(
            action for action in policy["actions"] if action["name"] == "navigate"
        )
        assert navigate["parameter_schema"] == NavigateAction.model_json_schema()
        assert navigate["callback_identity"] == "btb.fixture_only_navigate.v1"
        assert navigate["callback_behavior"] == (
            "pre_dispatch_exact_http_origin_validation;"
            "post_dispatch_detect_recover_and_stop_on_unverified_outcome"
        )
    for condition, policy in policies.items():
        name, provider_model = condition.split(":", 1)
        provider, model = provider_model.split("/", 1)
        frozen = browser_use_policy.schema_digests_for(name, provider, model)
        assert policy["schema_condition"] == condition
        assert policy["action_model_schema_sha256"] == frozen["action_model"]
        assert {
            action["name"]: action["parameter_schema_sha256"]
            for action in policy["actions"]
        } == frozen["actions"]
        expected_fixture_actions = {"navigate", "go_back"}
        if name == "browser-use-full":
            expected_fixture_actions.update({"switch", "close", "search"})
        for action_name in expected_fixture_actions:
            action = next(
                candidate
                for candidate in policy["actions"]
                if candidate["name"] == action_name
            )
            assert action["callback_identity"] == (
                browser_use_policy.FIXTURE_ACTION_CALLBACKS[action_name]["identity"]
            )
            assert action["callback_behavior"] == (
                browser_use_policy.FIXTURE_ACTION_CALLBACKS[action_name]["behavior"]
            )


def test_browser_use_version_gate_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_installed_version", lambda _name: "0.13.7")
    with pytest.raises(RuntimeError, match="browser-use==0.13.6"):
        engine._require_browser_use_version()


def test_browser_use_full_routes_through_the_learned_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    expected = object()

    def fake_execute_browser_use(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(engine, "_execute_browser_use", fake_execute_browser_use)
    result = engine._execute_baseline(
        baseline="browser-use-full",
        task=task_runner.load_definition("msg_send_01"),
        base_url="http://fixture.invalid",
        database_path=Path("unused.sqlite3"),
        builder=object(),
        provider="openai",
        model="gpt-4.1-mini",
        max_steps=4,
    )
    assert result is expected
    config = captured["config"]
    assert isinstance(config, engine.BrowserUseConfig)
    assert config.name == "browser-use-full"
    assert config.provider == "openai"
    assert config.model == "gpt-4.1-mini"
    assert engine._browser_use_agent_options(config)["max_actions_per_step"] == 8


@pytest.mark.parametrize("url", ["file:///tmp/fixture", "not-a-url", ""])
def test_fixture_origin_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTP\\(S\\) origin|non-empty URL"):
        engine._fixture_origin_pattern(url)


def test_unreceipted_agent_overrides_are_rejected() -> None:
    with pytest.raises(ValueError, match="unreceipted overrides"):
        engine._reject_agent_overrides({"max_failures": 1})
