# Verification sequence

Run from the repository root with the project environment active:

```bash
ruff check btb tests run_pilot.py .verify
python .verify/pilot_verifier.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python .verify/pilot_verifier.py
```

The second smoke is intentional: it confirms the verifier still passes after
the test suite has exercised temporary receipt and database paths.

For a release candidate that is required to contain canonical evidence, add
`--require-canonical`. Legacy JSON files directly under `manifests/` are
historical exploratory artifacts and are never aggregated by this verifier.
