"""离线复现页头卡片误归属与 Web 屏外媒体裁剪，不调用浏览器或数据库。"""

import hashlib
import json
from copy import deepcopy

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    web_page_structure_facts,
)
from services.api.app.application.web_primary_content_capture import (
    build_web_primary_content_targets,
    resolve_web_primary_content_selector,
)
from services.api.app.application.web_subject_region_extraction import extract_regions
from services.api.app.application.web_subject_region_scopes import SubjectRegions
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.app.infrastructure.browser.page_capture import (
    MAX_UI_SNAPSHOT_BYTES,
    _sanitize_and_bound_ui_snapshot,
)
from services.api.tests.test_web_page_classification import Assessor, _capture


def _article_with_site_header(*, ancestor_only=False):
    elements = [
        {"tag": "body", "selector": "#document", "visible": True},
        {"tag": "main", "selector": "#main", "parent_selector": "#document", "visible": True},
        {"tag": "article", "selector": "#article", "parent_selector": "#main", "visible": True},
        {"tag": "header", "selector": "#article-header", "parent_selector": "#article",
         "visible": True},
        {"tag": "p", "selector": "#article-introduction", "parent_selector": "#article-header",
         "text": "正文" * 100, "visible": True},
    ]
    if not ancestor_only:
        elements.append({"tag": "header", "selector": "#site-header",
                         "parent_selector": "#document", "visible": True})
    for index in range(2):
        parent = f"#header-control-{index}"
        if not ancestor_only:
            elements.append({"tag": "div", "selector": parent,
                             "parent_selector": "#site-header", "visible": True,
                             "repeat_group_size": 2})
        elements.append({
            "tag": "a", "selector": f"#header-link-{index}", "parent_selector": parent,
            "href": f"https://example.test/navigation/{index}", "visible": True,
            "within_navigation": True, "within_web_navigation": False,
            "repeat_group_size": 2,
            "web_ancestor_structure": [
                {"tag": "div", "selector": parent, "repeat_group_size": 2},
                {"tag": "header", "selector": "#site-header"},
                {"tag": "body", "selector": "#document"},
            ],
            "web_ancestor_structure_complete": True,
        })
    return {"elements": elements}


@pytest.mark.parametrize("ancestor_only", [False, True])
def test_site_header_controls_are_not_primary_content_cards(ancestor_only):
    facts = extract_regions(_article_with_site_header(ancestor_only=ancestor_only))
    assert facts.primary_subject_kind == "article"
    assert facts.primary_subject_count == 1
    assert facts.primary_article_text_length == 200
    assert facts.repeated_primary_card_count == 0


def test_article_internal_header_remains_in_primary_body_scope():
    snapshot = _article_with_site_header()
    regions = SubjectRegions(snapshot["elements"])
    assert "#article-header" in regions.primary
    assert "#article-introduction" in regions.primary
    facts = extract_regions(snapshot)
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length == 200


def test_real_main_list_cards_remain_primary_content_cards():
    snapshot = _article_with_site_header()
    elements = snapshot["elements"]
    elements = [e for e in elements if not e["selector"].startswith("#article")]
    elements = [e for e in elements if not e["selector"].startswith(("#site-", "#header-"))]
    elements.extend({
        "tag": "a", "selector": f"#card-{index}", "parent_selector": "#main",
        "href": f"https://example.test/entry/{index}", "visible": True,
        "repeat_group_size": 2,
    } for index in range(2))
    facts = extract_regions({"elements": elements})
    assert facts.repeated_primary_card_count == 2
    assert facts.primary_subject_count == 0


@pytest.mark.parametrize("with_topics", [True, False], ids=[
    "huangguo-duanju-video-7311", "huangguo-manju-video-7303",
])
def test_repeated_detail_controls_are_not_primary_list_cards(with_topics):
    """作者入口、播放器操作和专题标签不是并列视频卡片。"""
    elements = [
        {"tag": "main", "selector": "#main", "visible": True,
         "rect": {"x": 0, "y": 78, "width": 1920, "height": 3480.13}},
        {"tag": "video", "selector": "#player", "parent_selector": "#main",
         "visible": True, "src": "blob:https://example.test/media",
         "media": {"ready_state": 4},
         "rect": {"x": 96, "y": 183, "width": 1308, "height": 460.41}},
        {"tag": "div", "selector": "#author", "parent_selector": "#main",
         "visible": True, "repeat_group_size": 2},
        {"tag": "a", "selector": "#author-link", "parent_selector": "#author",
         "visible": True, "href": "https://example.test/author/1"},
        {"tag": "img", "selector": "#author-icon", "parent_selector": "#author",
         "visible": True, "src": "https://example.test/icon.png"},
        {"tag": "a", "selector": "#channel-action", "parent_selector": "#main",
         "visible": True, "class_tokens": ["hg-web-play__action"],
         "repeat_group_size": 3, "href": "https://example.test/chan/1"},
        {"tag": "a", "selector": "#next-episode", "parent_selector": "#main",
         "visible": True, "class_tokens": ["hg-web-play__action"],
         "repeat_group_size": 3, "href": "https://example.test/video/2"},
    ]
    if with_topics:
        elements.extend({
            "tag": "a", "selector": f"#topic-{index}", "parent_selector": "#main",
            "visible": True, "class_tokens": ["hg-web-detail__topic"],
            "repeat_group_size": 2, "href": f"https://example.test/topics/{index}",
        } for index in range(2))

    facts = extract_regions({"elements": elements})
    assert facts.primary_player_count == 1
    assert facts.repeated_primary_card_count == 0


