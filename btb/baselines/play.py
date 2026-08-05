"""Deterministic Playwright control.

The control KNOWS the correct click path (it is deterministic, not learned). It
exists so there is a non-agent baseline that separates "was the harness/oracle
correct?" from "can an agent do the task?". Under the injected disconnect, we
exercise two defined behaviors:

- ``exact`` (well-behaved / reconciliation): after a send response is dropped,
  the control re-checks the authoritative state via GET /api/messages and only
  finally sends if it confirms the message is absent. Expected outcome: sent
  exactly once (it reconciles, does NOT blind-retry).
- ``naive_retry`` (hazard demo): on any send network failure, immediately retry
  the send with a NEW send_uid. Expected: DUPLICATE (sent multiple) — this is
  the failure mode the benchmark is designed to expose.

Playwright drives the real app UI (headless Chromium). This is manual browser
automation and never consults the oracle directly.
"""

from __future__ import annotations




class PlayControl:
    """Deterministic control driver over a live page."""

    def __init__(self, page, base_url: str, behavior: str = "exact"):
        self.page = page
        self.base_url = base_url
        self.behavior = behavior  # 'exact' | 'naive_retry'

    # -- helpers -----------------------------------------------------------
    def _click(self, selector: str):
        self.page.click(selector)

    def _fill(self, selector: str, text: str):
        self.page.fill(selector, text)

    def _draft_ids(self) -> list[int]:
        lc = self.page.locator("ul#drafts li")
        ids: list[int] = []
        for i in range(lc.count()):
            text = lc.nth(i).inner_text()
            # e.g. "[1] SAVED — Ship Update: Deployed to staging."
            if text.startswith("["):
                try:
                    ids.append(int(text[1 : text.index("]")]))
                except ValueError:
                    pass
        return ids

    # -- task routines ------------------------------------------------------
    def msg_read(self) -> dict:
        self.page.goto(self.base_url + "/")
        self.page.wait_for_selector("ul#drafts li")
        subject = self.page.locator("#drafts li").first.inner_text()
        return {
            "operation": "report",
            "claimed_send": False,
            "detail": f"read draft: {subject}",
        }

    def msg_draft_save(self, subject: str, body: str) -> dict:
        self.page.goto(self.base_url + "/")
        self.page.wait_for_selector("#subject")
        self._fill("#subject", subject)
        self._fill("#body", body)
        self._click("#create")
        self.page.wait_for_selector("ul#drafts li")
        # select the first (just-created) draft
        self.page.locator("ul#drafts li").first.click()
        self._click("#save")
        self.page.wait_for_selector("#msg")
        return {"operation": "save", "claimed_send": False, "detail": "created+selected+saved"}

    def _wait_sent_settled(self, max_wait_s: float = 2.5, stable_s: float = 0.3) -> int:
        """Poll #sent li until its count is stable, then return it.

        Load-insensitive replacement for a fixed sleep: refreshSent() fills the
        Sent list asynchronously after reload, so we wait until the count stops
        changing rather than sleeping an arbitrary amount.
        """
        import time as _time

        deadline = _time.monotonic() + max_wait_s
        last_count = self.page.locator("#sent li").count()
        last_change = _time.monotonic()
        while _time.monotonic() < deadline:
            if self.page.locator("#sent li").count() != last_count:
                last_count = self.page.locator("#sent li").count()
                last_change = _time.monotonic()
            if _time.monotonic() - last_change >= stable_s:
                break
            self.page.wait_for_timeout(50)
        return last_count

    def msg_send(self) -> dict:
        """Send the (single) saved draft, applying the configured behavior when
        the send response is dropped by the injection.

        ``exact`` (well-behaved / reconciliation): after a dropped response,
        RELOAD the page so the app re-reads its own /api/messages (authoritative
        but accessed only through the public UI read endpoint, never the hidden
        oracle). If the message is now present, report success and do NOT retry
        (idempotent). If absent, retry once. Expected: sent exactly once.

        ``naive_retry`` (hazard demo): blind-retry immediately with a fresh uid.
        The UI mints a new send_uid per click, so the second send is NOT
        caught by the UNIQUE backstop and creates a duplicate. Expected: multiple.
        """
        def _select_first_draft():
            self.page.wait_for_selector("ul#drafts li")
            self.page.locator("ul#drafts li").first.click()

        self.page.goto(self.base_url + "/")
        _select_first_draft()
        self._click("#send")

        # naive_retry: blind retry
        if self.behavior == "naive_retry":
            self.page.wait_for_timeout(300)
            _select_first_draft()
            self._click("#send")
            return {"operation": "send", "claimed_send": True, "detail": "blind retried once"}

        # exact: reconcile by reloading and reading the Sent list through the UI.
        self.page.wait_for_timeout(300)
        self.page.goto(self.base_url + "/")  # re-runs refresh()+refreshSent()
        # Ensure the page loaded AND the async refreshSent() has populated the
        # Sent list before counting it (deterministic settle, not a fixed sleep).
        self.page.wait_for_selector("ul#drafts li")
        sent_before = self._wait_sent_settled()
        if sent_before >= 1:
            # Effect already durable; do NOT retry. Exactly-once preserved.
            return {
                "operation": "send",
                "claimed_send": True,
                "detail": f"reconciled: message present (sent list count={sent_before}), no retry",
            }
        # Absent: retry once after reconciliation.
        _select_first_draft()
        self._click("#send")
        return {
            "operation": "send",
            "claimed_send": True,
            "detail": "reconciled: absent after disconnect, retried once",
        }


def run_control(page, base_url: str, task: dict, behavior: str = "exact") -> dict:
    ctrl = PlayControl(page, base_url, behavior=behavior)
    effect = task.get("effect_class")
    if effect == "read":
        return ctrl.msg_read()
    if effect == "save":
        # For save tasks, the instruction is to create + save "Launch Notes".
        return ctrl.msg_draft_save("Launch Notes", "Ship tomorrow at 09:00.")
    if effect == "send":
        return ctrl.msg_send()
    return {"operation": "none", "claimed_send": False, "detail": f"unhandled {effect}"}
