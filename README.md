# BrowserTransactionBench

BrowserTransactionBench (BTB) is a clean-room research harness for testing
transactional safety in browser agents: whether an authorized durable effect
happens exactly once, whether forbidden effects are avoided, and whether the
agent's stated belief is calibrated after an acknowledgement is lost.

The SQLite fixture is authoritative. Screenshots, framework success flags, and
agent prose never determine effect truth.

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
python -m pip install --constraint constraints/runtime-0.1.0.txt -e ".[dev]"
python -m playwright install chromium
```

For the learned Browser Use baseline:

```bash
python -m pip install --constraint constraints/runtime-0.1.0.txt -e ".[baselines]"
```

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

# Learned baseline. Provider/model are explicit in the receipt.
export DEEPSEEK_API_KEY=...
btb-run \
  --task msg_send_01 \
  --baseline browser-use \
  --provider deepseek \
  --model deepseek-chat
```

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

## Clean-room and claim boundary

- The fixture and tasks use only synthetic local data. No real message is sent.
- No Anticipy code, data, screenshots, branding, or private task history is
  included.
- Existing exploratory controls establish harness behavior only. They do not
  establish comparative Browser Use or model capability.
- Publication claims require the Phase 3 calibration and Phase 4 multi-domain,
  randomized, preregistered reproduction plan in
  `docs/handoffs/2026-08-06-btb-roadmap.md`.

## License

MIT. See `LICENSE`.
