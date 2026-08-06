# Manifest classification

The JSON files directly in this directory are legacy schema-v1 exploratory
artifacts. They are formally invalidated for canonical aggregation; see
`../docs/legacy-evidence.md`. They are intentionally preserved byte-for-byte and
must not be upgraded in place.

Current destinations:

- `exploratory/current/`: schema-v2 exploratory and failure receipts;
- `canonical/`: independently valid schema-v2 receipts from explicitly
  requested clean committed source only.

Artifact traces live in an `artifacts/` child of the corresponding receipt
directory and are hash/size bound by their receipt.
