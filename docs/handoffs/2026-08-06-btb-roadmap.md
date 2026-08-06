# BrowserTransactionBench Implementation Roadmap Handoff

**Repository:** `/Users/raghav/browserbench`
**Primary detailed plan:** `docs/plans/2026-08-06-btb-integrity-rebuild.md`
**Current-state handoff:** `docs/handoffs/2026-08-06-btb-phase2-candidate.md`

## 2026-08-06 Phase 2 checkpoint

Phase 2 is now a locally committed candidate. The integrity slice is `cd46fad`,
the packaging/CI slice is `36cc2e5`, and the documentation slice is the commit
containing this roadmap. The precise current
evidence and remaining claim boundaries are in
`docs/handoffs/2026-08-06-btb-phase2-candidate.md`. The detailed Phase 2 steps
below are retained as an audit trail, but their earlier "in progress" wording
is superseded by that checkpoint.

The requested catalog-backed `code-smell-review` skill was not available in
the current skill set. A manual mixed structural, error-path, security-boundary,
and maintainability review was completed instead. A fresh-context,
veto-capable reviewer and any formal catalog report remain required before a
release or publication claim.

## Goal

Turn the exploratory BrowserTransactionBench pilot into a run-isolated, auditable, clean-installable benchmark that can produce modest but defensible repeatability evidence without overstating model comparisons.

The benchmark must keep three questions separate:

1. What durable state actually changed?
2. What did the agent report or believe happened?
3. Was the post-commit connection-loss treatment actually delivered?

A single headline label may summarize those axes, but it must never replace them.

## Working method

- Keep one writer for shared runtime, scorer, schema, and validator surfaces.
- Use a fresh-context, veto-capable reviewer before any publication claim or release.
- The user requested the catalog-backed `code-smell-review` workflow, but that
  skill/catalog was unavailable in this session. The manual review covered the
  exact `main` diff, structural duplication, error paths, capability/security
  boundaries, documentation truth, and full re-verification. If the catalog is
  later made available, run its exact report before release; do not fabricate
  catalog IDs or claim that workflow already ran.
- Do not push, reset, clean, or rewrite history without explicit user authorization.
- Canonical benchmark runs require a clean committed source tree and the exact-tip gate plus fresh veto review.

## Immediate resume: finish Phase 2.1

Current status: locally committed candidate; exact-tip gate and external review pending.

### Step 1 — Re-establish the current gate

Run:

```bash
cd /Users/raghav/browserbench
git status --short --branch
git diff --check
pytest -q
ruff check btb tests run_pilot.py .verify
```

Do not assume the historical 66-pass result validates the newest late-arriving `engine.py` integration.

### Step 2 — Complete CLI/engine receipt ownership

Target files:

- `run_pilot.py`
- `btb/harness/engine.py`
- `btb/harness/manifest.py`

Required end state:

1. Add an explicit CLI mode:
   - `--mode exploratory` by default;
   - `--mode canonical` fails closed unless Git commit exists and source is clean.
2. Add an optional receipt output directory for tests and controlled tooling.
3. Construct receipt provenance before managed fixture startup so managed-runtime setup failures can still emit `setup_error` receipts.
4. Pass one `ReceiptBuilder` through the execution path; do not let CLI and engine create competing builders.
5. Write exactly one final receipt per run ID.
6. Ensure failures return/print failure summaries rather than success-shaped summaries.
7. Return a nonzero CLI status when any requested run fails.
8. Preserve `KeyboardInterrupt` and `SystemExit`; do not catch `BaseException` in orchestration logic.
9. Record the exact prompt actually executed:
   - learned agent: complete generated URL/instruction/final-answer contract;
   - deterministic control: a clearly identified deterministic-control instruction/procedure, not a fabricated LLM prompt.
10. Enforce configured wall budgets or clearly defer that to Phase 3 without claiming they were enforced.

Review the late engine integration carefully. Current potential risks include:

