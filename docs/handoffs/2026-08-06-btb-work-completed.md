# BrowserTransactionBench Work Handoff — Completed and Current State

> **SUPERSEDED AT 2026-08-06T11:18:28Z:** This file preserves the earlier
> mid-implementation checkpoint. Phase 2 has since reached a locally committed
> candidate. Use
> `docs/handoffs/2026-08-06-btb-phase2-candidate.md` for current state. Do not
> use the unfinished-work or 66-test statements below as present status.

**Captured:** 2026-08-06T00:32:38Z
**Repository:** `/Users/raghav/browserbench`
**Branch:** `main`
**HEAD:** `ca26e9e2b9b789b0c830d0c899b04d93a8c71ab1`
**Commit status:** All integrity-rebuild work is uncommitted. Do not reset or clean the working tree.

## Executive status

Phase 1 is implemented and was fully exercised. Phase 2.1 is partially implemented: the schema-v2 receipt core, validator, and late engine integration are present, but CLI integration, verifier migration, manifest tests, and the clean-code review are unfinished. The newest Phase 2.1 engine/manifest state has not yet been accepted as green.

No implementation subagent or background process is still active. This is a safe stopping point.

## Completed: Phase 1 scientific-validity rebuild

### 1. Managed per-run fixture isolation

Implemented:

- `btb/app/server.py`
  - explicit `create_app(database_path, run_id=...)` app factory;
  - `/health` reports canonical DB path and run ID;
  - module-level app remains available for normal ASGI invocation.
- `btb/harness/runtime.py`
  - unique temporary run directory and SQLite DB;
  - pre-bound localhost socket;
  - dedicated in-process Uvicorn server/thread;
  - exact health/identity verification;
  - deterministic teardown on normal and exceptional exits.
- `run_pilot.py`
  - managed fixture is the default execution path;
  - explicit external-server mode fails closed on DB identity mismatch.
- `tests/test_runtime.py`, `tests/test_server.py`
  - concurrent environments use distinct URLs and DBs;
  - reset/write isolation is tested;
  - normal/exception cleanup is tested.

This removes the shared-DB contamination that invalidated the original acting-agent and DeepSeek receipts.

### 2. Verified post-commit connection-loss injection

Implemented in `btb/harness/inject.py` and `tests/test_inject.py`:

- proxy state is instance-local rather than module-global;
- each send attempt records sequence, body hash, parsed draft/UID, actual upstream status, classification, committed identity, timestamps, and downstream treatment;
- connection loss is injected only after a successful upstream JSON response proves a durable commit;
- the configured `after_nth_attempt` is honored and treatment is delivered at most once;
- later retries receive their real response;
- upstream rejection/network failure is not mislabeled as treatment delivery;
- request-quiescence barrier runs before scoring;
- client-abandoned response delivery is recorded as `downstream_disconnected_before_response` rather than raising an unhandled `BrokenPipeError` or pretending the response was delivered;
- two proxies cannot share targets, counters, or events.

Real managed-browser calibration was exercised:

- exact control: one durable message;
- naive retry: two durable messages;
- first committed acknowledgement dropped;
- second naive retry response forwarded normally.

Reviewer-generated smoke receipts were removed after inspection so they cannot be mistaken for canonical evidence.

### 3. Full-state task evaluator and strict claims

Implemented:

- `btb/app/db.py`
  - `DraftNotSavedError` and saved-before-send enforcement;
  - narrow same-UID collision handling;
  - atomic `full_snapshot()` across users, drafts, messages, and send attempts.
- `btb/oracle/claim.py`
  - strict canonical final JSON;
  - `sent`, `not_sent`, and `unknown` remain distinct;
  - malformed and absent output are distinct;
  - last valid structured claim wins;
  - legacy heuristic requires explicit opt-in;
  - full raw final answer and parser version are retained.
- `btb/oracle/score.py`
  - evaluates complete before/after state against the complete task contract;
  - separate axes for functional status, effect state, authorization violations, duplicate attempts, belief, treatment delivery, and headline outcome;
  - exact read report, save content, source-message content, authorized draft identity, forbidden effects, and duplicate attempts are checked.
- task JSON
  - exact expected report/content added;
  - neutral task no longer contains an ambiguity cue;
  - send tasks bind subject/body/draft identity and `after_nth_attempt`.
- `btb/baselines/play.py`, `btb/harness/engine.py`
  - deterministic controls emit auditable claim dictionaries;
  - Browser Use final output uses the installed synchronous `AgentHistoryList.final_result()` API when available;
  - learned-agent prompt requests strict final JSON.

Regression tests now reject all review-discovered false positives:

- read creates an unsaved draft;
- read mutates content without changing status;
- read reports wrong content;
- save uses wrong subject/body;
- save creates an extra draft;
- unsaved send through DB or HTTP;
- send mutates draft state;
- unauthorized draft send;
- message content diverges from its saved source;
- same-UID duplicate attempt is hidden behind one committed row;
- unknown is collapsed into not-sent;
- malformed and absent claims are conflated.

