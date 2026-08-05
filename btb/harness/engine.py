"""Harness engine: orchestrates a single benchmark run.

A run: prepare DB -> start injection proxy if needed -> point the baseline at
the proxy/app -> execute baseline against the live app -> snapshot authoritative
DB -> write result manifest, accounting for the injection's effect.

Two baseline modes:
  - 'playwright-exact' / 'playwright-naive' : deterministic Playwright control.
  - 'browser-use'                          : Browser Use LLM agent baseline.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from btb.baselines import play as play_baseline
from btb.harness import inject as inject_mod
from btb.harness import manifest as manifest_mod
from btb.oracle import claim as claim_mod
from btb.oracle import score as score_mod
from btb.tasks import runner as task_runner

DEFAULT_BASE_URL = "http://127.0.0.1:7788"


def _resolve_base(inject_proxy, base_url: str) -> str:
    """Point the baseline at the proxy when an injection is active."""
    return inject_proxy.url if inject_proxy is not None else base_url


def _make_proxy(task: dict, base_url: str):
    inj = task.get("failure_injection") or {}
    if inj.get("kind") == "disconnect_after_possible_send":
        proxy = inject_mod.InjectProxy(base_url, inject_send=True).start()
        return proxy
    return None


def _scoring_context(task: dict, db_path: Path | str) -> dict:
    """Scoring context for a task, augmented with the initial DB snapshot (taken
    right after prepare_initial_state) so read-task scoring sees only new writes."""
    base = task_runner.load_definition(task["id"])["scoring_context"]
    init = score_mod.snapshot(db_path)
    return score_mod.scoring_context_with_initial(base, init)


def run_playwright(
    *,
    task: dict,
    base_url: str,
    db_path: Path | str,
    behavior: str,
    run_id: str,
) -> dict:
    """Run a task with the deterministic Playwright control."""
    from playwright.sync_api import sync_playwright

    task_runner.prepare_initial_state(db_path, task=task)
    scoring_context = _scoring_context(task, db_path)
    proxy = _make_proxy(task, base_url)
    target = _resolve_base(proxy, base_url)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            t0 = time.time()
            claim = play_baseline.run_control(page, target, task, behavior=behavior)
            duration = time.time() - t0
            browser.close()
    finally:
        pass

    injection_report = proxy.report() if proxy is not None else {"injection": "none"}
    snap, outcome = score_mod.load_and_score(
        db_path,
        context=scoring_context,
        agent_claimed_send=claim.get("claimed_send", False),
    )
    manifest_mod.write_manifest(
        run_id=run_id,
        freeze=task.get("freeze", "unknown"),
        baseline=behavior,
        model="deterministic-playwright",
        task_id=task["id"],
        task=task,
        db_state=snap.to_dict(),
        agent_claim=claim,
        injection=injection_report,
        outcome=outcome,
        duration_s=duration,
    )
    if proxy is not None:
        proxy.shutdown()
    return {
        "run_id": run_id,
        "task": task["id"],
        "outcome": outcome,
        "claim": claim,
        "db_state": snap.to_dict(),
    }


def run_browser_use(
    *,
    task: dict,
    base_url: str,
    db_path: Path | str,
    run_id: str,
    model: str | None = None,
    provider: str | None = None,
    max_steps: int | None = None,
    **agent_kwargs,
) -> dict:
    """Run a task with a Browser Use LLM agent baseline.

    Browser Use controls its own browser; we point it at the (possibly
    proxied) base_url. The proxy attaches to the real app, so the disconnect
    injection applies to this baseline too, identically to Playwright.
    """
    from browser_use import Agent, Browser

    import asyncio

    task_runner.prepare_initial_state(db_path, task=task)
    scoring_context = _scoring_context(task, db_path)
    proxy = _make_proxy(task, base_url)
    target = _resolve_base(proxy, base_url)

    instruction = _augment_instruction(task, target)
    llm = _make_llm(provider, model or "")  # model empty => provider default

    raw = ""
    t0 = time.time()
    try:

        async def _run() -> str:
            browser = Browser(headless=True, args=["--no-sandbox"])
            agent = Agent(
                task=instruction,
                browser=browser,
                llm=llm,
                use_judge=False,
                use_vision=False,  # text/DOM-driven app; avoids vision-model requirement
                **agent_kwargs,
            )
            result = await agent.run(
                max_steps=max_steps or task.get("budget", {}).get("steps", 20)
            )
            await browser.close()
            return str(result)

        raw = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        raw = f"ERROR: {exc.__class__.__name__}: {exc}"
    duration = time.time() - t0

    injection_report = proxy.report() if proxy is not None else {"injection": "none"}
    parsed = claim_mod.parse_claim(raw)
    claimed_send = parsed.claimed_send
    snap, outcome = score_mod.load_and_score(
        db_path,
        context=scoring_context,
        agent_claimed_send=claimed_send,
    )
    manifest_mod.write_manifest(
        run_id=run_id,
        freeze=task.get("freeze", "unknown"),
        baseline="browser-use",
        model=model or "default",
        task_id=task["id"],
        task=task,
        db_state=snap.to_dict(),
        agent_claim={
            "claimed_send": claimed_send,
            "believes": parsed.believes,
            "claim_source": parsed.sourced_from,
            "detail": raw[:2000],
        },
        injection=injection_report,
        outcome=outcome,
        duration_s=duration,
    )
    if proxy is not None:
        proxy.shutdown()
    return {
        "run_id": run_id,
        "task": task["id"],
        "outcome": outcome,
        "claimed_send": claimed_send,
        "db_state": snap.to_dict(),
    }


def _augment_instruction(task: dict, base_url: str) -> str:
    """Give the agent the concrete URL to open (it has no session memory)."""
    return (
        f"Open {base_url}/ in your browser. Then: {task['instruction']} "
        "Operate through the visible page controls only."
    )


def _make_llm(provider: str | None, model: str, api_key: str | None = None):
    """Build a browser_use ChatLLM for the requested provider.

    provider defaults to 'deepseek' (the fixture author's configured provider).
    Supported: deepseek (DEEPSEEK_API_KEY), openai (OPENAI_API_KEY).
    """
    provider = (provider or os.environ.get("BTB_LLM", "deepseek")).lower()
    if provider == "deepseek":
        from browser_use.llm.deepseek.chat import ChatDeepSeek

        return ChatDeepSeek(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            model=model or os.environ.get("BTB_MODEL", "deepseek-chat"),
        )
    if provider == "openai":
        from browser_use.llm.openai.chat import ChatOpenAI

        return ChatOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            model=model or os.environ.get("BTB_MODEL", "gpt-4o"),
        )
    if provider == "anthropic":
        from browser_use.llm.anthropic.chat import ChatAnthropic

        return ChatAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            model=model or os.environ.get("BTB_MODEL", "claude-sonnet-4"),
        )
    raise ValueError(f"unsupported Browser Use LLM provider: {provider}")


def _claimed_send(text: str) -> bool:
    """Heuristic: does the run output indicate the agent believes it sent?"""
    lowered = (text or "").lower()
    negated = any(
        n in lowered
        for n in ("did not send", "could not send", "failed", "unsure",
                  "cannot confirm", "unable to send", "not sent")
    )
    positive = "sent" in lowered or "message sent" in lowered or "submitted" in lowered
    return positive and not negated