- success receipt may be written before proxy cleanup completes and then replaced on cleanup failure;
- failure-receipt writing can itself fail and hide the primary exception;
- setup outside the engine currently has no receipt;
- duplicated Playwright/Browser Use orchestration suggests a shared run-finalization boundary may be cleaner;
- `configured_steps or budget_steps` treats `0` as absent instead of invalid;
- broad evidence-capture exceptions are intentional only if every secondary error is retained in `evidence_failures`.

### Step 3 — Simplify and harden manifest validation

Target files:

- `btb/harness/validate_manifest.py`
- `schemas/manifest-v2.schema.json`
- later `pyproject.toml`

Required invariants:

- embedded canonical task hash matches the task definition;
- exact prompt hash matches exact prompt text;
- success requires before/after/evaluation/claim/injection and no execution failure;
- failure requires exception type/message/stage and no fabricated headline/evaluation;
- top-level outcome equals evaluation headline;
- effect state equals the count of new message IDs;
- committed DB attempts and new messages correspond by draft/send UID;
- duplicate-attempt count equals new `duplicate_rejected` rows;
- every proxy event classified committed identifies a matching after-state message;
- every new proxied send effect has complete committed-event evidence;
- treatment delivery is reconstructed from event treatment, and only the configured Nth committed event may be dropped;
- proxy `in_flight` is zero;
- one successful proxied event cannot explain two new message effects;
- canonical receipt requires clean source and a non-null exact commit.

Clean-code action:

- `jsonschema 4.26.0` is installed locally.
- Declare it as a runtime dependency in Phase 2.2.
- Remove the hand-written partial JSON Schema fallback rather than maintaining a second validator implementation.
- Keep invariant reconstruction independent from `btb.oracle.score`.

Packaging decision:

- Prefer moving the schema under a package-owned path such as `btb/schemas/manifest-v2.schema.json`, or prove repository-root `schemas/` is installed and discoverable through package data.
- The source-tree hash and validator default path must follow the chosen location consistently.

### Step 4 — Add missing Phase 2.1 tests

Create:

- `tests/test_manifest.py`
- `tests/test_manifest_validation.py`
- focused engine/CLI receipt tests where appropriate.

Tests must construct valid receipts in temporary directories, then mutate one invariant at a time. At minimum reject:

- task hash tampering;
- prompt hash tampering;
- success missing snapshots;
- headline mismatch;
- effect-count mismatch;
- duplicate-count mismatch;
- fake treatment delivery;
- nonzero proxy `in_flight`;
- one committed proxy event paired with two new effects;
- dirty canonical receipt;
- canonical receipt with no commit;
- success containing execution failure;
- failure containing evaluation/headline;
- CLI success summary for a failed run.

Also test:

- atomic receipt replacement leaves no temp files;
- exploratory output goes only under `manifests/exploratory/current/`;
- canonical output goes only under `manifests/canonical/`;
- canonical mode refuses the current dirty tree;
- managed setup, baseline, timeout, and evaluation errors each produce a receipt.

### Step 5 — Rewrite the independent verifier

Rewrite `.verify/pilot_verifier.py` so it:

- no longer imports removed Boolean-era scorer APIs;
- retains useful DB/application smoke checks using current APIs;
- validates every schema-v2 receipt under `manifests/canonical/` using `btb.harness.validate_manifest`;
- ignores legacy root manifests for canonical aggregation;
- fails on no canonical receipts only when explicitly invoked with a canonical-required flag;
- returns nonzero for any schema or invariant failure;
- does not accept a receipt merely because its headline is in a known string set.

Follow the repository’s verifier discipline:

1. lint;
2. smoke;
3. pytest with `-p no:cacheprovider` where used by the verifier workflow;
4. smoke again;
5. custom verifier.

The second smoke is required because pytest may recreate a cache directory even when the cache file provider is disabled.

### Step 6 — Perform the requested clean-code review

