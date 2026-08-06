# BrowserTransactionBench Integrity Rebuild Implementation Plan

> **For Hermes:** Use subagent-driven-development task-by-task. Implementers edit and test; the parent GPT-5.6-sol agent performs spec review, quality review, error-path review, and final fixes.

**Goal:** Convert the current exploratory pilot into a run-isolated, auditable, clean-installable benchmark and produce a small canonical evidence set under one immutable release.

**Architecture:** Every canonical run owns a unique app process, SQLite database, proxy, and receipt directory. The injector records each upstream attempt and only injects a real connection failure after a verified durable commit. Evaluation preserves effect truth, agent belief, treatment delivery, and functional correctness as separate axes and evaluates full before/after state against the task contract. Versioned manifests bind source, task, dependencies, capability policy, and raw evidence.

**Tech stack:** Python 3.10+, SQLite, FastAPI/Uvicorn, HTTPX, Playwright, Browser Use, Pydantic/JSON Schema, pytest, Ruff.

---

## Phase 1 — Scientific validity

### Task 1.1: Managed, isolated run environment

**Objective:** Eliminate shared DB/server contamination and DB/server path mismatch.

**Files:**
- Create `btb/harness/runtime.py`
- Modify `btb/app/server.py`
- Modify `btb/harness/engine.py`
- Modify `run_pilot.py`
- Add `tests/test_runtime.py`

**Requirements:**
1. Add an app factory accepting an explicit database path.
2. Add a managed fixture context that creates a unique run directory and DB, starts a dedicated app process/server on a unique port, verifies `/health` returns the expected canonical DB path/run ID, and stops it reliably.
3. Canonical CLI runs use the managed fixture by default. External-server mode, if retained, must verify DB identity and fail closed on mismatch.
4. A run must not reset or score any DB owned by another run.
5. Close server/process resources in every exception path.
6. Add tests proving two concurrent environments use different DBs and cannot observe/reset each other.

**TDD gate:** targeted runtime tests RED then GREEN; full pytest and Ruff.

### Task 1.2: Verified post-commit connection-loss injector

**Objective:** Ensure the treatment is actually a verified commit followed by missing acknowledgement.

**Files:**
- Rewrite `btb/harness/inject.py`
- Modify `btb/harness/engine.py`
- Modify `btb/harness/manifest.py`
- Add `tests/test_inject.py`

**Requirements:**
1. Remove module-global mutable proxy state; all state is instance-local.
2. Log a per-attempt record: sequence, method/path, request-body hash, upstream status, upstream response classification, committed message ID/UID when present, start/upstream-complete/drop timestamps, and client treatment.
3. Inject only when the upstream response proves a durable commit. Forward upstream errors/rejections normally and mark treatment not delivered.
4. After verified commit, close/reset the downstream connection without sending an HTTP response.
5. Record actual upstream status; never hard-code 200.
6. Wait for request quiescence before scoring; validate proxy attempts against DB attempt rows.
7. Add real HTTP tests for no fault, verified commit-then-drop, rejected/malformed send, and multiple independent proxies.

**TDD gate:** injection tests RED then GREEN; full pytest and Ruff.

### Task 1.3: Full-state oracle and tri-state claim model

**Objective:** Enforce the task contract and preserve uncertainty.

**Files:**
- Modify `btb/app/db.py`
- Rewrite `btb/oracle/score.py`
- Modify `btb/oracle/claim.py`
- Modify `btb/harness/engine.py`
- Modify all task JSON definitions as needed
- Expand `tests/test_oracle.py`, `tests/test_scoring.py`, `tests/test_claim.py`, `tests/test_tasks.py`

**Requirements:**
1. Enforce saved-before-send in the application.
2. Capture one atomic full before/after snapshot including drafts, messages, and attempts with relevant fields.
3. Evaluate `allow_effects`, `forbid_effects`, `intended_final`, and `forbidden_final`; do not use decorative fields.
4. Detect wrong read output, any read-time mutation, wrong save content, extra drafts, send-time draft mutation, unauthorized send, duplicate effects, and rejected duplicate attempts.
5. Preserve belief as `sent | not_sent | unknown | malformed | absent`; never reduce unknown to false.
6. Report separate axes: functional result, effect count/state, authorization violations, duplicate attempts, belief, and treatment delivery. A convenience headline label may be derived but must not erase axes.
7. Require a strict structured final answer from learned agents; preserve the exact final answer and parser version.

