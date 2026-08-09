"""Child-only Browser Use construction and execution.

The parent creates and removes the sandbox.  This module deliberately receives
no benchmark database or repository path and must write its result below its
current working directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from btb.harness import engine


def _sandbox_path(name: str) -> Path:
    root = Path(os.environ["BTB_BROWSER_USE_SANDBOX_ROOT"]).resolve(strict=True)
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("worker path escaped its sandbox") from exc
    return path


def _write_result(value: dict[str, Any]) -> None:
    root = _sandbox_path(".")
    destination = root / "worker-result.json"
    temporary = root / ".worker-result.tmp"
    payload = json.dumps(value, sort_keys=True).encode("utf-8")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("safe worker result creation requires O_NOFOLLOW support")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, destination)


def _config(value: object) -> engine.BrowserUseConfig:
    if not isinstance(value, dict):
        raise ValueError("worker configuration must be an object")
    return engine.BrowserUseConfig(
        name=str(value["name"]),
        provider=str(value["provider"]),
        model=str(value["model"]),
        max_steps=int(value["max_steps"]),
        wall_s=float(value["wall_s"]),
        temperature=float(value.get("temperature", 0.0)),
        max_output_tokens=int(value.get("max_output_tokens", 4096)),
        provider_retries=int(value.get("provider_retries", 0)),
        excluded_actions=tuple(value["excluded_actions"]),
        allowed_actions=tuple(value["allowed_actions"]),
        use_vision=bool(value["use_vision"]),
        use_judge=bool(value["use_judge"]),
        max_actions_per_step=value.get("max_actions_per_step"),
    )


def _paths_are_confined(*paths: Path) -> bool:
    root = _sandbox_path(".")
    return all(path.resolve().is_relative_to(root) for path in paths)


def _take_provider_api_key(config: engine.BrowserUseConfig, *, kind: str) -> str:
    """Consume only the selected key before Browser Use can launch children."""

    credential_name = {
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(config.provider)
    if credential_name is None:
        raise ValueError(f"unsupported Browser Use provider: {config.provider}")
    api_key = os.environ.pop(credential_name, None)
    if kind == "audit":
        return "constructor-only-not-sent"
    if not api_key:
        raise RuntimeError("selected Browser Use provider credential is unavailable")
    return api_key


async def _construct_and_maybe_run(request: dict[str, Any]) -> dict[str, Any]:
    engine._require_browser_use_version()
    config = _config(request["config"])
    engine._require_browser_use_model_capabilities(config)
    target = str(request.get("target", "http://127.0.0.1:7788/"))
    instruction = str(request.get("instruction", "Constructor-only Browser Use audit."))
    kind = request.get("kind")
    if kind not in {"audit", "run"}:
        raise ValueError("worker kind must be audit or run")
    api_key = _take_provider_api_key(config, kind=kind)

    from browser_use import Agent, Browser, BrowserProfile, Tools

    profile_dir = _sandbox_path("browser-profile")
    downloads_dir = _sandbox_path("downloads")
    agent_files_dir = _sandbox_path("agent-files")
    if not _paths_are_confined(profile_dir, downloads_dir, agent_files_dir):
        raise RuntimeError("Browser Use path is outside its sandbox")
    profile = BrowserProfile(
        headless=True,
        user_data_dir=profile_dir,
        downloads_path=downloads_dir,
        accept_downloads=False,
        auto_download_pdfs=False,
        allowed_domains=[engine._fixture_origin_pattern(target)],
        captcha_solver=False,
        cross_origin_iframes=False,
        deterministic_rendering=True,
        enable_default_extensions=False,
        permissions=[],
        record_har_path=None,
        record_video_dir=None,
        traces_dir=None,
        storage_state=None,
    )
    browser = Browser(browser_profile=profile)
    primary_error: BaseException | None = None
    try:
        engine._audit_browser_use_browser(
            browser,
            _sandbox_path("."),
            target=target,
        )
        llm = engine._make_llm(
            config,
            api_key=api_key,
        )
        tools = engine._make_browser_tools(Tools, config, target=target)
        agent = engine._make_browser_use_agent(
            Agent,
            instruction=instruction,
            browser=browser,
            llm=llm,
            tools=tools,
            config=config,
            file_system_path=agent_files_dir,
        )
        policy = engine._audit_browser_use_agent(
            agent,
            config,
            sandbox_root=_sandbox_path("."),
            target=target,
        )
        if kind == "audit":
            return {"status": "ok", "policy": policy}
        history = await agent.run(max_steps=config.max_steps)
        final_result = getattr(history, "final_result", None)
        answer = final_result() if callable(final_result) else None
        return {
            "status": "ok",
            "policy": policy,
            "raw": "" if answer is None else str(answer),
            "history": engine._history_payload(history),
        }
    except BaseException as exc:
        primary_error = exc
        return {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            await browser.close()
        except Exception as close_error:
            if primary_error is None:
                raise
            raise RuntimeError("Browser Use Browser.close failed after worker failure") from close_error


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("worker request must be an object")
        result = asyncio.run(_construct_and_maybe_run(request))
    except Exception as exc:
        result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
    _write_result(result)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