## Last accepted verification for Phase 1

Before the late Phase 2.1 engine integration landed, the canonical local gate returned:

- `pytest -q`: **66 passed**;
- Ruff: passed;
- `git diff --check`: passed;
- one third-party Starlette/FastAPI TestClient deprecation warning only.

Important: these results establish Phase 1. They must not be cited as verification of the newest partial Phase 2.1 integration. Rerun all gates after completing Phase 2.1.

## Current partial work: Phase 2.1

### Implemented but not yet accepted

- `schemas/manifest-v2.schema.json`
  - schema version `2.0`;
  - success/failure status, canonical flag, timestamps, source, task/prompt hashes, baseline policy, execution/failure, full claim, injection, before/after snapshots, evaluation, outcome, and component versions.
- `btb/harness/manifest.py`
  - source-tree hashing over benchmark source/task/template/schema/protocol inputs;
  - Git commit/dirty provenance;
  - canonical-source refusal;
  - `BaselineProvenance` and `ReceiptBuilder`;
  - exploratory/canonical output directories;
  - atomic temp-file + `os.replace` writes;
  - success and failure receipt construction.
- `btb/harness/validate_manifest.py`
  - JSON Schema validation;
  - independent invariant reconstruction without importing `btb.oracle.score`;
  - checks hashes, success/failure evidence, effect cardinality, DB attempt/message identity, duplicate count, proxy commit evidence, treatment delivery, configured Nth treatment, quiescence, and canonical cleanliness.
- `btb/harness/engine.py`
  - late partial integration now constructs provenance and receipt builders;
  - records before/after/evaluation/claim/injection evidence;
  - attempts failure receipts and cleanup evidence;
  - returns success/failure result objects.

### Incomplete or unverified

1. `run_pilot.py` is still the old CLI shape.
   - no `--mode exploratory|canonical`;
   - no receipt output-directory option;
   - does not pass `ReceiptOptions` or a prebuilt receipt builder;
   - managed-runtime setup failure occurs outside the engine and is therefore not yet guaranteed a receipt;
   - `_summarize()` assumes success and can mishandle a failure result;
   - top-level docstring still claims old manifest location.
2. `.verify/pilot_verifier.py` is still legacy code.
   - imports removed APIs such as `score_outcome()` and `scoring_context_with_initial()`;
   - scans legacy root manifests;
   - validates only a hard-coded outcome set;
   - has not been migrated to `validate_manifest.py`.
3. No Phase 2.1 tests exist yet.
   - `tests/test_manifest.py` absent;
   - `tests/test_manifest_validation.py` absent;
   - no engine failure-receipt tests;
   - no CLI canonical/exploratory behavior tests.
4. The current source has not been rerun through pytest after the late engine receipt integration.
5. `validate_manifest.py` contains a large fallback implementation of part of JSON Schema. `jsonschema 4.26.0` is installed locally; Phase 2.2 should declare it and the fallback should be removed during the clean-code pass.
6. Clean-code/catalog review requested by the user is not complete for the current diff.
7. The schema currently lives at repository-root `schemas/`; packaging must decide whether to move it under `btb/` or explicitly include it as installed data.
8. Existing root manifests remain legacy/exploratory and have not yet been classified or moved.

## Current working-tree inventory

Modified tracked source/tests:

- `btb/app/db.py`
- `btb/app/server.py`
- `btb/baselines/play.py`
- `btb/harness/engine.py`
- `btb/harness/inject.py`
- `btb/harness/manifest.py`
- `btb/oracle/claim.py`
- `btb/oracle/score.py`
- all four task definition JSON files
- `run_pilot.py`
- `tests/test_claim.py`
- `tests/test_oracle.py`
- `tests/test_scoring.py`
- `tests/test_server.py`
- `tests/test_tasks.py`

New/untracked implementation or documentation:

- `btb/harness/runtime.py`
- `btb/harness/validate_manifest.py`
- `schemas/manifest-v2.schema.json`
- `tests/test_inject.py`
- `tests/test_runtime.py`
- `docs/plans/2026-08-06-btb-integrity-rebuild.md`
- this handoff and its companion roadmap under `docs/handoffs/`

Latest `git diff --check` passed. Nothing has been committed or pushed.

## Safe resume command sequence

From `/Users/raghav/browserbench`:

```bash
git status --short --branch
git diff --check
pytest -q
ruff check btb tests run_pilot.py .verify
```

Expect pytest or the verifier to expose unfinished Phase 2.1 integration. Do not weaken tests or restore removed Boolean scoring APIs; finish migration to the v2 contract instead.

## Non-negotiable cautions

- Do not delete, reset, stash, or clean the current working tree.
- Do not treat existing manifests as canonical evidence.
- Do not call the original acting agent GPT-5.6; its model identity was not receipted.
- Do not run canonical Phase 3 evidence until source is clean and committed; committing requires explicit user authorization.
- Keep implementation delegated to the configured GPT-5.6-class subagent when practical, with the parent agent doing specification review, clean-code inspection, final fixes, and verification.