Classification will likely be `mixed`; primary lens should be `Mixed`.

Use the exact report structure from the invoked skill and cite one catalog ID per finding. High-probability areas to inspect, without prejudging them as findings:

- large fallback schema interpreter: `DS.NEEDLESS-COMPLEXITY` or `DS.NEEDLESS-REPETITION`;
- duplicated run orchestration in `run_playwright()` and `run_browser_use()`: `GOF.TEMPLATE-MISSING` or `CC.G5`;
- large mutable `ReceiptBuilder` responsibility surface: `CC.G30` or `CC.G8`;
- broad caught exceptions in evidence collection: `PY.BROAD-EXCEPT` only if they can hide a primary correctness failure rather than being fully receipted;
- long condition-heavy evaluator functions: inspect for `CC.G30`, `CC.G19`, or `DS.OPACITY`, but report only concrete costs;
- stale docs/verifier references to removed APIs: `CC.C2` or `CC.G11`.

Fix accepted findings before final Phase 2 verification.

### Step 7 — Phase 2.1 acceptance gate

Required:

```bash
ruff check btb tests run_pilot.py .verify
pytest -q
git diff --check
python -m btb.harness.validate_manifest <temporary-valid-receipt>
python .verify/pilot_verifier.py
```

Also run at least one real exploratory managed-browser exact control and one naive injected control, validate their v2 receipts independently, inspect the receipt contents, and remove only those reviewer-generated exploratory artifacts afterward.

## Phase 2.2 — Packaging, assets, dependencies, CI

Current status: local build and clean-install acceptance passed; CI is locally
committed but has not run because no revision was pushed.

### Packaging

Update `pyproject.toml` with:

- a real `[build-system]`;
- runtime dependencies used by the CLI/harness;
- test/dev optional dependencies;
- console scripts for runner and manifest validator where useful;
- package discovery/data configuration;
- Python version compatibility matching actual syntax and tested CI versions.

At minimum assess/decorate dependencies for:

- FastAPI/Starlette-compatible test stack;
- Uvicorn;
- Playwright;
- Browser Use;
- HTTPX;
- JSON Schema validation.

Ensure installation includes:

- HTML fixture templates;
- task definition JSON;
- manifest schema;
- any verifier/runtime assets needed by installed CLI execution.

### License and reproducibility

- Add the actual `LICENSE` file promised by project metadata.
- Add a reproducible lock/constraints strategy rather than leaving only loose lower bounds.
- Add CI for supported Python versions with lint, tests, package build, clean-venv install, installed smoke, and manifest validation.
- Add a clean-install test that executes outside the repository working directory so hidden source-tree imports cannot mask missing package data.

### Phase 2.2 acceptance

- build wheel and sdist;
- create a clean temporary virtual environment;
- install the wheel;
- run installed CLI task listing;
- load task JSON and HTML template through installed package paths;
- validate a receipt using the installed schema;
- run tests and CI-equivalent gates.

## Phase 2.3 — Freeze consistency and artifact hygiene

Current status: local consistency review complete. New contracts use `0.1.0`;
earlier identifiers and receipts are explicitly invalidated legacy evidence.

### Freeze/version consistency

Choose one release/freeze identifier and use it consistently across:

- package version;
- task JSON `freeze`;
- manifest `release` and `freeze`;
- README;
- protocol;
- results and limitations documents;
- artifact directory naming.

Do not leave `0.0.1`, `0.0.2`, and `0.1.0` mixed without an explicit historical explanation.

### Existing artifact classification

- Keep old root manifests out of canonical aggregation.
- Add an invalidation/provenance note stating that pre-rebuild acting-agent and DeepSeek receipts used shared state and incomplete receipts.
- Do not silently rewrite legacy receipts to schema v2.
- Move or index them only as explicitly exploratory/legacy evidence.
- Document that the old acting agent’s model identity is unknown.

### Documentation corrections

Update README/protocol/results/limitations so they no longer claim:

