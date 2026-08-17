# BrowserTransactionBench status

**Public status date:** 2026-08-17

**Release/task contract:** `0.1.0`

**Maturity:** exploratory research alpha

## Goal

BrowserTransactionBench aims to evaluate whether browser agents perform
transactional actions safely under uncertainty. The target artifact is not only
a test suite: it is a frozen benchmark, comparative baselines, a model-agnostic
safety reference, and a publishable study backed by independently verifiable
receipts.

The current repository is the first synthetic message-domain fixture. It is a
working research artifact and engineering foundation, not a completed benchmark
paper or model leaderboard.

## What is implemented

- Four frozen exploratory tasks: read, save draft, send under an explicit
  ambiguity cue, and send under a matched neutral instruction.
- Per-run ownership of the temporary application, SQLite database, proxy,
  capability token, browser context, traces, and receipt lifecycle.
- Response loss only after the proxy independently verifies the configured
  durable send commit.
- Full before/after state evaluation across users, drafts, messages, and send
  attempts.
- Independent scoring axes for intended state, duplicates, forbidden effects,
  treatment delivery, reconciliation, agent belief, and calibration.
- Strict structured final claims with malformed, absent, sent, not-sent, and
  unknown outcomes kept distinct.
- Schema-v2 success and denominator-bearing failure receipts, atomic output,
  source provenance, and an independent validator.
- Deterministic Playwright controls, Python packaging, an MIT license, and a
  least-privilege GitHub Actions workflow.

## Evidence currently available

The Phase 2 candidate handoff records local source checks, clean-wheel checks,
installed-package checks, and exploratory deterministic browser controls. Those
checks support the engineering behavior of the harness at that checkpoint.

They do **not** establish model quality, population-level rates, external
validity, or publication-ready results. No learned-agent provider run and no
canonical schema-v2 dataset is claimed by the accepted public baseline.

All older root-level JSON manifests and pre-`0.1.0` result notes are retained as
audit history but are invalid for canonical aggregation. The reasons are
documented in [legacy-evidence.md](legacy-evidence.md).

## Current gates

| Gate | State | Required evidence |
| --- | --- | --- |
| Exploratory harness | Implemented | Source, package, validator, and deterministic-control checks |
| Hosted CI | To be established on the public tip | Green GitHub Actions matrix and package/browser jobs |
| Independent exact-tip review | Pending | Fresh review with authority to veto claims |
| Repetition and randomization | Pending | Resumable runner, global deadline, stable controls, compatible provenance |
| Canonical calibration | Blocked | Clean committed source and validated canonical receipts |
| Learned-agent pilot | Blocked | Stable deterministic calibration first |
| Publication study | Blocked | Multi-domain design, preregistration, power/analysis plan, and reproduction |

## Methodology principles

1. The application/database oracle, not the agent or model, determines durable
   effect truth.
2. Agent belief, treatment delivery, and durable effect are recorded separately.
3. Every task/baseline run owns isolated state; shared pilot databases are not
   accepted as comparative evidence.
4. Failure receipts remain in the denominator; setup or cleanup failures are not
   silently discarded.
5. Canonical evidence must bind clean source, frozen task/protocol inputs,
   baseline policy, environment, trace, and state.
6. Exploratory controls can validate harness mechanics, but they cannot prove
   learned-agent capability or production safety.

## Next work

The immediate engineering sequence is:

1. Establish hosted CI and complete a fresh exact-tip review.
2. Build Phase 3.1 repetition, randomization, resume, and aggregation tooling.
3. Calibrate deterministic controls from clean canonical commits.
4. Run a small matched learned-agent pilot with explicit provider/model
   provenance and no retrospective metric changes.
5. Design and preregister the multi-domain publication study, including
   idempotency and reconciliation affordance ablations.

The detailed implementation plan is maintained in
[the roadmap](handoffs/2026-08-06-btb-roadmap.md).

## Claim boundary

It is accurate to describe this repository as an open exploratory research
harness and methodology for browser-agent transactional safety. It is not yet
accurate to describe it as a completed benchmark, a validated model comparison,
a production-safety certification, or a finished academic publication.
