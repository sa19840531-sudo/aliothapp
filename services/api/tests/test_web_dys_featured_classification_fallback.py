"""Dys 精选页同结构快照的模型误判回归。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    WebPageStructureFacts,
    WebReadyPageCapture,
    web_page_structure_facts,
)
from services.api.app.application.web_subject_classification_audit import (
    subject_structure_fingerprint,
)
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.page import load_web_page_type_catalog

_FIXTURE = Path(__file__).parent / "fixtures" / "web_dys18_featured_20260930_structure.json"


class _HomeAssessor:
    def __init__(self, *, confidence: float = 0.75, needs_review: bool = False) -> None:
        self.confidence = confidence
        self.needs_review = needs_review

    async def classify(self, **_: object) -> WebPageAssessment:
        return WebPageAssessment(
            page_type="home",
            confidence=self.confidence,
            evidence=("模型认为多分区是首页",),
            needs_review=self.needs_review,
        )


def _real_featured_capture() -> tuple[WebReadyPageCapture, dict[str, object]]:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    region_payload = payload["subject_region_facts"]
    regions = WebSubjectRegionFacts(
        **{
            **region_payload,
            "player_vendor_tokens": tuple(region_payload["player_vendor_tokens"]),
            "selector_summaries": tuple(
                (role, tuple(values)) for role, values in region_payload["selector_summaries"]
            ),
            "token_summaries": tuple(
                (role, tuple(values)) for role, values in region_payload["token_summaries"]
            ),
        }
    )
    facts_payload = payload["structure_facts"]
    facts = WebPageStructureFacts(
        **{**facts_payload, "dom_tokens": tuple(facts_payload["dom_tokens"])},
        subject_region_facts=regions,
    )
    capture = WebReadyPageCapture(
        capture_id=payload["source_capture_ids"][1],
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="screenshot-artifact",
        dom_artifact_id="dom-artifact",
        visible_text_artifact_id="visible-text-artifact",
        page_role="menu",
        url=payload["url"],
        structure_facts=facts,
    )
    return capture, payload


@pytest.mark.asyncio
async def test_real_dys_featured_snapshot_recovers_video_list_from_rejected_home() -> None:
    capture, payload = _real_featured_capture()
    assert subject_structure_fingerprint(capture.structure_facts.subject_region_facts) == (
        payload["structure_fingerprint"]
    )
    result = await WebPageClassifier(
        assessor=_HomeAssessor(), catalog=load_web_page_type_catalog()
    ).classify(capture)

    assert result.page_type == "video_list"
    assert result.confidence >= 0.7
    assert result.decision_source == "deterministic"
    assert result.model_result["page_type"] == "home"
    assert result.observed_page_type == "home"
    assert result.raw_page_type == "home"
    assert result.proposed_page_type == "video_list"
    assert result.deterministic_result["reason_code"] == "WEB_NONROOT_HOME_VIDEO_LIST_FALLBACK"
    assert result.deterministic_result["rules"]["home"]["missing_facts"] == (
        "is_canonical_root_url:eq:True",
    )
    assert result.deterministic_result["rules"]["video_list"]["required_satisfied"] is True
    assert result.structure_fingerprint == payload["structure_fingerprint"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["primary_player", "ad_obstruction", "no_media", "mixed_content", "search"]
)
async def test_nonroot_home_fallback_never_promotes_detail_ad_or_nonvideo(
    kind: str,
) -> None:
    capture, _ = _real_featured_capture()
    facts = capture.structure_facts
    if kind == "primary_player":
        facts = replace(
            facts,
            player_root_count=1,
            confirmed_primary_video_resource_count=1,
            subject_region_facts=replace(
                facts.subject_region_facts,
                primary_subject_count=1,
                primary_subject_kind="video",
                primary_player_count=1,
            ),
        )
    elif kind == "ad_obstruction":
        facts = replace(facts, top_level_obstruction_count=1)
    elif kind == "no_media":
        facts = replace(facts, media_card_count=0, dom_tokens=())
    elif kind == "mixed_content":
        facts = replace(facts, media_card_count=2)
    else:
        facts = replace(facts, search_trigger_count=1)
    result = await WebPageClassifier(
        assessor=_HomeAssessor(), catalog=load_web_page_type_catalog()
    ).classify(replace(capture, structure_facts=facts))

    assert result.page_type != "video_list"
    assert "WEB_NONROOT_HOME_VIDEO_LIST_FALLBACK" not in result.reason_codes


@pytest.mark.asyncio
async def test_nonroot_home_fallback_rejects_generic_image_cover_cards() -> None:
    capture, _ = _real_featured_capture()
    facts = replace(
        capture.structure_facts,
        # 图文卡片仍会被通用媒体卡统计为 18，不能凭 cover/poster 签发视频列表。
        dom_tokens=("cover", "poster", "thumbnail", "article-item"),
        video_specific_card_count=0,
    )
    result = await WebPageClassifier(
        assessor=_HomeAssessor(), catalog=load_web_page_type_catalog()
    ).classify(replace(capture, structure_facts=facts))

    assert result.page_type == "unknown"
    assert "WEB_NONROOT_HOME_VIDEO_LIST_FALLBACK" not in result.reason_codes


def test_video_specific_card_count_is_scoped_to_trusted_detail_cards() -> None:
    def card(index: int, marker: str) -> dict[str, object]:
        return {
            "selector": f"#card-{index} a",
            "tag": "a",
            "role": "link",
            "text": f"内容 {index}",
            "href": f"/detail/{index}",
            "rect": {"x": index * 200, "y": 200, "width": 180, "height": 150},
            "interactive": True,
            "visible": True,
            "within_repeated_structure": True,
            "ancestor_class_tokens": [marker],
        }

    snapshot = {
        "capture_id": "capture-image-list",
        "url": "https://example.test/featured",
        "elements": [
            card(1, "poster"),
            card(2, "thumbnail"),
            {"selector": "#sidebar .video-item", "tag": "div", "visible": True,
             "class_tokens": ["video-item"]},
        ],
    }
    image_facts = web_page_structure_facts(
        snapshot, origin="https://example.test", current_url=snapshot["url"]
    )
    assert image_facts.media_card_count == 2
    assert image_facts.video_specific_card_count == 0

    snapshot["elements"] = [card(1, "video-item"), card(2, "video-item")]
    video_facts = web_page_structure_facts(
        snapshot, origin="https://example.test", current_url=snapshot["url"]
    )
    assert video_facts.video_specific_card_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence,needs_review", [(0.55, False), (0.75, True)])
async def test_nonroot_home_fallback_rejects_weak_or_uncertain_model(
    confidence: float, needs_review: bool
) -> None:
    capture, _ = _real_featured_capture()
    result = await WebPageClassifier(
        assessor=_HomeAssessor(confidence=confidence, needs_review=needs_review),
        catalog=load_web_page_type_catalog(),
    ).classify(capture)

    assert result.page_type == "unknown"
    assert result.model_result["page_type"] == "home"


@pytest.mark.asyncio
async def test_canonical_root_still_uses_home_rule() -> None:
    capture, _ = _real_featured_capture()
    result = await WebPageClassifier(
        assessor=_HomeAssessor(), catalog=load_web_page_type_catalog()
    ).classify(replace(capture, url="https://dys18.com/"))

    assert result.page_type == "home"
    assert result.decision_source == "model"
    assert "WEB_NONROOT_HOME_VIDEO_LIST_FALLBACK" not in result.reason_codes