- runs are canonical merely because a JSON file exists;
- full prompts and transcripts are preserved when they are not;
- clean install works before it has been tested;
- unsupported quantitative conclusions about Browser Use or model capability.

Add exact rerun/validation commands and explain the independent validator.

## Phase 2 final review

Before Phase 3:

1. clean-code/catalog review fixed and rerun;
2. full test suite green;
3. Ruff green;
4. package build green;
5. clean-wheel install green;
6. installed asset smoke green;
7. verifier green;
8. docs/freeze consistency reviewed;
9. no exploratory artifact can enter canonical aggregation;
10. no secrets, API keys, environment dumps, or cookies in receipts.

The verified Phase 1/2 work is now locally committed. Rerun the exact-tip gate
and obtain fresh veto-capable review before any canonical Phase 3 receipts.

## Phase 3.1 — Repetition runner and summary tooling

Current status: pending.

Implement a repetition runner that:

- builds a balanced task × condition matrix;
- randomizes execution order from a recorded seed;
- records provider/model/framework/configuration;
- enforces wall-clock and step budgets;
- preserves one schema-v2 receipt per run;
- records failures/timeouts rather than dropping them;
- supports resume without duplicating completed run IDs;
- validates every receipt before aggregation;
- writes CSV and Markdown summaries derived only from validated canonical receipts.

Report per cell:

- denominator;
- pass count/rate;
- effect-state counts;
- duplicate-attempt rate;
- authorization-violation rate;
- belief distribution;
- treatment-delivery count;
- Wilson interval or another declared binomial interval;
- failure/timeout count;
- provenance hash set.

Do not aggregate unmatched source hashes, freezes, prompts, or baseline policies into one cell.

## Phase 3.2 — Deterministic control calibration

Run on one clean committed source tree:

- exact deterministic control;
- naive-retry deterministic control;
- send ambiguity-cue task;
- neutral send task;
- read task;
- save task.

Target at least 10 repetitions per relevant cell, randomized by the Phase 3.1 runner.

Assertions:

- exact injected send always produces one effect;
- naive injected send always produces multiple effects;
- treatment is delivered exactly once in injected send cells;
- control outcomes are stable;
- read/save controls satisfy complete contracts;
- independent validator accepts every receipt;
- no run shares a database or proxy state.

Any deterministic-control instability is a harness defect, not model variance. Stop and fix it before learned-agent runs.

## Phase 3.3 — Matched learned-agent pilot

Only after deterministic calibration passes:

- choose the explicitly configured provider/model;
- record exact installed Browser Use version, provider, model, parameters, prompt, capability policy, and source commit/hash;
- use the same task definitions, injection semantics, budgets, and validator as controls;
- start with a small matched matrix before spending on a larger pilot;
- retain all failed and timed-out runs in denominators;
- label results as exploratory unless the full canonical protocol is satisfied.

Do not compare old DeepSeek runs to new runs as if matched. Do not infer the old acting-agent model identity.

## Final integration review

After all phases:

- rerun the complete clean-code smell review;
- perform structural/error-path review;
- audit receipt contents for secrets and omissions;
- compare every documented claim to executable behavior;
- rerun full local and CI-equivalent gates;
- verify canonical artifacts from a clean checkout/install;
- generate the final limitations-aware report from validated receipts only.

## Phase 4 — Publication study

Current status: pending; blocked on Phase 3 deterministic calibration and one
clean committed artifact.

Phase 4 converts the narrow message pilot into a publishable benchmark study.
It is a new study, not a retrospective relabeling of Phase 1–3 artifacts.

### Phase 4.1 — Several synthetic transaction domains

Implement independently authoritative fixtures for several domains, selected
before data collection and sized by the statistical plan rather than an
arbitrary task-count target. Candidate domains:

- checkout/order placement;
- calendar booking;
- file upload/publish;
- account setting changes;
- synthetic financial or inventory mutations.

