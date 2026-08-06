"""Top-level run lifecycle and receipt ownership tests."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from btb.harness import engine
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
        "run_fixture_origin_only"
    )


class _FakeActionRegistry:
    def __init__(self, action_names: set[str]) -> None:
        self.registry = type(
            "Registry",
            (),
            {"actions": {name: object() for name in action_names}},
        )()


class _FakeTools:
    action_names = set(engine._BROWSER_USE_ALLOWED_ACTIONS)

    def __init__(self, *, exclude_actions: list[str]) -> None:
        self.exclude_actions = exclude_actions
        self.registry = _FakeActionRegistry(self.action_names)


def _browser_config() -> engine.BrowserUseConfig:
    return engine.BrowserUseConfig(
        provider="deepseek",
        model="deepseek-chat",
        max_steps=5,
        wall_s=10,
    )


def test_browser_use_tools_match_the_exact_frozen_allowlist() -> None:
    tools = engine._make_browser_tools(_FakeTools, _browser_config())
    assert tools.exclude_actions == list(engine._BROWSER_USE_EXCLUDED_ACTIONS)


@pytest.mark.parametrize(
    ("action_names", "message"),
    [
        (
            set(engine._BROWSER_USE_ALLOWED_ACTIONS) | {"new_framework_action"},
            "unexpected=['new_framework_action']",
        ),
        (
            set(engine._BROWSER_USE_ALLOWED_ACTIONS) - {"navigate"},
            "missing=['navigate']",
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
        engine._make_browser_tools(DriftedTools, _browser_config())


@pytest.mark.parametrize("url", ["file:///tmp/fixture", "not-a-url", ""])
def test_fixture_origin_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTP\\(S\\) origin"):
        engine._fixture_origin_pattern(url)


def test_browser_use_telemetry_is_forcibly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "true")
    engine._disable_browser_use_telemetry()
    assert engine.os.environ["ANONYMIZED_TELEMETRY"] == "false"


def test_unreceipted_agent_overrides_are_rejected() -> None:
    with pytest.raises(ValueError, match="unreceipted overrides"):
        engine._reject_agent_overrides({"max_failures": 1})