def test_repeated_media_grid_cards_remain_primary_list_evidence():
    """排除详情控件不能吞掉真正的重复视频卡片。"""
    elements = [
        {"tag": "main", "selector": "#main", "visible": True,
         "rect": {"x": 0, "y": 78, "width": 1920, "height": 3480.13}},
        {"tag": "video", "selector": "#player", "parent_selector": "#main",
         "visible": True, "src": "blob:https://example.test/media",
         "media": {"ready_state": 4},
         "rect": {"x": 96, "y": 183, "width": 1308, "height": 460.41}},
    ]
    for index in range(2):
        selector = f"#video-card-{index}"
        elements.extend([
            {"tag": "a", "selector": selector, "parent_selector": "#main",
             "visible": True, "class_tokens": ["hg-card"],
             "repeat_group_size": 2,
             "href": f"https://example.test/video/{index}"},
            {"tag": "img", "selector": f"#poster-{index}",
             "parent_selector": selector, "visible": True,
             "src": f"https://example.test/poster/{index}.png",
             "rect": {"x": 20 + index * 300, "y": 1100, "width": 240, "height": 135}},
        ])

    facts = extract_regions({"elements": elements})
    assert facts.repeated_primary_card_count == 2
    assert facts.primary_subject_count == 1


@pytest.mark.asyncio
async def test_header_scope_fix_does_not_clear_model_review_or_raise_confidence():
    facts = web_page_structure_facts(_article_with_site_header())
    assessment = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type="content_detail", confidence=.6, evidence=("主体证据待复核",),
            needs_review=True,
        )), catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))
    assert assessment.page_type == "unknown"
    assert assessment.needs_review is True
    assert assessment.confidence == .6


def _crowded_snapshot(*, web_marker=None):
    video = {
        "selector": "#primary-video", "tag": "video", "visible": True,
        "src": "https://media.example.test/stream.mp4", "class_tokens": [],
        "is_advertisement": False, "within_web_navigation": False,
        "rect": {"x": 638, "y": 3585, "width": 644, "height": 362},
    }
    snapshot = {
        "url": "https://example.test/detail/1", "viewport": {"width": 1920, "height": 1080},
        "elements": [*({
            "selector": f"#link-{index}", "tag": "a", "visible": True,
            "class_tokens": [], "text": "普通候选" * 2000,
            "href": f"https://example.test/entry/{index}",
            "rect": {"x": 1, "y": 100, "width": 100, "height": 20},
        } for index in range(300)), video],
        "media": [{"selector": video["selector"], "tag": "video", "bounds": video["rect"],
                   "viewport_coverage": 0, "state": {"ready_state": 4}}],
    }
    if web_marker is not None:
        snapshot["web_detail_candidate_priority_enabled"] = web_marker
    return snapshot


def test_web_offscreen_native_video_survives_bounding_as_a_resolvable_real_element():
    snapshot = _sanitize_and_bound_ui_snapshot(_crowded_snapshot(web_marker=True))
    assert snapshot["truncated_reason"] == "serialized_size_limit"
    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()) <= (
        MAX_UI_SNAPSHOT_BYTES
    )
    assert any(media["tag"] == "video" for media in snapshot["media"])
    targets = build_web_primary_content_targets(
        capture_id="capture", ui_snapshot=snapshot, expected_page_type="video_detail",
    )
    assert len(targets) == 1
    resolved = resolve_web_primary_content_selector(
        capture_id="capture", ui_snapshot=snapshot, expected_page_type="video_detail",
        candidate_id=targets[0].candidate_id,
    )
    assert resolved is not None and resolved[0] == "#primary-video"
    assert any(item["selector"] == resolved[0] for item in snapshot["elements"])


@pytest.mark.parametrize("web_marker", [None, False])
def test_non_web_bounding_keeps_frozen_legacy_result(web_marker):
    snapshot = _sanitize_and_bound_ui_snapshot(_crowded_snapshot(web_marker=web_marker))
    assert snapshot["truncated_reason"] == "serialized_size_limit"
    assert all(item["tag"] != "video" for item in snapshot["elements"])
    # 标志缺省与显式关闭均沿用旧输出；黄金摘要覆盖元素顺序、裁剪计数与元数据。
    snapshot.pop("web_detail_candidate_priority_enabled", None)
    fingerprint = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert fingerprint == "198339c9bf99431026033e27aa01d040e83ad1e724283d3a3fbafc549dbe7894"


def test_media_summary_alone_cannot_create_an_executable_primary_target():
    snapshot = deepcopy(_crowded_snapshot(web_marker=True))
    snapshot["elements"] = []
    assert not build_web_primary_content_targets(
        capture_id="capture", ui_snapshot=snapshot, expected_page_type="video_detail",
    )
