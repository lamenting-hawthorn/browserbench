# BrowserTransactionBench

**An evidence-first benchmark for transactional safety in browser agents.**

[![CI](https://github.com/lamenting-hawthorn/browserbench/actions/workflows/ci.yml/badge.svg)](https://github.com/lamenting-hawthorn/browserbench/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Research status: exploratory](https://img.shields.io/badge/research-exploratory-orange.svg)](#current-status)

[Research questions](#research-questions) ·
[Methodology](#how-it-works) ·
[Status](docs/STATUS.md) ·
[Protocol](PROTOCOL.md) ·
[Roadmap](docs/handoffs/2026-08-06-btb-roadmap.md) ·
[Contributing](CONTRIBUTING.md)

BrowserTransactionBench (BTB) is a clean-room research harness for testing
transactional safety in browser agents: whether an authorized durable effect
happens exactly once, whether forbidden effects are avoided, and whether the
agent's stated belief is calibrated after an acknowledgement is lost.

The SQLite fixture is authoritative. Screenshots, framework success flags, and
agent prose never determine effect truth.

> A browser agent can say "done" while the real transaction failed, happened
> twice, or completed without its acknowledgement reaching the agent. BTB
> measures the durable effect separately from the agent's belief.

## Why this project exists

Most browser-agent evaluations ask whether the final screen looks correct or
whether a task was reported as successful. That is not enough for transactions.
For sends, bookings, purchases, updates, and other durable actions, a useful
evaluation must also answer:

- Did the intended effect happen exactly once?
- Did any duplicate, forbidden, or unauthorized effect happen?
- Was an ambiguous outcome genuinely injected after commit?
- Did the agent reconcile through the visible product before retrying?
- Did the agent report what it knew—and what it could not know—accurately?

BTB makes those questions independently auditable with a synthetic application,
an authoritative database oracle, controlled post-commit failures, and
schema-validated run receipts.

## Research questions

The exploratory benchmark is organized around three questions:

- **RQ1 — Effect integrity:** Does the authorized durable effect occur exactly
  once, with no duplicate, forbidden, or unauthorized state transition?
- **RQ2 — Calibrated uncertainty:** When an acknowledgement is lost after a
  possible commit, does the agent distinguish known success, known failure, and
  an unresolved outcome?
- **RQ3 — Reconciliation behavior:** Does the agent use visible product state to
  reconcile before retrying, and is its final belief consistent with the
  authoritative application state?

## Research artifacts

The project is being developed as four connected artifacts:

1. a versioned browser-task benchmark with authoritative application/database
   oracles and controlled failure injection;
2. deterministic and learned-agent baselines under explicit capability
   policies;
3. a model-agnostic transactional-safety reference layer based on reconciliation
   and independently validated receipts; and
4. a preregistered, multi-domain study and paper after the exploratory harness
   and calibration gates are complete.

Only the first artifact and the reproducibility foundation of the second and
third are implemented in this repository today. The fourth remains planned.

## Current status

Release and task-contract version: `0.1.0`.

The repository contains a four-task exploratory pilot and schema-v2 receipt
pipeline. It does **not** yet contain a preregistered, powered, multi-domain
study or canonical learned-agent results. Historical root-level manifests and
result notes predate the integrity rebuild and are excluded from canonical
aggregation; see [docs/legacy-evidence.md](docs/legacy-evidence.md).

`PROTOCOL.md` is the current executable-study contract, but it is explicitly a
post-hoc exploratory protocol, not a claim that the study was specified before
implementation.

### Progress snapshot

| Workstream | Status | What exists |
| --- | --- | --- |
| Scientific-validity core | Complete for the exploratory fixture | Isolated runs, verified post-commit response loss, full-state oracle, strict agent claims |
| Reproducibility layer | Engineering candidate | Schema-v2 receipts, independent validator, packaging, deterministic controls, hosted CI green |
| Canonical calibration | Not started | Requires exact-tip review, repetition tooling, randomized ordering, and clean canonical receipts |
| Learned-agent comparison | Not started | No model-performance result is claimed |
| Publication study | Planned | Multi-domain tasks, preregistration, ablations, analysis, and independent reproduction |

The detailed, evidence-bounded status is in [docs/STATUS.md](docs/STATUS.md).
The implementation sequence is in the
[research roadmap](docs/handoffs/2026-08-06-btb-roadmap.md), and invalidated
historical artifacts are classified in
[docs/legacy-evidence.md](docs/legacy-evidence.md).

## How it works

```mermaid
flowchart LR
    A["Frozen synthetic task"] --> B["Browser agent or control"]
    B --> C["Visible local application"]
    C --> D["Fault proxy"]
    D --> E["Authoritative SQLite state"]
    D -->|"drop acknowledgement after verified commit"| B
    B --> F["Strict final belief"]
    E --> G["Independent evaluator"]
    F --> G
    D --> G
    G --> H["Schema-v2 receipt"]
    H --> I["Independent validator"]
```

The methodology keeps three facts separate:

1. **Effect truth:** what the authoritative application state proves happened.
2. **Agent belief:** what the agent says happened in strict final JSON.
3. **Treatment truth:** whether the intended ambiguity was actually delivered.

This separation prevents framework success flags, screenshots, or plausible
agent prose from standing in for transactional evidence.

### Evidence layers

BTB labels evidence by the layer it actually exercises:

| Layer | What it can establish | What it cannot establish |
| --- | --- | --- |
| Unit/static checks | Local invariants, schema behavior, and code quality | Browser behavior or provider behavior |
| Managed-browser controls | Fixture, injector, oracle, and deterministic control mechanics | Learned-agent capability |
| Installed-package checks | Packaging and clean-environment execution | Hosted or external reproducibility |
| Hosted CI | Repeatability on GitHub-hosted runners | Canonical study results |
| Canonical receipts | Frozen, source-bound benchmark observations | External validity without study design |
| Preregistered study | Predeclared comparisons within the sampled domains | Production-safety certification |

## What the pilot measures

| Task | Class | Contract |
| --- | --- | --- |
| `msg_read_01` | read | Report exact visible content with no durable mutation. |
| `msg_draft_save_01` | save | Create and save one exact draft without sending. |
| `msg_send_01` | send | Send once after a post-commit response drop, with an ambiguity cue. |
| `msg_send_neutral_01` | send | Same injected fault, without the ambiguity cue. |

Each run records separate axes rather than collapsing them into one score:
functional status, effect cardinality, authorization violations, duplicate
attempts, agent belief, whether ambiguity was actually delivered,
reconciliation behavior, and belief calibration.

For send tasks, the proxy drops the response only after independently verifying
the configured Nth durable commit. Rejected or uncommitted requests do not
consume the treatment. The complete proxied request sequence is bound into the
receipt.

## Install

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --constraint constraints/runtime-0.1.0.txt -e ".[baselines,dev]"
python -m playwright install chromium
```

The documented test environment includes Browser Use 0.13.6: its local
constructor/sandbox gate is decisive and is not skipped when the dependency is
missing.

`constraints/runtime-0.1.0.txt` pins the direct versions exercised by the
local release gate and CI workflow. It is a constraint set, not a complete
transitive lockfile.

## Run

Managed isolation is the default: every task/baseline pair gets a fresh server,
database, UI capability token, and injector lifecycle. Do not start a shared
fixture first.

```bash
# List frozen pilot tasks.
btb-run --list

# Deterministic controls. These produce exploratory receipts by default.
btb-run --task msg_send_01 --baseline playwright-exact
btb-run --task msg_send_01 --baseline playwright-naive

# Learned baselines. Provider/model are explicit in the receipt.
export DEEPSEEK_API_KEY=...
btb-run \
  --task msg_send_01 \
  --baseline browser-use \
  --provider deepseek \
  --model deepseek-chat

# Clean-room expanded composite condition: vision on,
# judge off, max_actions_per_step=8, and the 18-action visible-page
# tool set (arbitrary file/PDF/evaluate/write actions still excluded).
export OPENAI_API_KEY=...
btb-run \
  --task msg_send_01 \
  --baseline browser-use-full \
  --provider openai \
  --model gpt-4.1-mini
```

`browser-use-full` is a **composite** condition: it changes vision, action-set
breadth, and `max_actions_per_step` at once. It is implemented as a clean-room
benchmark condition and must be decomposed by later ablations before any
single-capability claim is drawn from it. Both
learned conditions keep fresh managed browsers, per-run UI-token isolation,
origin-only navigation, telemetry off, and no direct API/database or arbitrary
agent-controlled filesystem access. Framework-created files are restricted to
one parent-owned system-temporary sandbox per run, inventory-hashed after the
child exits, then removed and verified before a successful learned receipt.
The independent validator reconstructs each condition's exact policy and
rejects drift or inventory tampering.

Here, origin-only navigation is a narrow, explicit contract: the
benchmark-owned `navigate` callback validates the exact fixture HTTP(S) origin
before dispatch. Browser Use's allowed-domain/SecurityWatchdog layer is only
defense in depth for HTTP(S) navigation after dispatch, and the harness then
observes/recover-stops an unverified final URL. The controlled fixture exposes
no link or navigation-producing click controls; that does not establish a
general click-containment guarantee. In `browser-use-full`, the retained
`search` name preserves Browser Use 0.13.6's `SearchAction` schema but is a
benchmark-owned no-dispatch rejection, so external web search is unavailable.

`browser-use-full` requires an explicit statically allowlisted pair:
`openai/gpt-4.1-mini` or `anthropic/claude-sonnet-4-0`. This allowlist is a
local framework-policy constraint, not evidence that either remote service was
called or its vision behavior was verified. Other pairs fail closed before
browser launch or provider invocation. Browser Use 0.13.6 removes `screenshot`
when passed an explicit vision boolean, so the harness uses its public `auto`
compatibility setting, binds the effective value to `true`, and replaces the
upstream file-capable screenshot action with a benchmark-owned action that has
no filename/path parameter and only requests an image in the next observation.
Immediately before `Agent.run()`, the harness audits effective settings, LLM
roles, the action registry, generated action schemas, and screenshot callback
identity/parameter behavior plus each fixture-owned bound callback, including
the no-dispatch search rejection; the observed semantic policy is bound into
the receipt and independently validated without callable-source hashing.

This is application/framework path confinement, not mandatory OS containment
against arbitrary Python or native code. The implemented proof is static/unit,
local child-process lifecycle, and exact installed-framework constructor audit;
it does not include a live provider or browser benchmark run.

Never put API keys on the command line. Receipt-owned free text and JSON traces
are sanitized, but provider credentials must still remain in environment
variables.

Exploratory receipts default to `manifests/exploratory/current/`. Canonical
receipts default to `manifests/canonical/` and fail closed unless `HEAD` exists
and every source input bound by the receipt is clean:

```bash
btb-run --mode canonical --task msg_read_01 --baseline playwright-exact
```

A failed canonical request is not silently relabeled as canonical. Its failure
receipt records that canonical mode was requested and why it was refused.

`--external-server` exists only as an explicit compatibility mode. The runner
verifies the server's canonical database identity before reset or scoring.

## Repeated canonical studies

`btb-repeat` creates an immutable, canonical-only task × condition plan before
it can execute anything. Planning itself is offline: it does not launch a
browser or call a provider. Each condition freezes its complete effective
baseline/framework-version/provider/model/step configuration; there is no
global provider or model default. For deterministic controls the recorded model
is `deterministic-playwright`.

```bash
# This writes or verifies one byte-stable, SHA-bound plan. It does not run it.
btb-repeat plan \
  --plan manifests/repetition-plans/control-plan.json \
  --study-id control-calibration-01 \
  --seed control-calibration-01 \
  --task msg_read_01 \
  --condition '{"baseline":"playwright-exact","provider":null,"model":"deterministic-playwright","max_steps":null}' \
  --repetitions 10 \
  --study-wall-s 1800 \
  --source-repo "$PWD"

# On a clean committed checkout, run/resume exactly that plan.
btb-repeat run \
  --plan manifests/repetition-plans/control-plan.json \
  --receipt-dir manifests/repetition-receipts/control-calibration-01 \
  --source-repo "$PWD"

# Require the exact planned receipt set, independently validate each receipt,
# then write deterministic canonical-only summaries.
btb-repeat summarize \
  --plan manifests/repetition-plans/control-plan.json \
  --receipt-dir manifests/repetition-receipts/control-calibration-01 \
  --csv results/control-calibration-01.csv \
  --markdown results/control-calibration-01.md \
  --source-repo "$PWD"
```

Plans bind the exact clean, committed source tree named by `--source-repo`
(release, Git commit, and source-tree digest) and the imported `btb` runtime
tree digest separately. This lets an installed wheel plan against a clean source
checkout without pretending the wheel is that checkout; execution preflights
both identities. Run IDs are SHA-256 identities of that normalized plan, frozen
task, explicit condition, and repetition; their order is a recorded
`sha256-seed-sort-v1`, not Python's implementation-specific shuffle. The outer
`study_wall_s` budget is cumulative: each valid completed receipt's `duration_s`
is deducted before a resume starts, and the runner also charges current setup
time before every new run. Production execution runs each engine lifecycle in a
parent-owned process group capped by the remaining study/task budget; timeout
reaps the group, discards all staged child output, and lets only the parent
publish the one timeout receipt. Successful child artifacts are independently
validated in an ephemeral system-temp root, whose cleanup is verified before
the parent publishes artifacts and then receipt JSON last. It starts nothing
unless enough time remains for that task's recorded wall budget, never
fabricates a receipt for an unstarted cell, and skips only a valid, exact,
canonical receipt from the same frozen source and runtime.

The seed changes order only. It does not assign faults or mutate tasks: the
frozen task definition declares each treatment. CSV and Markdown outputs retain
all completed planned runs, including setup/baseline/timeout/evaluation failures
in denominators. They publish pass metrics and observed safety-event rates, status
and outcome counts, effect/belief/treatment counts, literal prompt-hash sets, and
declared 95% Wilson score intervals without continuity correction. Managed
runtime ports are normalized only by the declared `managed-loopback-url-v1`
compatibility rule; every other prompt template/provenance mismatch fails closed.
CSV and Markdown form one accepted summary pair only when their embedded
`summary_sha256` values match; interrupted or partial generations are detectable.

## Validate

The independent validator loads the packaged JSON Schema and reconstructs
task, state-transition, injection, claim, reconciliation, provenance, and trace
invariants without importing the benchmark scorer.

```bash
btb-validate manifests/exploratory/current
python .verify/pilot_verifier.py

# Require at least one valid canonical receipt.
python .verify/pilot_verifier.py --require-canonical
```

Development/release checks:

```bash
python -m ruff check .
python -m pytest -q -p no:cacheprovider
python .verify/pilot_verifier.py
python -m build
```

CI also builds the distributions, installs the wheel into a clean environment,
checks packaged tasks/template/schema and console scripts, and runs the exact
and naive injected controls in managed Chromium.

## Receipt trust boundary

A schema-v2 receipt binds:

- the complete frozen task and exact executed prompt;
- exact source-tree digest plus Git state;
- framework, provider, model, generation parameters, configured budgets,
  modality, and enforced capability policy;
- before/after full SQLite snapshots;
- strict final-answer claim and complete sanitized execution trace;
- full injection/reconciliation evidence;
- independently reconstructable evaluation axes.

Only one top-level lifecycle owner may finalize a receipt, and only after proxy
and fixture teardown succeed. Setup, timeout, baseline, evaluation, and cleanup
failures remain denominator-bearing failure receipts.

## Research materials

- [Exploratory protocol](PROTOCOL.md) — current task and scoring contract.
- [Project status](docs/STATUS.md) — completed work, evidence, limitations, and
  next gates.
- [Integrity rebuild plan](docs/plans/2026-08-06-btb-integrity-rebuild.md) —
  validity and reproducibility design.
- [Implementation roadmap](docs/handoffs/2026-08-06-btb-roadmap.md) — path from
  the exploratory fixture to a preregistered multi-domain study.
- [Publication-gap research](results/publication_gap_research.md) — early
  related-work and positioning notes; not a finished literature review.
- [Legacy evidence classification](docs/legacy-evidence.md) — why early pilot
  artifacts are preserved but excluded from claims.

## Repository structure

```text
btb/app/                 synthetic transaction application and SQLite state
btb/harness/             run isolation, fault injection, receipts, validation
btb/oracle/              strict claim parsing and authoritative evaluation
btb/tasks/definitions/   versioned exploratory task contracts
btb/baselines/           deterministic browser controls
btb/schemas/             schema-v2 receipt contract
tests/                   unit and integration coverage
.verify/                 independent verification entry point
docs/                    status, integrity plan, handoffs, and roadmap
results/                 historical exploratory research notes
manifests/               preserved legacy artifacts; non-canonical
```

## Roadmap

1. Complete an exact-tip independent review; hosted CI is already green on the
   accepted private-repository tip.
2. Add a resumable, randomized repetition runner with a global wall-clock bound.
3. Produce clean canonical deterministic calibration receipts.
4. Run a small matched learned-agent pilot only after controls are stable.
5. Expand to several synthetic transaction domains and preregister the study.
6. Publish analysis code, limitations, checksummed artifacts, and an independent
   clean-install reproduction.

See [CONTRIBUTING.md](CONTRIBUTING.md) for research-integrity rules and the local
verification gate.

## Clean-room and claim boundary

- The fixture and tasks use only synthetic local data. No real message is sent.
- No Anticipy code, data, screenshots, branding, or private task history is
  included.
- Existing exploratory controls establish harness behavior only. They do not
  establish comparative Browser Use or model capability.
- Publication claims require the Phase 3 calibration and Phase 4 multi-domain,
  randomized, preregistered reproduction plan in
  `docs/handoffs/2026-08-06-btb-roadmap.md`.

## Citation

If this repository informs your work, cite the software using
[`CITATION.cff`](CITATION.cff). The preferred paper citation will be added only
after the study design is preregistered and a manuscript exists.

## License

MIT. See `LICENSE`.
