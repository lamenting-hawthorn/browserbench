"""Tests for strict, non-collapsing final-answer claim parsing."""

from __future__ import annotations

from btb.oracle import claim as claim_mod


def test_structured_sent() -> None:
    raw = '{"believes":"sent"}'
    claim = claim_mod.parse_claim(raw)
    assert claim.believes == "sent"
    assert claim.claimed_send is True
    assert claim.sourced_from == "structured"
    assert claim.raw == raw
    assert claim.parser_version == claim_mod.PARSER_VERSION


def test_structured_not_sent() -> None:
    claim = claim_mod.parse_claim('{"believes":"not_sent"}')
    assert claim.believes == "not_sent"
    assert claim.claimed_send is False


def test_structured_unknown_is_not_collapsed_to_false() -> None:
    claim = claim_mod.parse_claim('{"believes":"unknown"}')
    assert claim.believes == "unknown"
    assert claim.claimed_send is None


def test_absent_and_malformed_are_distinct() -> None:
    absent = claim_mod.parse_claim("  ")
    malformed = claim_mod.parse_claim("I think it probably sent.")
    assert absent.believes == "absent"
    assert absent.sourced_from == "absent"
    assert malformed.believes == "malformed"
    assert malformed.sourced_from == "malformed"


def test_invalid_structured_value_is_malformed() -> None:
    claim = claim_mod.parse_claim('{"believes":"maybe"}')
    assert claim.believes == "malformed"


def test_canonical_parser_rejects_transcript_wrapped_claims() -> None:
    raw = (
        'prompt example {"believes":"sent"}\n'
        'final answer {"believes":"unknown","subject":"S","body":"B"}'
    )
    claim = claim_mod.parse_claim(raw)
    assert claim.believes == "malformed"
    assert claim.sourced_from == "malformed"


def test_canonical_parser_rejects_trailing_prose() -> None:
    claim = claim_mod.parse_claim('{"believes":"sent"} done')
    assert claim.believes == "malformed"


def test_canonical_parser_accepts_surrounding_whitespace_only() -> None:
    claim = claim_mod.parse_claim(' \n {"believes":"unknown"}\t')
    assert claim.believes == "unknown"
    assert claim.sourced_from == "structured"


def test_json_strings_may_contain_braces() -> None:
    claim = claim_mod.parse_claim(
        '{"believes":"not_sent","subject":"Plan {draft}","body":"Body } text"}'
    )
    assert claim.subject == "Plan {draft}"
    assert claim.body == "Body } text"


def test_extra_structured_keys_are_rejected() -> None:
    claim = claim_mod.parse_claim('{"believes":"sent","confidence":0.9}')
    assert claim.believes == "malformed"


def test_duplicate_json_keys_are_rejected_instead_of_using_the_last_value() -> None:
    claim = claim_mod.parse_claim(
        '{"believes":"sent","believes":"unknown"}'
    )
    assert claim.believes == "malformed"


def test_nonstandard_json_constants_are_rejected() -> None:
    claim = claim_mod.parse_claim('{"believes":"sent","subject":NaN}')
    assert claim.believes == "malformed"


def test_legacy_heuristic_is_explicit_opt_in() -> None:
    canonical = claim_mod.parse_claim("The message was sent successfully.")
    legacy = claim_mod.parse_claim(
        "The message was sent successfully.", allow_legacy=True
    )
    assert canonical.believes == "malformed"
    assert legacy.believes == "sent"
    assert legacy.sourced_from == "heuristic"


def test_legacy_uncertainty_remains_unknown() -> None:
    claim = claim_mod.parse_claim(
        "I could not confirm whether the message was sent.", allow_legacy=True
    )
    assert claim.believes == "unknown"
    assert claim.claimed_send is None


def test_control_mapping_retains_report_fields() -> None:
    claim = claim_mod.claim_from_mapping(
        {
            "operation": "report",
            "believes": "not_sent",
            "subject": "Quarterly Plan",
            "body": "Align hiring with roadmap.",
        }
    )
    assert claim.sourced_from == "control"
    assert claim.subject == "Quarterly Plan"
    assert claim.body == "Align hiring with roadmap."
    assert claim.to_dict()["claimed_send"] is False