Each domain needs its own complete state model, authorization contract,
idempotency identity, reconciliation surface, fault hook, task family, and
independent-validator rules. No fixture may make a real external transaction.

### Phase 4.2 — Randomized fault timing and no-fault controls

Use a preregistered seeded randomization schedule with matched no-fault controls.
Fault families should include:

- failure before commit;
- dropped or delayed acknowledgement after commit;
- client timeout;
- 5xx before and after commit;
- stale read after commit;
- process restart/recovery;
- concurrent duplicate requests.

The treatment assignment and timing parameters must be concealed from the
learned agent, recorded in the receipt, and independently reconstructed. Report
treatment-delivery failures separately; never analyze assignment as delivery.

### Phase 4.3 — Idempotency and reconciliation affordance ablations

Build a factorial, matched set of product affordances:

- stable versus fresh operation IDs;
- server-side idempotency on versus off;
- visible transaction history on versus off;
- explicit reconciliation endpoint/tool on versus off;
- ambiguity cue versus neutral instruction;
- DOM-only, vision-enabled, and explicitly API-assisted capability policies.

Change only the declared factor inside a matched comparison. Receipt hashes and
validator rules must make unintended fixture/prompt differences detectable.

### Phase 4.4 — Matched learned-agent conditions

Run multiple explicitly selected learned-agent configurations against the same
randomized task/treatment matrix and budgets. Record exact framework, provider,
model, generation settings, capability policy, prompt, source, and environment
fingerprint. Preserve errors and timeouts in denominators. Do not compare legacy
runs or unmatched framework/model versions.

Primary outcomes need counter-metrics: exactly-once completion alongside missed
effects, duplicate effects, forbidden effects, reconciliation attempts,
latency/cost, and belief calibration. Include a deterministic control and at
least one external reality check for every fixture/fault family.

### Phase 4.5 — Preregistration and analysis

Before canonical learned-agent collection, freeze and preregister:

- hypotheses and primary/secondary outcomes;
- unit of analysis and planned contrasts;
- sample-size/power or precision target;
- randomization scheme and seed handling;
- stopping, exclusion, retry, and missing-data rules;
- multiplicity handling and interval estimators;
- aggregation compatibility keys;
- planned robustness and ablation analyses.

Generate tables only from independently validated canonical receipts. Publish
cell denominators, failures/timeouts, effect-state counts, calibration, interval
estimates, source/provenance sets, and all declared deviations.

### Phase 4.6 — Independent clean-install reproduction

A fresh reviewer with veto authority must reproduce the study from the released
wheel/source archive and checksums in a new checkout or machine. Required proof:

- clean install and packaged-asset smoke;
- deterministic calibration reproduced first;
- canonical receipt index and artifact checksums verified;
- a preregistered subset of every domain/fault/affordance family rerun;
- independent validator and analysis pipeline produce matching classifications;
- discrepancies resolved or published as limitations before release.

Publication release artifacts should include task/schema migration policy,
signed or checksum-bound artifact index, environment/installation guide,
analysis code, preregistration, exclusions/deviations, and a limitations-aware
paper/report. Mature external tooling may replace custom code only when the
native auditable receipt and authority boundaries remain intact.

## Current task-state snapshot

- Phase 1.1 managed isolation: completed
- Phase 1.2 verified injector: completed
- Phase 1.3 full-state oracle/claims: completed
- Phase 1 review: completed
- Phase 2.1 manifest v2/validator: local acceptance passed; committed as `cd46fad`
- Phase 2.2 packaging/CI: clean wheel acceptance passed; hosted CI pending
- Phase 2.3 docs/freeze/artifacts: local consistency review passed
- Phase 2 review: manual review complete; fresh veto review pending
- Phase 3.1 repetition runner: pending
- Phase 3.2 deterministic controls: pending
- Phase 3.3 learned-agent pilot: pending
- Phase 4 publication study: pending, blocked on Phase 3
- Final review and gates: pending
