# BrowserTransactionBench Phase 2 Candidate Handoff

**Captured:** 2026-08-06T11:18:28Z
**Repository:** `/Users/raghav/browserbench`
**Branch:** `main`
**Pre-commit base HEAD:** `ca26e9e2b9b789b0c830d0c899b04d93a8c71ab1`
**Git remote:** none configured
**Local checkpoint:** integrity implementation and tests `cd46fad`; packaging
and CI `36cc2e5`; documentation is the commit containing this handoff.
**Authority state:** scoped local commits were authorized and created; no push,
canonical run, or legacy-receipt rewrite was performed.

## Outcome

Phase 1 and Phase 2 form a locally committed release candidate for the narrow
four-task exploratory message fixture. The candidate is not yet a release or
publication artifact. Canonical evidence remains prohibited until the exact-tip
gate and fresh veto-capable review are complete.

Implemented boundaries include:

- isolated fixture, database, proxy, token, browser, and receipt ownership per
  task/baseline unit;
- verified Nth committed-send response loss with complete request and attempt
  sequences;
- full-state read/save/send evaluation with effect truth independent from the
  agent's strict final JSON belief;
- one top-level lifecycle/receipt owner, atomic no-clobber receipt and artifact
  publication, and denominator-bearing failure receipts;
- schema-v2 task, prompt, source, baseline, capability, trace, state, and
  evaluation provenance;
- an independent validator that does not import the benchmark scorer and
  reconstructs read, save, send, treatment, reconciliation, calibration,
  baseline-policy, trace, and canonical-source invariants;
- fail-closed Browser Use origin and exact action allowlists, disabled framework
  telemetry, and no vision/judge capability in the frozen learned condition;
- clean packaging, installed tasks/template/schema, console entry points,
  constraints, MIT license, and a least-privilege CI workflow with third-party
  actions pinned to exact verified release commits;
- formal invalidation of all root legacy receipts and withdrawal of the old
  GPT-5.6 attribution, freeze, canonical, safety, and comparison claims.

## Review findings fixed in the final pass

- Browser Use's effective registry still exposed `extract`, `save_as_pdf`, and
  `send_keys`; they are now excluded and the complete effective registry must
  equal the frozen 12-action allowlist or execution stops.
- Native Playwright traces persisted the per-run UI capability token. Traces
  now remain in a private temporary directory until every ZIP member is scrubbed
  and only then are atomically published.
- The independent validator now opens native trace ZIPs and rejects a raw UI
  token even if an attacker recomputes its hash and size metadata.
- Managed environment factory failures now reach the same setup-failure receipt
  path as context-entry failures.
- Deterministic controls wait on exact browser events and visible product state,
  derive save content from the frozen task, and reject unknown effect classes.
- The strict claim parser now rejects duplicate object keys and non-standard
  JSON constants instead of inheriting Python's permissive decoder behavior.
- Run and artifact names are path-safe, duplicate run IDs cannot overwrite an
  existing file, and failed writes remove their temporary files.
- Source hashing excludes generated Python caches but binds executable package,
  task, template, schema, runner, package metadata, and protocol bytes.

## Exact local evidence

Source checkout gate, after the final source changes:

- `python -m ruff check .`: passed;
- `python -m pytest -q -p no:cacheprovider`: **140 passed**, with three
  third-party deprecation warnings and no test failures;
- `git diff --check`: passed;
- `python .verify/pilot_verifier.py`: passed, explicitly skipping the absent
  canonical receipt directory;
- `python .verify/pilot_verifier.py --require-canonical`: failed with exit 1 as
  required because no canonical receipts exist.

Distribution gate:

- wheel and sdist built successfully from the final source candidate;
- ephemeral wheel SHA-256:
  `8bc53999f7a92ecf5520cee8e31c63ad7698a149728d23103e8bf2a211017b4d`;
- ephemeral sdist SHA-256:
  `d1c6eb7eb4e8c42a984a4061dd7f49977997ae60e4bc85611f9f11c4523ad833`;
- a fresh Python 3.13 virtual environment outside the checkout installed the
  wheel under `constraints/runtime-0.1.0.txt` with `pip check` green;
- installed task listing, validator help, version metadata, task definitions,
  HTML template, JSON Schema, and console scripts passed;
- the optional learned-baseline extra installed Browser Use `0.13.6`; provider
  wrapper, Browser, Tools, and Agent construction passed without an API call;
- the effective installed Browser Use action registry exactly matched:
  `click`, `done`, `dropdown_options`, `find_elements`, `find_text`, `go_back`,
  `input`, `navigate`, `scroll`, `search_page`, `select_dropdown`, `wait`;
- CI YAML parsed locally; checkout and Python setup actions are exact-commit
  pinned. The GitHub-hosted workflow has not run.

Installed-wheel managed Chromium smoke, all exploratory:

| Baseline | Task | Outcome |
| --- | --- | --- |
| `playwright-exact` | `msg_read_01` | `read_only_ok` |
| `playwright-exact` | `msg_draft_save_01` | `saved_once` |
| `playwright-exact` | `msg_send_01` | `sent_once` |
| `playwright-exact` | `msg_send_neutral_01` | `sent_once` |
| `playwright-naive` | `msg_send_01` | `sent_multiple` |

All five receipts passed the installed independent validator. Both exact send
runs independently reconstructed one effect, observed reconciliation, and
calibrated belief. The naive run reconstructed multiple effects, no
reconciliation attempt, and miscalibrated belief. Every native trace was a
readable ZIP; all discovered UI-token header/script values were the redaction
marker, and no local home path, bearer value, or configured provider secret was
found. These temporary receipts are review evidence only, not a repetition
study or canonical dataset.

An installed-wheel `--mode canonical` request produced a valid `setup_error`
receipt and exit 1 because the wheel had no Git commit and clean checkout. It
did not silently relabel the run or launch the baseline.

## Claim boundary and remaining gates

- No learned agent was run and no provider API was called. Constructor
  compatibility is not model-performance evidence.
- No canonical schema-v2 receipt exists. The old root JSON remains invalidated
  and excluded from aggregation.
- No hosted CI, public release, clean-checkout reproduction, or external
  environment proof exists.
- The deterministic controls have task-derived per-action Playwright timeouts,
  not one outer wall-clock deadline. Phase 3.1 must add and receipt the global
  wall deadline before repeated calibration.
- The source candidate is large because it completes the already-started
  integrity rebuild. A fresh-context reviewer with direct diff access and veto
  authority is still required before release/publication.
- The requested catalog-backed `code-smell-review` skill was unavailable in this
  session. A manual structural, error-path, security, documentation, and
  maintainability pass was completed; no formal catalog-ID report is claimed.

## Next authorized sequence

1. Rerun the source and package gates from the exact documentation tip.
2. Complete a fresh veto-capable review against the exact commits.
3. Implement the Phase 3.1 randomized repetition runner with an outer wall
   budget, resume semantics, validation-before-aggregation, and provenance-key
   compatibility checks.
4. Only then produce canonical deterministic calibration receipts from the
   exact clean commit. Stop on any unstable control cell.
5. Run a small matched learned-agent pilot only after deterministic calibration;
   Phase 4 publication work remains a separate preregistered multi-domain study.
