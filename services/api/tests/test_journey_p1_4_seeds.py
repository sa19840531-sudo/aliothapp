"""P1-4 Journey Definition 种子契约：可解析、可哈希、三端 surface。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.api.app.domain.journey import parse_journey_definition

_SEED = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "data"
    / "journey_definitions"
    / "p1_4_journeys.json"
)

_EXPECTED_IDS = frozenset(
    {
        "player-playback-seek-episode",
        "payment-checkout-chain",
        "comment-post-and-refresh",
        "comment-rate-limit-burst",
        "publish-upload-limit",
        "ad-outbound-landing-return",
        "startup-to-home",
        "popup-multi-frame-sequence",
        "share-link-clipboard-or-external",
        "official-group-entrances",
        "comic-page-turn",
    }
)


def _payloads() -> list[dict]:
    raw = json.loads(_SEED.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    return raw


def test_p1_4_journey_seeds_parse_and_cover_catalog() -> None:
    payloads = _payloads()
    ids = {item["definition_id"] for item in payloads}
    assert ids == _EXPECTED_IDS
    for payload in payloads:
        definition = parse_journey_definition(payload)
        assert definition.version == 1
        assert set(definition.surface_kinds) >= {"web", "android", "ios"}
        assert definition.content_hash
        assert definition.steps
        # 稳定哈希：同 payload 重解析不变
        assert definition.content_hash == parse_journey_definition(payload).content_hash


def test_p1_4_player_journey_includes_seek_and_episode_switch() -> None:
    player = next(
        item for item in _payloads() if item["definition_id"] == "player-playback-seek-episode"
    )
    definition = parse_journey_definition(player)
    ops = [
        step.payload.get("operation")
        for step in definition.steps
        if step.kind.value == "action"
    ]
    assert "seek_media" in ops
    assert "back" in ops
    roles = [
        step.payload.get("role")
        for step in definition.steps
        if step.kind.value == "capture"
    ]
    assert "player_scrub_series" in roles


def test_p1_4_payment_and_ad_use_app_evidence_roles() -> None:
    payment = next(
        item for item in _payloads() if item["definition_id"] == "payment-checkout-chain"
    )
    ad = next(
        item for item in _payloads() if item["definition_id"] == "ad-outbound-landing-return"
    )
    pay_def = parse_journey_definition(payment)
    ad_def = parse_journey_definition(ad)
    assert list(pay_def.extensions.get("app_capture_roles") or ()) == [
        "checkout_chain_step"
    ]
    assert "ad_outbound_source" in list(
        ad_def.extensions.get("app_capture_roles") or ()
    )
    assert ad_def.extensions.get("verification_repeats") == 10


@pytest.mark.parametrize("definition_id", sorted(_EXPECTED_IDS))
def test_each_p1_4_journey_has_rule_codes_extension(definition_id: str) -> None:
    payload = next(item for item in _payloads() if item["definition_id"] == definition_id)
    definition = parse_journey_definition(payload)
    codes = definition.extensions.get("rule_codes")
    assert isinstance(codes, (list, tuple)) and len(codes) > 0
