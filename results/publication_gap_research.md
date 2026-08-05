# Publication-gap & literature report (BrowserTransactionBench)

**Source:** independent research subagent (arxiv API + browser verification), 2026-08-06.
**Purpose:** ground the paper intro and preempt novelty-threat reviewers.

## 1. Novelty verdict

**Defensible gap — with one important caveat.** No existing work is a DB-oracle-
grounded benchmark for *browser-use agents* evaluating exactly-once side-effect
correctness coupled with interruption/ambiguous-outcome/disconnect-after-dispatch.
Components exist as separate agent-mechanism papers or in different environments.
The narrowly claimable contribution:

> no benchmark wires an authoritative **DB oracle** into a **browser-rendered app**
> so ground truth is exactly "message sent ≥0/1/∞ times", then **injects
> ambiguity** (disconnect post-dispatch, verifier failure, interruption) to force
> the agent to *infer what actually happened* — not just finish the task.

Caveat: the space is moving fast (≥4 adjacent works in the current window). Don't
claim "first browser benchmark"; claim the *coupling* (transactional exactly-once
measurement × state-inference under ambiguity, scoped to browser agents).

## 2. Closest related works (differentiate against these)

Directly adjacent / dangerous:
- **Safety Invariants for Agents Orchestrating Irreversible State Transitions**
  — arXiv **2608.00783**. Exactly-once under ambiguous outcomes on *public
  ledgers*, agent-side formalism, not a browser benchmark.
- **ToolPro: Tool Programs...** — arXiv **2606.19992** (ICML 2026). Effect-aware
  replay for exactly-once over MCP; plumbing, not a browser benchmark.
- **ROGUE** — arXiv **2606.00341**. Human interrupt/Stop/corrigibility for
  *computer-use* agents; tests if agent *overrides* interrupt, not if it
  *reconstructs* what happened.
- **Bounded Autonomy for Enterprise AI** — arXiv **2604.14723**. Reports the
  unconstrained system *"hallucinated success"*, app-as-source-of-truth; an
  architecture evaluation, not a reusable benchmark.
- **AgentHazard** — **2604.02947**. Harmful-behavior benchmark; orthogonal.
- **OSWorld-Human** — **2506.16042**. Efficiency/speed, not effect correctness.

Security:
- **AgentDojo** — **2406.13352**. Prompt-injection security on tool calls;
  "no unauthorized side effects" but attack/defense, not duplicate accounting.
- **AgentSpec** — **2412.14224**. Static tool-spec analyzer, not a live benchmark.

Canonical browser benchmarks to contrast ("functional success ≠ transactional"):
- **Mind2Web** — **2306.06070**; **WebArena** — **2307.13854** (no DB oracle, no
  interruption, would allow repeat sends); **WebVoyager** — **2401.13919**
  (LLM-judge, hallucination-prone = the false-success risk); **OSWorld** —
  **2404.07972**; **BrowserGym/WebGym** — **2412.05467**; **WebShop** —
  **2207.01206**.

False-success / verifier-failure hook:
- **ToolEmu** — **2407.21745**; **AgentBoard** — **2412.13189** (over-optimistic
  graders); **ProcBench** — **2507.20482** (machine-checkable success). Our DB
  oracle is a perfect verifier that removes judge/LLM-grader false success.

Closest methodological precedent (DB-as-oracle, other interface):
- **IDB-Bench** — **2410.05074**. Uses the DB as ground truth but agent interface
  is SQL/text, no rendered browser UI, no exactly-once/duplicate, no interruption.

## 3. Canonical references to cite in intro

1. WebArena **2307.13854**
2. OSWorld **2404.07972**
3. WebVoyager **2401.13919**
4. Mind2Web **2306.06070**
5. AgentDojo **2406.13352**
6. ToolEmu **2407.21745**
7. AgentBoard **2412.13189**
8. BrowserGym **2412.05467** (or WebShop **2207.01206** as lineage)
Plus the §2A siblings (2606.19992, 2608.00783, 2606.00341) to preempt novelty
reviewers, and the exactly-once/idempotency anchor (Kreps' "You Cannot Have
Exactly-Once Delivery" + classic transactional atomicity).

## 4. Existing DB-oracle exactly-once message-send test?

**No.** No browser benchmark wires a DB oracle behind a rendered UI with 0/1/∞
send-counting + interruption. Closest is DB-agents (IDB-Bench) with a textual
interface. This conjunction is the novelty.

## 5. Recommended framing (avoid the false-success pitfall)

1. Lead with the **measurement gap** — functional end-state success ≠ effect-level
   truth (repeat sends pass; LLM-judges hallucinate; silence under interruption).
2. **Name threat-of-novelty papers explicitly** and differentiate in 2-3 sentences.
   Add a "Relationship to concurrent work" paragraph.
3. Position along **3 axes**: (i) Measurement (DB oracle = perfect verifier),
   (ii) Task class (browser, real DOM), (iii) Stress dimension (interruption/
   disconnect/verifier-failure/Stop as first-class injected conditions).
4. Define **outcome categories explicitly** (exactly-once / at-least-once / at-
   most-once-when-unsafe / forbidden / unknown-outcome-vs-hallucinated-success),
   contrasting which WebArena/OSWorld can detect.
5. Sharpenest differentiator: **"unknown-outcome after interruption"** — no
   adjacent benchmark requires declaring uncertainty and reconciling with ground
   truth post-interruption.
6. Stay honest: AgentDojo/AgentHazard already cover *unauthorized* side effects;
   our *new* contribution is the exactly-once + ambiguity + DB-oracle coupling.
