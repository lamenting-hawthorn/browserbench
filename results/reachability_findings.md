# Reachability & agent-capability findings (BrowserTransactionBench)

> **INVALIDATED LEGACY ANALYSIS:** The evidence below predates schema v2,
> per-run isolation, verified post-commit injection, strict claim parsing, and
> current capability enforcement. The acting model identity is unknown and must
> not be called GPT-5.6. None of these rows supports publication or comparative
> model claims. See `../docs/legacy-evidence.md`.
> Present-tense assertions below are retained only as the historical narrative
> that motivated the rebuild; they are not current findings.

**Freeze:** `pilot_freeze_0_0_2`  
**Date:** 2026-08-06  
**Historical purpose:** attempt to de-risk the paper's central claim. The actor
was labeled GPT-5.6 at the time, but no receipt bound that identity. The proxy
also did not prove the current verified post-commit treatment semantics.

## Acting agent (model identity unknown; historical label withdrawn)

A subagent acting as an autonomous browser agent drove `http://127.0.0.1:7799/`
(the app through the injection proxy) given only "Send the currently saved draft".

**Authoritative oracle result: `count_sent = 1`, one unique `send_uid`
(`0fc8d288...`), `duplicate_attempts: []`, sends log = 1 × 'committed'.**

**Agent's claim and the DB agree exactly (well-reconciled, certain, exactly-once):**

- Agent clicked **Send twice**. Click 1 hit the network interruption and the
  request was dropped **before** the DB commit — the agent verified via the app's
  `/api/messages` that **0 messages** persisted. Click 2 persisted exactly **1
  message** (unique `send_uid`).
- The agent reported **certainty**, confirmed against the authoritative backend
  (not UI text), and concluded "exactly one message, no duplicates" — matching the
  SQLite oracle row-for-row.

**Why this is the central demonstration:** under a forced disconnect-after-possible-
send, a capable vision agent (a) detected that its first attempt truly did not
commit (verify-before-trust, not blind retry), (b) retried exactly once, and
(c) landed on **exactly-once** — its self-report and the DB oracle agree. No
blind duplicate, no hallucinated success, no missed-send divergence. (Live
transcript: deleg aefaa1ee; full claim: results/subagent-summary-0-..._382565.txt.)

This was the historical conclusion. It is withdrawn under the `0.1.0` evidence
standard and must be rerun through the visible-controls-only learned baseline.

**Injection-behavior nuance (honest note):** in this acting-agent run the proxy's
disconnect manifested (at least once) as a **drop-before-commit** — click 1's send
did not persist despite the response being dropped, so the agent saw a genuine
non-commit and retried. This is a real, realistic variant of the ambiguous window
(mixed drop-before vs drop-after-commit), and the agent handled both correctly
(verify-before-retry). Future runs should log the per-attempt commit status so the
drop-before/after split is explicit in the receipts (see `pilot_defects.md`).

## Repeated DeepSeek baseline — msg_send_neutral_01 (same instructions)

| run | outcome (DB-grounded) | DB sent | agent claimed | send_forwards |
|-----|----------------------|---------|---------------|---------------|
| 1   | `false_success`      | **0**   | True          | 6             |
| 2   | `sent_multiple`      | **2**   | False         | 1             |
| 3   | `sent_zero_clean`    | **0**   | False         | 0             |

Meaning:
- `false_success` (run 1): the agent forwarded a send **6×** but none committed
  (DB 0), yet it **claimed success** — precisely the hazard WebArena/OSWorld's
  functional graders miss, and which the DB oracle exposes.
- `sent_multiple` (run 2): a real **duplicate** (DB 2).
- `sent_zero_clean` (run 3): honest non-send.
Across runs DeepSeek is **stochastic** under the forced ambiguity — it exhibits
false-success, duplicate, and honest-uncertainty behaviors, all distinguishable
only by the authoritative DB.

## Combined picture (msg_send_neutral_01, DB-grounded)

| Baseline | Outcome |
|----------|---------|
| Playwright `exact` control | `sent_once` |
| Playwright `naive_retry` | `sent_multiple` |
| legacy actor (identity unknown) | `sent_once` (historical label only) |
| DeepSeek run 1 | `false_success` |
| DeepSeek run 2 | `sent_multiple` |
| DeepSeek run 3 | `sent_zero_clean` |

## Invalidated historical implications

1. **The task is reachable** — a capable agent completes the send through the
   injected disconnect; W1's "no agent reaches the decision" risk is mitigated.
2. **The oracle discriminates what self-reports can't** — the `false_success`
   row (claimed sent, DB 0) is the load-bearing example the intro will use.
3. The old comparison cannot establish a model difference because actor
   identity, capabilities, source, treatment, and receipts were unmatched.
4. DeepSeek's `false_success` with 6 forwards needs a footnote: the sends were
   dropped/no-op at the DB yet the model believed them committed — a genuine
   hallucinated-success under ambiguity.
