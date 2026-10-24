"""验证高密度 Web 详情页裁剪后仍保留可信主播放器分类事实。"""

from copy import deepcopy

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    web_page_structure_facts,
)
from services.api.app.application.web_primary_content_capture import (
    build_web_primary_content_targets,
)
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.tests.test_web_page_classification import Assessor, _capture


def _truncated_detail_snapshot() -> dict[str, object]:
    """保留 Dys18 实测快照的播放器、裁剪和下方十二张卡的结构形状。"""

    elements: list[dict[str, object]] = []
    for index in range(12):
        left = 432 + index % 6 * 205
        top = 1888 + index // 6 * 180
        card = f"#related > li:nth-of-type({index + 1})"
        elements.extend(
            (
                {
                    "tag": "li",
                    "selector": card,
                    "parent_selector": "#related",
                    "visible": True,
                    "repeat_group_size": 12,
                    "within_repeated_structure": True,
                    "rect": {"x": left, "y": top, "width": 189, "height": 162},
                },
                {
                    "tag": "a",
                    "selector": f"{card} > a",
                    "parent_selector": card,
                    "visible": True,
                    "repeat_group_size": 12,
                    "within_repeated_structure": True,
                    "interactive": True,
                    "href": f"https://example.test/video/detail/{index + 2}",
                    "rect": {"x": left, "y": top, "width": 189, "height": 106},
                },
                {
                    "tag": "img",
                    "selector": f"{card} > a > img",
                    "parent_selector": f"{card} > a",
                    "visible": True,
                    "repeat_group_size": 12,
                    "within_repeated_structure": True,
                    "src": f"https://example.test/poster/{index + 2}.jpg",
                    "rect": {"x": left, "y": top, "width": 189, "height": 106},
                },
            )
        )
    return {
        "url": "https://example.test/video/detail/1",
        "viewport": {"width": 1920, "height": 1080},
        "elements": elements,
        "truncated": True,
        "truncated_reason": "serialized_size_limit",
        "web_detail_candidate_priority_enabled": True,
        "media_truncated": False,
        "web_native_structure": {
            "scope": "current_document_light_dom",
            "native_video_count": 1,
            "complete": False,
            "unobservable_reasons": ["iframe_unobserved"],
        },
        "media": [
            {
                "selector": "#mse > video",
                "tag": "video",
                "bounds": {"x": 432, "y": 247, "width": 1216, "height": 684},
                "viewport_coverage": 0.401,
                "state": {"ready_state": 4, "paused": False, "duration": 550},
                "is_advertisement": False,
                "media_ad_context_complete": True,
                "within_navigation": False,
                "within_web_navigation": False,
                "within_repeated_structure": False,
                "within_footer": False,
                "hit_test": {
                    "center_hits_target": False,
                    "center_within_player_surface": True,
                    "center_obstruction_layer": False,
                },
            }
        ],
    }


def _facts(snapshot: dict[str, object], *, page_role: str = "detail"):
    return web_page_structure_facts(
        snapshot,
        origin="https://example.test",
        current_url="https://example.test/video/detail/1",
        page_role=page_role,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ad_click_overlay", (False, True))
async def test_truncated_detail_media_keeps_single_player_and_excludes_lower_recommendations(
    ad_click_overlay: bool,
):
    snapshot = _truncated_detail_snapshot()
    if ad_click_overlay:
        snapshot["media"][0]["hit_test"] = {
            "center_hits_target": False,
            "center_within_player_surface": False,
            "center_obstruction_layer": False,
            "center_ad_click_overlay": True,
            "center_pointer_interception": True,
        }
    facts = _facts(snapshot)

    assert facts.subject_region_facts.primary_subject_kind == "video"
    assert facts.subject_region_facts.primary_subject_count == 1
    assert facts.subject_region_facts.primary_player_count == 1
    assert facts.subject_region_facts.repeated_primary_card_count == 0
    assert facts.repeated_primary_card_count == 0

    assessment = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_detail", confidence=0.85, evidence=("单一主体视频",)
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            page_role="detail",
            url="https://example.test/video/detail/1",
            facts=facts,
        )
    )
    assert assessment.page_type == "video_detail"
    assert assessment.needs_review is False
    if ad_click_overlay:
        assert build_web_primary_content_targets(
            capture_id="00000000-0000-0000-0000-000000000001",
            ui_snapshot=snapshot,
            expected_page_type="video_detail",
        ) == ()


@pytest.mark.parametrize(
    "case",
    (
        "menu_role",
        "advertisement_video",
        "advertisement_parent_dropped",
        "navigation_parent_dropped",
        "repeated_parent_dropped",
        "center_popup_obscured",
        "opaque_ad_click_overlay",
        "ad_overlay_without_pointer_interception",
        "missing_safety_facts",
        "multiple_videos",
        "missing_native_census",
        "non_light_dom_census",
    ),
)
def test_truncated_media_never_recovers_unproven_primary_player(case: str):
    snapshot = _truncated_detail_snapshot()
    page_role = "detail"
    if case == "menu_role":
        page_role = "menu"
    elif case == "advertisement_video":
        snapshot["media"][0]["selector"] = "#advertisement > video"
        snapshot["elements"].append(
            {
                "tag": "div",
                "selector": "#advertisement",
                "visible": True,
                "is_advertisement": True,
            }
        )
    elif case == "advertisement_parent_dropped":
        snapshot["media"][0]["selector"] = "#video"
        snapshot["media"][0]["is_advertisement"] = True
    elif case == "navigation_parent_dropped":
        snapshot["media"][0]["selector"] = "#video"
        snapshot["media"][0]["within_web_navigation"] = True
    elif case == "repeated_parent_dropped":
        snapshot["media"][0]["selector"] = "#video"
        snapshot["media"][0]["within_repeated_structure"] = True
    elif case == "center_popup_obscured":
        snapshot["media"][0]["hit_test"] = {
            "center_hits_target": False,
            "center_within_player_surface": False,
            "center_obstruction_layer": True,
        }
    elif case == "opaque_ad_click_overlay":
        snapshot["media"][0]["hit_test"] = {
            "center_hits_target": False,
            "center_within_player_surface": False,
            "center_obstruction_layer": False,
            "center_ad_click_overlay": False,
            "center_pointer_interception": True,
        }
    elif case == "ad_overlay_without_pointer_interception":
        snapshot["media"][0]["hit_test"] = {
            "center_hits_target": False,
            "center_within_player_surface": False,
            "center_obstruction_layer": False,
            "center_ad_click_overlay": True,
            "center_pointer_interception": False,
        }
    elif case == "missing_safety_facts":
        snapshot["media"][0].pop("is_advertisement")
        snapshot["media"][0].pop("hit_test")
    elif case == "multiple_videos":
        snapshot["web_native_structure"]["native_video_count"] = 2
        other = deepcopy(snapshot["media"][0])
        other["selector"] = "#second > video"
        snapshot["media"].append(other)
    elif case == "missing_native_census":
        snapshot["web_native_structure"].pop("native_video_count")
    else:
        snapshot["web_native_structure"]["scope"] = "shadow_tree"

    facts = _facts(snapshot, page_role=page_role)
    assert facts.subject_region_facts.primary_player_count == 0
    assert facts.subject_region_facts.repeated_primary_card_count == 12


def test_truncated_media_does_not_hide_cards_beside_player():
    snapshot = _truncated_detail_snapshot()
    for node in snapshot["elements"]:
        node["rect"]["y"] -= 1600

    facts = _facts(snapshot)
    assert facts.subject_region_facts.repeated_primary_card_count == 12
    assert facts.repeated_primary_card_count == 12
