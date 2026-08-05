"""Tests for the declarative final-answer claim parser (btb.oracle.claim)."""

from __future__ import annotations

from btb.oracle import claim as claim_mod


def test_structured_sent() -> None:
    raw = 'Some transcript noise. {"believes": "sent"} done.'
    c = claim_mod.parse_claim(raw)
    assert c.believes == "sent"
    assert c.claimed_send is True
    assert c.sourced_from == "structured"


def test_structured_not_sent() -> None:
    c = claim_mod.parse_claim('I could not confirm. {"believes": "not_sent"}')
    assert c.believes == "not_sent"
    assert c.claimed_send is False


def test_structured_unknown() -> None:
    c = claim_mod.parse_claim('{"believes": "unknown"} the response was cut off')
    assert c.believes == "unknown"
    assert c.claimed_send is False


def test_heuristic_fallback_sent() -> None:
    # No structured block -> fall back to keyword heuristic.
    c = claim_mod.parse_claim("I clicked send and the message was sent successfully.")
    assert c.sourced_from == "heuristic"
    assert c.believes == "sent"


def test_heuristic_fallback_unknown_on_negation() -> None:
    c = claim_mod.parse_claim("I could not confirm whether the message was sent.")
    assert c.believes == "not_sent"  # negation suppresses the positive match


def test_invalid_believes_ignored_falls_back() -> None:
    # A block with an invalid 'believes' value is not treated as structured.
    raw = '{"believes": "maybe"} I sent it.'
    c = claim_mod.parse_claim(raw)
    assert c.sourced_from == "heuristic"
