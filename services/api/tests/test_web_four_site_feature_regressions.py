"""四站证据归纳的通用结构回归，不依赖域名、路由或业务标题。"""

import pytest

from services.api.app.application.web_detail_candidate_extraction import (
    web_primary_detail_anchor_is_media,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    web_page_structure_facts,
)
from services.api.app.application.web_primary_content_capture import (
    build_web_primary_content_targets,
)
from services.api.app.application.web_primary_content_exposure import web_primary_content_exposed
from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.test_web_page_classification import Assessor, _capture
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


def _node(selector, tag, parent=None, **extra):
    return dict(selector=selector, tag=tag, parent_selector=parent, visible=True, **extra)


def test_global_header_search_is_not_primary_search_but_main_search_is():
    nodes = [
        _node("#header", "header"),
        _node("#form", "form", "#header", role="search"),
        _node("#input", "input", "#form", type="search"),
        _node("#submit", "button", "#form", type="submit"),
    ]
    structure = web_page_structure_facts({"elements": nodes})
    assert structure.search_input_count == structure.search_trigger_count == 0
    nodes[0]["tag"] = "main"
    structure = web_page_structure_facts({"elements": nodes})
    assert structure.search_input_count == structure.search_trigger_count == 1


def test_list_intro_cannot_absorb_primary_cards_into_an_article():
    nodes = [
        _node("#main", "main", web_subject_text_length=900),
        _node("#intro", "p", "#main", text_length=260),
        *[
            _node(f"#link-{i}", "a", "#main", href=f"https://example.test/item/{i}",
                  repeat_group_size=3)
            for i in range(3)
        ],
    ]
    regions = extract_subject_region_facts({"elements": nodes})
    assert regions.primary_subject_count == regions.primary_article_body_count == 0
    assert regions.repeated_primary_card_count == 3


def test_card_taxonomy_links_do_not_hide_card_roots_or_promote_descriptions_to_article():
    nodes = [_node("#main", "main")]
    for i in range(2):
        card = f"#card-{i}"
        nodes.extend([
            _node(card, "div", "#main", repeat_group_size=2),
            _node(f"#title-{i}", "a", card, href=f"https://example.test/item/{i}"),
            _node(f"#tag-{i}", "a", card,
                  href="https://example.test/tag/topic", class_tokens=["tag"]),
            _node(f"#desc-{i}", "p", card, text_length=136, repeat_group_size=2),
        ])
    regions = extract_subject_region_facts({"elements": nodes})
    assert regions.repeated_primary_card_count == 2
    assert regions.primary_article_body_count == regions.primary_subject_count == 0


@pytest.mark.parametrize("selector,classes", [
    ("#detail-content", []),
    ("#body", ["markdown-body"]),
    ("#body", ["entry-content"]),
])
def test_explicit_div_article_region_preserves_body_and_inline_media(selector, classes):
    nodes = [
        _node("#main", "main"),
        _node(selector, "div", "#main", class_tokens=classes, web_subject_text_length=900),
        _node("#inline", "video", selector, src="https://example.test/sample.mp4"),
    ]
    regions = extract_subject_region_facts({"elements": nodes})
    assert regions.primary_subject_kind == "article"
    assert regions.primary_article_text_length == 900
    assert regions.primary_player_count == 0
    assert regions.inline_article_media_count == 1
    structure = web_page_structure_facts({"elements": nodes})
    assert structure.confirmed_primary_video_resource_count == 0


@pytest.mark.parametrize("token,expected", [
    ("site-drama-card__body", True),
    ("sample-video-card__title", True),
    ("drama-card", True),
    ("non-video-card", False),
    ("non-video-card__body", False),
    ("editorial-card__body", False),
    ("videocardish", False),
])
def test_namespaced_media_card_requires_complete_positive_semantic_tokens(token, expected):
    assert web_primary_detail_anchor_is_media({"ancestor_class_tokens": [token]}) is expected


def test_id_article_remains_executable_and_can_be_exposed_without_center_of_long_body():
    snapshot = {"viewport": {"width": 1920, "height": 1080}, "elements": [
        _node("#detail-content", "div", rect={"x": 480, "y": 200, "width": 960, "height": 9000})
    ]}
    targets = build_web_primary_content_targets(
        capture_id="current", ui_snapshot=snapshot, expected_page_type="content_detail",
    )
    assert targets and targets[0].selector == "#detail-content"
    assert web_primary_content_exposed(
        snapshot, selector=targets[0].selector, page_type="content_detail",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence,review,expected", [
    (.8, False, "content_detail"), (.65, True, "unknown"), (.8, True, "unknown"),
])
async def test_added_article_feature_preserves_model_review_and_confidence(
    confidence, review, expected,
):
    structure = web_page_structure_facts({"elements": [
        _node("#detail-content", "div", web_subject_text_length=900),
    ]})
    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type="content_detail", confidence=confidence, needs_review=review,
            evidence=("正文主体",),
        )), catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=structure))
    assert result.page_type == expected
    assert result.confidence == confidence
    assert result.needs_review is review


@pytest.mark.asyncio
@pytest.mark.parametrize("article_class,recommendation_class", [
    (' class="markdown-body"', ' class="recommendations"'), ("", ""),
    (' class="markdown-body"', ' class="widget-recommend"'),
])
async def test_real_collector_retains_id_article_and_recommendation_boundary_under_many_links(
    article_class, recommendation_class,
):
    from playwright.async_api import async_playwright

    html = (
        '<base href="https://example.test/"><nav>'
        + "".join(f'<a href="/menu/{i}">菜单{i}</a>' for i in range(120))
        + f'</nav><div id="detail-content"{article_class}><p>'
        + "正文" * 450
        + f'</p></div><section{recommendation_class}>'
        + ('<div>更多作品</div>' if 'widget-recommend' in recommendation_class
           else '<h2>相关推荐</h2>')
        + "".join(f'<a href="/item/{i}">作品{i}</a>' for i in range(12))
        + '</section><style>a {display:block;min-height:30px}</style>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html, wait_until="domcontentloaded")
            default = await _collect_ui_snapshot(page)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(str(exc))
    assert not any("web_subject_text_length" in node for node in default["elements"])
    assert any(node["selector"] == "#detail-content" for node in snapshot["elements"])
    regions = extract_subject_region_facts(snapshot)
    assert regions.primary_subject_kind == "article"
    assert regions.primary_article_text_length == 900
    assert regions.repeated_primary_card_count == 0
    assert regions.secondary_recommendation_card_count == 12
