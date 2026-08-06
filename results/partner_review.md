# Independent research-partner review (adversarial)

> **HISTORICAL REVIEW:** This review informed the integrity rebuild. Any result
> claims or freeze references in it are superseded by `0.1.0` and are not
> canonical evidence. See `../docs/legacy-evidence.md`.

**Source:** independent critical-review subagent that read the actual repo files,
2026-08-06. Verdict up front: **the harness is well-engineered and the
fixture/oracle idea is genuinely good — but the pilot's headline "research
demonstration" is not what it is framed as, and its most novel metric (DB-oracle
exactly-once) has a hidden leakage and an untested central claim.**

## W1 — The pilot never actually tests the research question (on a model)
The only *model* baseline (Browser Use) recorded **`send_forwards: 0`** on the
send task — the agent never reached the send action, so the disconnect injection
was a **no-op** for it. The headline `sent_once` vs `sent_multiple` contrast was
produced by the *deterministic Playwright control*, whose reconciliation behavior
is hard-coded in `btb/baselines/play.py` — i.e. the agent's decision logic is
hand-written, not measured. The `/api/messages/send` request that the naive_retry
duplicate depends on is only minted client-side by the UI JS. The paper's central
claim ("measurement of state-inference under ambiguity for agents") is therefore
**not yet demonstrated by any actual agent run**.

## W2 — Prompt leakage defeats the H2 unknown-outcome claim
PROTOCOL hypothesis H2 ("agent should reconcile / report unknown") is largely
**written into the task instruction** (`msg_send_01.json`: "...If you are unsure
whether the send went through, report that explicitly instead of sending again").
A model that reports uncertainty may just be following the prompt. Must be
disclosed as a *prompted-capability* result, or the instruction neutralized (tell
the agent *just* "send the draft", no ambiguity hint) and measured separately.

## W3 — The naive-retry duplicate is environment-brittle
The blind-retry duplicate relies on the UI **minting a new `send_uid` per click**
(client-side JS/`server.py`). This is a specific mechanism, not a general rule; a
baseline that reuses one uid (or the agent reasoning "retry idempotently") would
NOT duplicate. The finding should be framed as "blind retry w/o reusing the
effect identity duplicates," not "retry leads to duplicate, period."

## W4 — Stochastic repeat discipline is missing
Only a handful of runs exist; no repeated/20× runs, no confidence intervals. The
`exact` control's reconciliation (a fixed sleep in `play.py`) is flaky-prone and
should be de-flaked (wait on a stable signal instead of a timestamp).

## W5 — Missing outcome class: "missed-send" (committed but agent says not-sent)
The taxonomy lacks the "message committed, but agent believes it was NOT sent"
class. That is a real, safety-relevant failure (silent divergence from ground
truth) distinct from `false_success` and `sent_zero_clean`.

## W6 — Declarative contract is decorative; scoring is imperative and drifting
PROTOCOL §6 promises machine-readable `intended_final`/`forbidden_final`, but
`btb/oracle/score.py` reimplements grading ad hoc and **ignores the JSON
`forbidden_final` blocks**. Concretely:
- `score.py` returns `sent_multiple` for `count>=2` and **never reaches the
  `forbidden_send` check** — so a duplicate send *and* a forbidden send collapse
  to `sent_multiple`, losing the distinction.
- `save` requires the *targeted* draft be saved but does not detect an **extra
  unauthorized saved draft** (e.g. the agent saves a 2nd draft) — 
  `cleanup_failed`/extra-state isn't checked in `save`.
The grader should *evaluate* the task JSON's `forbidden_final`, not re-hardcode it.

---

## Riskiest assumption that could kill the paper
**That an agent can be made to *reach* the ambiguous decision at all.** If real
agents exit before the send (as DeepSeek did at `send_forwards: 0`) or refuse to
reconcile, the paper has no "exactly-once under ambiguity" measurements.
**Cheap de-risk:** run a frontier vision-capable baseline ~20× NOW to get a
`send_forwards` distribution; if it reaches the send reliably, the paper is
viable; if not, redesign the task to give the agent a realistic shot.

## Recommended next steps (priority order)
1. **Frontier vision baseline** this session: get 20× `send_forwards`/duplicate
   distribution — proves both the task is reachable and the metric discriminates.
2. **De-flake the exact control** and run ~20× repeats; add confidence intervals.
3. **Bernoulli/randomized injection** (some runs inject, some don't, unknown to the
   agent) + structured uncertainty answer + **ambiguity-neutral prompt** (fix W2/W4).
4. **Enforce the JSON contract in the grader** (fix W6): evaluate `forbidden_final`,
   resolve `sent_multiple`→`forbidden_send` precedence, detect extra/unauthorized
   saves; add the **missed-send** outcome class (fix W5). Then bump freeze to
   `0_0_2` and re-run the full matrix once at freeze.
5. Write up with the "**DB oracle beats screenshot/agent self-report**" framing.
(Deferred: human-takeover and multi-tab — real value, higher effort, only after 1-4.)
