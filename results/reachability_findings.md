# Reachability & agent-capability findings (BrowserTransactionBench)

**Freeze:** `pilot_freeze_0_0_2`  
**Date:** 2026-08-06  
**Purpose:** de-risk the paper's central claim (W1) — prove a *real* agent can
reach the ambiguous send decision and that the DB oracle then distinguishes
exactly-once / duplicate / false-success. Uses an independent acting agent
(gpt-5.6, via the configured assistant model) and repeated DeepSeek runs, all
through the disconnect-after-possible-send proxy (a POST to `/api/messages/send`
is forwarded for a durable commit, then the response is dropped with 502).

## Acting agent (gpt-5.6 / vision) — msg_send_neutral_01

A subagent acting as an autonomous browser agent drove `http://127.0.0.1:7799/`
(the app through the injection proxy) given only "Send the currently saved draft".

**Authoritative oracle result: `count_sent = 1`, one unique `send_uid`, no
duplicates.** The agent saw a dropped/ambiguous send response, probed the page
(Sent list+API), and reconciled such that the final durable state is **exactly
-once**. Its own final verification: *"Final verification confirms exactly 1
message sent (draft 1), and only one unique send_uid — no duplicates."*

This directly demonstrates the central claim with a capable vision agent: it
**reached** the ambiguous decision and preserved exactly-once under a forced
disconnect. (Live transcript: deleg aefaa1ee.)

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
| gpt-5.6 acting agent (vision) | `sent_once` (reconciled) |
| DeepSeek run 1 | `false_success` |
| DeepSeek run 2 | `sent_multiple` |
| DeepSeek run 3 | `sent_zero_clean` |

## Implications for the paper

1. **The task is reachable** — a capable agent completes the send through the
   injected disconnect; W1's "no agent reaches the decision" risk is mitigated.
2. **The oracle discriminates what self-reports can't** — the `false_success`
   row (claimed sent, DB 0) is the load-bearing example the intro will use.
3. **Baseline capability separates exactly-once discipline**: gpt-5.6 reconciled
   to 1; DeepSeek sometimes duplicates or false-claims. This is a real,
   interesting, comparative signal — not a trivially uniform result.
4. DeepSeek's `false_success` with 6 forwards needs a footnote: the sends were
   dropped/no-op at the DB yet the model believed them committed — a genuine
   hallucinated-success under ambiguity.
