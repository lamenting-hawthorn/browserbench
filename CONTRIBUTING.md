# Contributing to BrowserTransactionBench

BrowserTransactionBench is an exploratory research project. Contributions are
welcome when they preserve the distinction between durable effect truth, agent
belief, and verified treatment delivery.

## Good contribution areas

- new synthetic transaction domains and task definitions;
- stronger independent receipt validation;
- fault-injection and reconciliation scenarios;
- deterministic controls and capability-policy tests;
- repetition, randomization, analysis, and reproducibility tooling;
- documentation, threat analysis, and related-work corrections.

Please open an issue before a large change so its research contract and evidence
requirements can be agreed on first.

## Research-integrity rules

- Never relabel exploratory or legacy artifacts as canonical evidence.
- Never infer durable success from a screenshot, framework flag, or agent prose.
- Keep setup, timeout, evaluation, and cleanup failures denominator-bearing.
- Do not add real accounts, recipients, payments, or outbound side effects to
  fixtures.
- Do not commit provider keys, credentials, local paths, or unsanitized traces.
- Bind material task, source, policy, environment, trace, and oracle changes into
  receipts and their independent validation.
- Label local, installed-package, hosted CI, provider, staging, and production
  evidence separately.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --constraint constraints/runtime-0.1.0.txt -e ".[dev]"
python -m playwright install chromium
```

## Verification

Run the complete local gate before opening a pull request:

```bash
python -m ruff check .
python -m pytest -q -p no:cacheprovider
python .verify/pilot_verifier.py
python -m build
git diff --check
```

Changes to the installed package or managed browser path should also be tested
through the relevant clean-wheel and browser smoke paths in
`.github/workflows/ci.yml`.

## Pull requests

Keep each pull request narrow. Explain:

1. the research or engineering problem;
2. the frozen behavior or invariant being changed;
3. the proof layer exercised by the tests;
4. known exclusions and remaining risks;
5. whether existing receipts remain compatible or require a versioned migration.

Passing tests demonstrate only the layer they exercise. A change cannot approve
its own canonical or publication claims.
