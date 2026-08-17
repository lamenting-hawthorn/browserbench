"""Frozen semantic policy identities for Browser Use 0.13.6.

This module intentionally contains only stable data and standard-library code so
the independent receipt validator can check an observed runtime policy without
importing Browser Use.
"""

from __future__ import annotations

from collections.abc import Mapping

BROWSER_USE_VERSION = "0.13.6"

FIXTURE_ACTION_CALLBACKS = {
    "navigate": {
        "identity": "btb.fixture_only_navigate.v1",
        "behavior": (
            "pre_dispatch_exact_http_origin_validation;"
            "post_dispatch_detect_recover_and_stop_on_unverified_outcome"
        ),
    },
    "go_back": {
        "identity": "btb.fixture_only_go_back.v1",
        "behavior": (
            "post_dispatch_observe_fixture_origin;"
            "recover_and_stop_on_unverified_outcome"
        ),
    },
    "switch": {
        "identity": "btb.fixture_only_switch.v1",
        "behavior": (
            "post_dispatch_observe_fixture_origin;"
            "recover_and_stop_on_unverified_outcome"
        ),
    },
    "close": {
        "identity": "btb.fixture_only_close.v1",
        "behavior": (
            "post_dispatch_observe_fixture_origin;"
            "recover_and_stop_on_unverified_outcome"
        ),
    },
    "search": {
        "identity": "btb.fixture_only_search_rejection.v1",
        "behavior": "reject_external_web_search_without_dispatch_or_navigation",
    },
}

# SHA-256 of manifest.canonical_json_bytes() for each public action's
# model_json_schema().  These values come from a local Browser Use 0.13.6
# post-Agent audit; they are semantic schema identities, not callable hashes.
_LEGACY_ACTION_SCHEMA_SHA256 = {
    "click": "7cbe10aead7ebe1ba937f0cbca23802092f33b394571597d81a27c828307612a",
    "done": "6263628abcd8540f733d491fa500ddc4833b90876892221144e075b943286ab0",
    "dropdown_options": "fdb4e1397983baa1fa79b5d1513c8ce022191aaaf38732e949807890989a9b80",
    "find_elements": "35761caa4a3a194709ff76e427e7744343f1fef07a5529aaaf30ff2576e74445",
    "find_text": "19d37726addd2e9d8c64284854fad48d6a031278cef85106642a7072359021d5",
    "go_back": "8dc82f58d2408f4f1dd73e05cfb0a708f8f71950339b2ef8206363ddb8ad64d7",
    "input": "4c19a782df561f32068b44fc33427721d1c44526cbed75194d71ccd1f52c519a",
    "navigate": "9d546ca1820a9652ee395c9c3f8a919a7d65a21884c39633c90e384a7d4f34f1",
    "scroll": "8b48838be4411cf30d0fffe05133c85947545c3cc7811f413bd0be31f5906fdd",
    "search_page": "d673c683ff03800ea3c7c00d82d6088bb578db9b62ad4d982957e46ccfc28edd",
    "select_dropdown": "07c00dd2c14d865def72100eabe9b63b8746eeb7f70365ffe892ba1960aa5364",
    "wait": "92a6b06e94fe8956a8b8c25aea24b73d9c77e18026c0da4be84c492c29204db5",
}
_FULL_ACTION_SCHEMA_SHA256 = {
    **_LEGACY_ACTION_SCHEMA_SHA256,
    "close": "98554e61f6a1f57db1387a3c8577545ba8cd1815742bbfebb58c363989ab68f5",
    "extract": "700a7011cfa7e72ff8bab4cb1fca171943473d9ba5c9a813933cbdd78e830d7b",
    "screenshot": "3de84847c0f4fd88804675fa8d11b3b230983b5c8a45c639a39348483a67957c",
    "search": "23ddb77b677b91e7e3ca0264b33320e0176144ee289375e896989374ad159a81",
    "send_keys": "5ccbe74d16259ad01f63f53d961108cc2c4d5803eb0f357119fbe7804d04f448",
    "switch": "af9af9b766d35d0cd024d8efcaab2dc065062a505c037cb089fa09cfe70ead14",
}

BROWSER_USE_SCHEMA_DIGESTS = {
    "browser-use:deepseek/deepseek-chat": {
        "action_model": "e7d672c5aa72440b0b449fe7a116036cfbf44078817ccccee577df5ebd02e213",
        "actions": _LEGACY_ACTION_SCHEMA_SHA256,
    },
    "browser-use-full:anthropic/claude-sonnet-4-0": {
        "action_model": "93eb8d59135167393dc0d844b20acd0dbe423c3fcbdedea9153ccbd948fdb090",
        "actions": {
            **_FULL_ACTION_SCHEMA_SHA256,
            "click": "5ca9b497f3dc331b700fe866379afc89eb15e34abb0881babca24db38125ef6c",
        },
    },
    "browser-use-full:openai/gpt-4.1-mini": {
        "action_model": "5799a2cc2cb6df9eef7de97fa513eb3010d2a2ded706a8786b629ec2647e55c2",
        "actions": _FULL_ACTION_SCHEMA_SHA256,
    },
}


def schema_condition(name: str, provider: str, model: str) -> str:
    """Return the exact frozen runtime condition identity."""

    return f"{name}:{provider}/{model}"


def schema_digests_for(
    name: str,
    provider: str,
    model: str,
) -> Mapping[str, object]:
    """Return the frozen semantic schema digests or fail closed."""

    condition = schema_condition(name, provider, model)
    try:
        return BROWSER_USE_SCHEMA_DIGESTS[condition]
    except KeyError as exc:
        raise ValueError(
            f"Browser Use schema condition is not frozen: {condition}"
        ) from exc
