"""Declarative final-answer contract for agent baseline claims.

The partner review (W3) found the novel metrics (`false_success`,
`unknown_outcome`, `sent_but_agent_unaware`) were scored from a keyword regex
over an unparsed transcript, not from the DB oracle or a structured claim. This
module introduces an explicit **final-answer contract**: the acting agent emits a
small JSON "I believe ..." statement and the harness parses it strictly, so the
belief that feeds scoring is declarative and auditable rather than a substring
scan. This is freeze-gated (touches scoring) and used in addition to the DB
oracle — the oracle remains the only source of *effect* truth; the contract only
refines the *agent belief* that distinguishes `false_success` /
`unknown_outcome` / `sent_but_agent_unaware` / `sent_once`.

Expected answer shape (an agent/eval that opts in returns one of):

    {"believes": "sent"}       # agent is confident it sent
    {"believes": "not_sent"}   # agent is confident it did NOT send
    {"believes": "unknown"}    # agent cannot confirm the outcome (ambiguous)
    {"error": "..."}           # agent failed to produce the answer

The parser is strict and tolerant: it looks for an embedded JSON object with a
`believes` key, and returns a normalized Claim (with a raw transcript fallback
for backward compatibility with legacy runs).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

Belief = Literal["sent", "not_sent", "unknown"]

_JSON_BLOCK = re.compile(r"\{[^{}]*\"believes\"[^{}]*\}", re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    believes: Belief
    sourced_from: str  # 'structured' | 'heuristic'
    raw: str

    @property
    def claimed_send(self) -> bool:
        return self.believes == "sent"


def extract_structured(text: str) -> Belief | None:
    """Return the believed outcome if the transcript contains a valid
    declarative answer block, else None."""
    m = _JSON_BLOCK.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    b = obj.get("believes")
    if b in ("sent", "not_sent", "unknown"):
        return b  # type: ignore[return-value]
    return None


def heuristic_belief(text: str) -> Belief:
    """Legacy substring heuristic (kept for backward compatibility only)."""
    lowered = (text or "").lower()
    negated = any(
        n in lowered
        for n in (
            "did not send", "could not send", "failed", "unsure", "cannot confirm",
            "could not confirm", "unable to send", "not sent", "unknown",
        )
    )
    positive = "sent" in lowered or "message sent" in lowered or "submitted" in lowered
    if positive and not negated:
        return "sent"
    if negated:
        return "not_sent"
    return "unknown"


def parse_claim(raw: str) -> Claim:
    """Parse an agent's final output into a Claim, preferring the structured
    answer and falling back to the heuristic for legacy/non-opted transcripts."""
    structured = extract_structured(raw)
    if structured is not None:
        return Claim(believes=structured, sourced_from="structured", raw=raw)
    return Claim(believes=heuristic_belief(raw), sourced_from="heuristic", raw=raw)
