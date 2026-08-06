"""Strict, auditable final-answer claims for BrowserTransactionBench.

Effects come exclusively from the SQLite oracle. A claim records the agent's
separate belief and (for read tasks) its reported content. Canonical evaluation
never infers a belief from prose; the legacy heuristic is opt-in only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal, Mapping

PARSER_VERSION = "btb-claim-v1"

CanonicalBelief = Literal["sent", "not_sent", "unknown"]
Belief = Literal["sent", "not_sent", "unknown", "malformed", "absent"]
ClaimSource = Literal["structured", "control", "heuristic", "malformed", "absent"]
_CANONICAL_BELIEFS = frozenset(("sent", "not_sent", "unknown"))
_CANONICAL_KEYS = frozenset(("believes", "subject", "body"))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


@dataclass(frozen=True)
class Claim:
    """Normalized final answer with the complete source text retained."""

    believes: Belief
    sourced_from: ClaimSource
    raw: str
    subject: str | None = None
    body: str | None = None
    parser_version: str = PARSER_VERSION

    @property
    def claimed_send(self) -> bool | None:
        """Compatibility projection that does not collapse uncertainty."""
        if self.believes == "sent":
            return True
        if self.believes == "not_sent":
            return False
        return None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["claimed_send"] = self.claimed_send
        return result


def _canonical_fields(obj: object) -> tuple[CanonicalBelief, str | None, str | None] | None:
    if not isinstance(obj, dict) or set(obj) - _CANONICAL_KEYS:
        return None
    belief = obj.get("believes")
    if belief not in _CANONICAL_BELIEFS:
        return None
    subject = obj.get("subject")
    body = obj.get("body")
    if subject is not None and not isinstance(subject, str):
        return None
    if body is not None and not isinstance(body, str):
        return None
    return belief, subject, body  # type: ignore[return-value]


def _structured_claim(
    text: str,
) -> tuple[CanonicalBelief, str | None, str | None] | None:
    """Parse one exact JSON object, allowing surrounding whitespace only.

    Agent transcripts and prompt examples are evidence, not final answers.  They
    must never be scanned for a convenient claim because doing so can silently
    turn an example from the prompt into the agent's reported belief.
    """
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return None
    return _canonical_fields(obj)


def extract_structured(text: str) -> CanonicalBelief | None:
    """Compatibility helper returning a strict structured belief."""
    claim = _structured_claim(text or "")
    return claim[0] if claim else None


def heuristic_belief(text: str) -> CanonicalBelief:
    """Legacy prose interpretation, available only through ``allow_legacy``."""
    lowered = (text or "").lower()
    uncertain = any(
        phrase in lowered
        for phrase in ("unsure", "unknown", "cannot confirm", "could not confirm")
    )
    if uncertain:
        return "unknown"
    negative = any(
        phrase in lowered
        for phrase in ("did not send", "could not send", "unable to send", "not sent", "failed")
    )
    if negative:
        return "not_sent"
    if "sent" in lowered or "message sent" in lowered or "submitted" in lowered:
        return "sent"
    return "unknown"


def parse_claim(raw: str | None, *, allow_legacy: bool = False) -> Claim:
    """Parse one exact canonical JSON object from an agent's final answer.

    Empty output is ``absent``. Non-empty output without a valid canonical object
    is ``malformed`` unless the caller explicitly requests legacy prose parsing.
    The exact untruncated input is retained in every case.
    """
    text = "" if raw is None else raw
    if not text.strip():
        return Claim(believes="absent", sourced_from="absent", raw=text)

    structured = _structured_claim(text)
    if structured is not None:
        belief, subject, body = structured
        return Claim(
            believes=belief,
            sourced_from="structured",
            raw=text,
            subject=subject,
            body=body,
        )

    if allow_legacy:
        return Claim(
            believes=heuristic_belief(text),
            sourced_from="heuristic",
            raw=text,
        )
    return Claim(believes="malformed", sourced_from="malformed", raw=text)


def claim_from_mapping(payload: Mapping[str, object]) -> Claim:
    """Convert a deterministic control's auditable report into a Claim."""
    belief = payload.get("believes")
    if belief not in _CANONICAL_BELIEFS:
        raise ValueError("control claim must set believes to sent, not_sent, or unknown")
    subject = payload.get("subject")
    body = payload.get("body")
    if subject is not None and not isinstance(subject, str):
        raise TypeError("control claim subject must be a string")
    if body is not None and not isinstance(body, str):
        raise TypeError("control claim body must be a string")
    raw = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    return Claim(
        believes=belief,  # type: ignore[arg-type]
        sourced_from="control",
        raw=raw,
        subject=subject,
        body=body,
    )
