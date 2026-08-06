# Legacy evidence classification

**Decision date:** 2026-08-06
**Decision:** invalid for canonical aggregation and publication claims

The 36 JSON files directly under `manifests/` and the pre-`0.1.0` documents
under `results/` are preserved only as historical audit material. They are not
schema-v2 receipts and must not be migrated or reinterpreted as if the missing
evidence had been captured.

## Why they are invalidated

The historical runs predate the integrity rebuild and lack one or more required
properties:

- isolated per-run fixture/database/proxy ownership;
- independently verified post-commit treatment semantics;
- complete before/after authoritative snapshots;
- strict final-result claim parsing rather than transcript/prose inference;
- complete sanitized trace artifacts;
- exact framework/provider/model/generation/capability provenance;
- one top-level receipt owner and teardown-complete finalization;
- source-tree byte binding and independently reconstructed schema-v2 invariants.

The historical acting-agent demonstration additionally used direct backend
inspection as ground truth from the actor side, outside the current
visible-controls-only policy. Its claimed model name was not bound in a receipt,
so the actor's exact model identity is unknown. It must not be described as a
GPT-5.6 result.

Some legacy proxy records used ambiguous or incomplete `forwarded` semantics and
did not prove whether a response was dropped before or after durable commit.
Consequently, they cannot establish that the intended ambiguity treatment was
delivered.

## Enforced handling

- Legacy JSON stays at `manifests/*.json` unchanged for auditability.
- New exploratory schema-v2 evidence goes under
  `manifests/exploratory/current/`.
- Canonical schema-v2 evidence may only go under `manifests/canonical/` after
  clean-source checks.
- `.verify/pilot_verifier.py` validates only `manifests/canonical/*.json` for
  canonical aggregation and ignores root legacy JSON.
- No rate, comparison, model-capability conclusion, or publication table may be
  derived from the legacy set.

The old files may still inform defect discovery, but every result must be rerun
from a clean committed `0.1.0` or later artifact under the current contract.
