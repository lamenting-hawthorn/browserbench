# Pilot defects log (BrowserTransactionBench)

Recording harness/oracle **bugs found during the pilot** (not agent-model
behaviors). All were fixed in place; this log is kept for honesty and for the
independent review (PROTOCOL §13). Each entry: symptom → root cause → fix.

Status header: **freeze `pilot_freeze_0_0_1` was defined BEFORE these fixes;
none of these change the research question, task contract, or scoring classes —
they are implementation defects, so the freeze holds.**

---

## 1. SQLite "database is locked" under the threaded server

- **Symptom:** `POST /api/drafts` and `/api/drafts/{id}/save` returned 500 with
  `sqlite3.OperationalError: database is locked` once the browser's parallel
  `refresh()` / `refreshSent()` reads fired.
- **Root cause:** `db.connect()` ran `executescript(_SCHEMA)` (CREATE TABLE ...)
  on **every** connection. In the FastAPI threadpool, concurrent short-lived
  connections triggering DDL each demand a write lock → deadlock / lock error.
- **Fix:** DDL moved to a one-time `init_db()` called from the server `lifespan`
  (main event-loop thread) and to `reset()`. `connect()` is read/write only, and
  WAL + `busy_timeout=30000` were enabled so concurrent readers/writers coexist.

## 2. Reverse-proxy returned HTML with wrong Content-Type

- **Symptom:** the page rendered as raw HTML text (`<body>` inner_text contained
  the literal `<!DOCTYPE html>...`) — no draft list, selector timeouts.
- **Root cause:** the fault-injection proxy always set `Content-Type:
  application/json` even when forwarding the HTML page from the app.
- **Fix:** the proxy now forwards the upstream `Content-Type` header verbatim.

## 3. Disconnect proxy was baseline-specific / not reusable

- **Symptom/design:** initial design used Playwright route interception, which
  only works for baselines we control the page of, not for an agent that manages
  its own browser (Browser Use).
- **Fix:** replaced with a **harness-owned reverse proxy** (`inject.InjectProxy`)
  that both Playwright and Browser Use point their base_url at. A POST to
  `/api/messages/send` is forwarded (DB commit = "effect may have occurred") and
  the response dropped (502). Baseline-agnostic and deterministic. **This is the
  injection described in PROTOCOL §8.**

## 4. Read-task scoring flagged pre-existing saved drafts

- **Symptom:** `msg_read_01` scored `forbidden_write` even though the read made
  no writes. The initial state legitimately contains a `saved` draft.
- **Root cause:** `score_outcome` for `effect_class == "read"` flagged
  `forbidden_write` whenever *any* draft was saved — treating the initial state
  as a write.
- **Fix:** read scoring compares against an **initial snapshot** (taken right
  after `prepare_initial_state`) and only flags *new* sends/saves introduced
  during the run. `oracle/score.py` now has `scoring_context_with_initial()` and
  the engine captures the initial snapshot via `_scoring_context()`.

## 5. Playwright `exact` reconciliation read the Sent list before it loaded

- **Symptom:** the reconciliation control did `wait_for_selector("#sent li,
  #sent", state="attached")` — `#sent` (an empty `<ul>`) is always in the DOM,
  so it returned before the async `refreshSent()` fetch populated child `li`s.
  Count came back 0 even after a successful committed send → it blind-retried →
  `sent_multiple` even for the well-behaved control.
- **Fix:** after reload, wait for `#drafts li` (confirms refresh) then a short
  settle (600 ms) before counting `#sent li`. Now `exact` → `sent_once`,
  `naive_retry` → `sent_multiple`.

---

## Remaining known limitations (accepted for the pilot)

- **Agent-claim heuristic is noisy:** `engine._claimed_send()` is a keyword
  search and can return True on verbose agent transcripts that merely mention
  "sent" (e.g. the read task's `claimed_send=True` here). It does **not** affect
  DB-derived outcome classes except `false_success` (send task, count=0). A
  structured final-answer parser is a freeze-gated improvement for the full
  benchmark (it touches scoring, so it bumps freeze below).
- **Provider scope:** the Browser Use baseline currently runs only with a
  text/DOM model (`use_vision=False`) because DeepSeek `deepseek-chat` is
  text-only. The app is DOM-driven so this is sufficient for the pilot; a
  vision-capable frontier model is a Phase 2 baseline.
