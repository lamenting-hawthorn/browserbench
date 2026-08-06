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

    @staticmethod
    def _is_send_request(request) -> bool:
        return request.method == "POST" and request.url.split("?", 1)[0].endswith(
            "/api/messages/send"
        )

    def _click_and_wait_for_dropped_send(self) -> None:
        with self.page.expect_event(
            "requestfailed",
            predicate=self._is_send_request,
        ):
            self._click("#send")

    def _click_and_wait_for_successful_send(self) -> None:
        with self.page.expect_response(
            lambda response: self._is_send_request(response.request)
        ) as response_info:
            self._click("#send")
        if not response_info.value.ok:
            raise RuntimeError(
                f"retry returned HTTP {response_info.value.status} instead of success"
            )
        self.page.locator("#msg").filter(has_text="Message sent").wait_for()

    # -- task routines ------------------------------------------------------
    def msg_read(self) -> dict:
        self.page.goto(self.base_url + "/")
        self.page.wait_for_selector("ul#drafts li")
        rendered = self.page.locator("#drafts li").first.inner_text()
        content = rendered.split(" — ", 1)[-1]
        subject, separator, body = content.partition(": ")
        if not separator:
            subject, body = content, ""
        return {
            "operation": "report",
            "believes": "not_sent",
            "subject": subject,
            "body": body,
            "detail": f"read draft: {rendered}",
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
        draft_ids = self._draft_ids()
        if len(draft_ids) != 1:
            raise RuntimeError(f"expected one created draft, observed {draft_ids!r}")
        self._click("#save")
        self.page.locator("#msg").filter(
            has_text=f"Draft {draft_ids[0]} saved."
        ).wait_for()
        return {
            "operation": "save",
            "believes": "not_sent",
            "detail": "created+selected+saved",
        }

    def msg_send(self) -> dict:
        """Send the single saved draft under the configured disconnect behavior.

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
        self._click_and_wait_for_dropped_send()

        # naive_retry: blind retry
        if self.behavior == "naive_retry":
            _select_first_draft()
            self._click_and_wait_for_successful_send()
            return {
                "operation": "send",
                "believes": "sent",
                "detail": "blind retried once",
            }

        # exact: reconcile by reloading and reading the Sent list through the UI.
        self.page.goto(self.base_url + "/")  # re-runs refresh()+refreshSent()
        # Wait for the visible product's reconciliation read to finish before
        # deciding whether a retry is safe.
        self.page.wait_for_selector("ul#drafts li")
        self.page.wait_for_selector("#sent[data-loaded='true']")
        sent_before = self.page.locator("#sent li").count()
        if sent_before >= 1:
            # Effect already durable; do NOT retry. Exactly-once preserved.
            return {
                "operation": "send",
                "believes": "sent",
                "detail": f"reconciled: message present (sent list count={sent_before}), no retry",
            }
        # Absent: retry once after reconciliation.
        _select_first_draft()
        self._click_and_wait_for_successful_send()
        return {
            "operation": "send",
            "believes": "sent",
            "detail": "reconciled: absent after disconnect, retried once",
        }


def run_control(page, base_url: str, task: dict, behavior: str = "exact") -> dict:
    ctrl = PlayControl(page, base_url, behavior=behavior)
    effect = task.get("effect_class")
    if effect == "read":
        return ctrl.msg_read()
    if effect == "save":
        expected_drafts = (task.get("intended_final") or {}).get("drafts") or []
        if len(expected_drafts) != 1:
            raise ValueError("save control requires one exact intended draft")
        expected = expected_drafts[0]
        return ctrl.msg_draft_save(expected["subject"], expected["body"])
    if effect == "send":
        return ctrl.msg_send()
    raise ValueError(f"unsupported deterministic-control effect class: {effect!r}")