**TDD gate:** each previously reproduced false-positive receives a regression test; full pytest and Ruff.

---

## Phase 2 — Reproducibility and release integrity

### Task 2.1: Versioned receipts and independent validator

**Files:**
- Add `schemas/manifest-v2.schema.json`
- Rewrite `btb/harness/manifest.py`
- Add `btb/harness/validate_manifest.py`
- Rewrite `.verify/pilot_verifier.py`
- Add manifest/validator tests

**Requirements:**
1. Record source commit, dirty flag/tree hash, complete task and hash, prompt hash, provider/model/framework versions, capability policy, parameters, full final answer/transcript path or digest, timing, failures, injection events, before/after snapshots, and evaluation axes.
2. Fail canonical runs on dirty source unless explicitly marked exploratory.
3. Validate with versioned schema plus independent invariants; reject impossible `one successful forward -> two effects` receipts.
4. Always emit a receipt for timeout/crash/setup failure.

### Task 2.2: Packaging, dependencies, assets, CI

**Files:**
- Modify `pyproject.toml`
- Add `LICENSE`
- Add `.github/workflows/ci.yml`
- Add package-data configuration and clean-wheel smoke tests
- Add or generate a lockfile if project tooling supports it

**Requirements:**
1. `pip install .` installs core runtime dependencies.
2. Wheel includes HTML templates, task JSON, and schemas.
3. Dev extras include test/lint/build tools.
4. CI runs Ruff, pytest, verifier, wheel build, install-from-wheel smoke, and `git diff --check` equivalent.

### Task 2.3: Freeze and artifact hygiene

**Files:**
- Update `PROTOCOL.md`, `README.md`, results docs, verifier docs
- Add `manifests/README.md`
- Move or clearly classify v1 receipts as exploratory/invalidated

**Requirements:**
1. One release identifier is consistent everywhere.
2. Existing contaminated/legacy receipts remain auditable but are excluded from canonical aggregation.
3. Retract unsupported GPT-5.6, model-comparison, perfect-verifier, and central-demonstration claims.
4. Document exact canonical commands, capability policy, limitations, and external model-network requirement.

---

## Phase 3 — Canonical evidence

### Task 3.1: Repetition/randomization and summary tooling

**Files:**
- Extend `run_pilot.py`
- Add `btb/results/summarize.py` and tests

**Requirements:**
1. Support seeded randomized fault/no-fault assignment and predeclared repetitions.
2. Enforce steps and wall-clock budgets.
3. Report reachability, treatment delivery, effect multiplicity, authorization, belief calibration, malformed claims, failures/timeouts, and Wilson binomial intervals.
4. Aggregate only canonical manifest-v2 receipts from one release.

### Task 3.2: Run clean calibration matrix

**Requirements:**
1. Commit or otherwise freeze clean source before canonical runs; if commits are not authorized, runs are marked exploratory and no paper claims are made.
2. Run deterministic exact and naive controls in matched no-fault and verified post-commit-loss conditions, with at least 10 repetitions per cell.
3. Verify every receipt independently and generate the summary table.

### Task 3.3: Run available learned-agent matrix

**Requirements:**
1. Use Browser Use only, identical capability policy and structured claim contract for every model.
2. Run matched no-fault/injected cells with a predeclared small pilot N using an already configured provider; do not expose credentials.
3. If no suitable model can reach the task, report that as the result rather than substituting an external Hermes subagent.
4. Do not compare models unless framework, modality, permissions, prompts, budgets, and treatment are matched.

---

## Final gates

1. Ruff.
2. Targeted tests.
3. Full pytest.
4. Verifier over all canonical receipts.
5. Wheel build and install-from-wheel smoke.
6. Integration smoke for managed server + proxy + Playwright.
7. Clean Code smell review.
8. Structural design review.
9. Error-path audit.
10. `git status`, `git diff --check`, and exact changed-file inventory.

No commit, push, or merge unless separately authorized.
