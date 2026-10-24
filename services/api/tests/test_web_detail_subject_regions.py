"""通用主体分区必须保留主媒体、正文与次要卡片之间的边界。"""

from dataclasses import asdict

import pytest

from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts
from services.api.tests.web_subject_fixture_support import load_fixture, snapshot_from_html


def test_player_inside_article_remains_primary_and_recommendations_are_secondary():
    facts = extract_subject_region_facts(load_fixture("video_primary_with_recommendations.html"))
    assert facts.primary_subject_kind == "video"
    assert facts.primary_player_count == 1
    assert facts.repeated_primary_card_count == 0
    assert facts.secondary_recommendation_card_count == 12


@pytest.mark.parametrize(
    "duration",
    [192.13, 496.02],
    ids=["huangguo-duanju-video-7311", "huangguo-manju-video-7303"],
)
def test_ready_blob_video_in_long_main_is_still_primary_subject(duration):
    """长页下方内容不能稀释首屏已就绪的独占播放器。"""
    facts = extract_subject_region_facts({
        "elements": [
            {"tag": "main", "selector": "#main", "visible": True,
             "rect": {"x": 0, "y": 78, "width": 1920, "height": 3480.13}},
            {"tag": "section", "selector": "#player-section",
             "parent_selector": "#main", "visible": True,
             "rect": {"x": 96, "y": 183, "width": 1308, "height": 460.41}},
            {"tag": "div", "selector": "#player-mount",
             "parent_selector": "#player-section", "visible": True,
             "rect": {"x": 96, "y": 183, "width": 1308, "height": 460.41}},
            {"tag": "video", "selector": "#primary-video",
             "parent_selector": "#player-mount", "visible": True,
             "src": "blob:https://example.test/ready-media",
             "media": {"ready_state": 4, "duration": duration},
             "rect": {"x": 96, "y": 183, "width": 1308, "height": 460.41}},
        ]
    })
    assert facts.primary_subject_kind == "video"
    assert facts.primary_subject_count == 1
    assert facts.primary_player_count == 1


@pytest.mark.parametrize(
    "trusted_length,duration",
    [(502, 192.13), (474, 496.02)],
    ids=["huangguo-duanju-video-7311", "huangguo-manju-video-7303"],
)
def test_dominant_video_does_not_turn_generic_main_counter_into_article(
    trusted_length, duration,
):
    """整页正文计数没有独立段落佐证时，不能覆盖独占播放器。"""
    facts = extract_subject_region_facts({
        "elements": [
            {"tag": "main", "selector": "#main", "visible": True,
             "text_length": 1980, "web_subject_text_length": trusted_length,
             "rect": {"x": 0, "y": 78, "width": 1920, "height": 3480.13}},
            {"tag": "video", "selector": "#primary-video", "parent_selector": "#main",
             "visible": True, "src": "blob:https://example.test/ready-media",
             "media": {"ready_state": 4, "duration": duration},
             "rect": {"x": 96, "y": 183, "width": 1308, "height": 460.41}},
        ]
    })
    assert facts.primary_subject_kind == "video"
    assert facts.primary_subject_count == 1
    assert facts.primary_player_count == 1
    assert facts.primary_article_body_count == 0


@pytest.mark.parametrize("body_tag", ["article", "main"])
def test_dominant_video_inside_proven_long_prose_remains_inline(body_tag):
    """真实长正文中的视频不能因几何显著而替代文章主体。"""
    facts = extract_subject_region_facts({
        "elements": [
            {"tag": body_tag, "selector": "#body", "visible": True,
             "web_subject_text_length": 900,
             "rect": {"x": 0, "y": 0, "width": 1000, "height": 2000}},
            {"tag": "p", "selector": "#prose", "parent_selector": "#body",
             "visible": True, "text_length": 900},
            {"tag": "video", "selector": "#inline-video", "parent_selector": "#body",
             "visible": True, "src": "blob:https://example.test/inline-media",
             "media": {"ready_state": 4, "duration": 120},
             "rect": {"x": 20, "y": 60, "width": 960, "height": 540}},
        ]
    })
    assert facts.primary_subject_kind == "article"
    assert facts.primary_subject_count == 1
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length == 900
    assert facts.primary_player_count == 0
    assert facts.inline_article_media_count == 1


def test_ready_blob_video_that_is_tiny_relative_to_subject_stays_unproven():
    """就绪媒体资源不能单独把小预览升格为详情主体。"""
    facts = extract_subject_region_facts({
        "elements": [
            {"tag": "main", "selector": "#main", "visible": True,
             "rect": {"x": 0, "y": 0, "width": 1000, "height": 800}},
            {"tag": "video", "selector": "#preview", "parent_selector": "#main",
             "visible": True, "src": "blob:https://example.test/preview",
             "media": {"ready_state": 4, "duration": 12},
             "rect": {"x": 20, "y": 20, "width": 160, "height": 90}},
        ]
    })
    assert facts.primary_player_count == 0
    assert facts.primary_subject_count == 0


def test_inline_article_video_is_not_primary_player():
    facts = extract_subject_region_facts(load_fixture("article_with_inline_video.html"))
    assert facts.primary_subject_kind == "article"
    assert facts.primary_player_count == 0
    assert facts.inline_article_media_count == 1
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length >= 800


def test_repeated_cards_in_main_region_remain_list_evidence():
    facts = extract_subject_region_facts(load_fixture("list_with_repeated_primary_cards.html"))
    assert facts.repeated_primary_card_count == 8
    assert facts.secondary_recommendation_card_count == 0
    assert facts.primary_subject_count == 0


def test_vendor_neutral_player_evidence_does_not_require_library_name():
    facts = extract_subject_region_facts(load_fixture("vendor_neutral_iframe_player.html"))
    assert facts.primary_subject_kind == "video"
    assert facts.primary_player_count == 1
    assert facts.player_vendor_tokens == ()


def test_gallery_forms_one_subject_without_becoming_article_body():
    facts = extract_subject_region_facts(load_fixture("gallery_primary.html"))
    assert facts.primary_subject_kind == "gallery"
    assert facts.primary_subject_count == 1
    assert facts.primary_article_body_count == 0


def test_competing_subjects_retain_conflict_instead_of_selecting_first():
    facts = extract_subject_region_facts(load_fixture("conflicting_primary_regions.html"))
    assert facts.primary_subject_count == 2
    assert facts.primary_subject_kind == "conflicting"
    assert facts.primary_player_count == 2


@pytest.mark.parametrize("width,height", [(80, 45), (0, 540), (float("nan"), 540)])
def test_small_or_invalid_media_geometry_does_not_prove_primary_player(width, height):
    snapshot = load_fixture("vendor_neutral_iframe_player.html")
    geometry = {
        "#subject": {"x": 0, "y": 0, "width": 1000, "height": 700},
        "#media": {"x": 0, "y": 0, "width": width, "height": height},
    }
    facts = extract_subject_region_facts(snapshot, element_geometry=geometry)
    assert facts.primary_player_count == 0
    assert facts.primary_subject_kind == "unknown"


def test_small_media_stays_insufficient_when_subject_geometry_is_missing():
    facts = extract_subject_region_facts(
        load_fixture("vendor_neutral_iframe_player.html"),
        element_geometry={
            "#media": {"x": 0, "y": 0, "width": 160, "height": 90},
        },
    )
    assert facts.primary_player_count == 0


def test_large_media_geometry_supports_vendor_neutral_player():
    snapshot = load_fixture("vendor_neutral_iframe_player.html")
    facts = extract_subject_region_facts(
        snapshot,
        element_geometry={
            "#subject": {"x": 0, "y": 0, "width": 1000, "height": 700},
            "#media": {"x": 20, "y": 60, "width": 960, "height": 540},
        },
    )
    assert facts.primary_player_count == 1
    assert "media_geometry_dominant" in dict(facts.token_summaries)["primary_media_player"]


def test_hidden_ancestor_and_preview_media_cannot_form_primary_subject():
    snapshot = snapshot_from_html(
        '<main><h1>主题</h1><div hidden><video src="/one.mp4"></video></div>'
        '<div class="video-preview"><video src="/two.mp4"></video></div></main>'
    )
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_player_count == 0


def test_ancestor_only_article_and_sidebar_preserve_region_boundaries():
    snapshot = {
        "elements": [
            {
                "selector": "#media",
                "tag": "video",
                "src": "/sample.mp4",
                "visible": True,
                "parent_selector": "#wrap",
                "web_ancestor_structure": [
                    {"selector": "#wrap", "tag": "div"},
                    {"selector": "#subject", "tag": "article"},
                ],
            },
            {
                "selector": "#side-media",
                "tag": "video",
                "src": "/side.mp4",
                "visible": True,
                "parent_selector": "#side",
                "web_ancestor_structure": [{"selector": "#side", "tag": "aside"}],
            },
        ]
    }
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_player_count == 1
    refs = dict(facts.selector_summaries)["primary_subject"]
    assert len(refs) == 1 and refs[0].startswith("article:sha256:")


def test_recommendation_heading_and_side_ad_cards_are_not_primary_cards():
    cards = '<ul><li><a href="/one">一</a></li><li><a href="/two">二</a></li></ul>'
    snapshot = snapshot_from_html(
        '<main><article><h1>主题</h1><video src="/sample.mp4"></video></article>'
        "<section><h2>相关推荐</h2>" + cards + "</section><aside>" + cards + "</aside>"
        '<section class="advertisement">' + cards + "</section></main>"
    )
    facts = extract_subject_region_facts(snapshot)
    assert facts.secondary_recommendation_card_count == 2
    assert facts.repeated_primary_card_count == 0


def test_audit_is_bounded_deterministic_and_contains_no_original_body():
    snapshot = load_fixture("article_with_inline_video.html")
    for node in snapshot["elements"]:
        node["class_tokens"] += ["private-" + "x" * 400] * 200
    facts = extract_subject_region_facts(snapshot)
    reverse = extract_subject_region_facts({"elements": list(reversed(snapshot["elements"]))})
    assert facts.selector_summaries
    assert facts.token_summaries
    assert asdict(facts) == asdict(reverse)
    assert "这是一段" not in repr(asdict(facts))
    assert "private-" not in repr(asdict(facts))
    assert "text_content" not in asdict(facts)
    assert "raw_html" not in asdict(facts)
    for _, values in facts.selector_summaries:
        assert len(values) <= 32
        assert all(len(value) <= 160 for value in values)


def test_navigation_source_and_breadcrumb_are_audited_without_creating_subject():
    snapshot = snapshot_from_html('<nav class="breadcrumb"><a href="/">首页</a></nav>')
    facts = extract_subject_region_facts(
        snapshot,
        navigation_sources=[
            {"selector": "#entry", "candidate_role": "detail"},
            {"selector": "#entry", "candidate_role": "detail"},
            {"selector": "#menu", "candidate_role": "menu"},
        ],
    )
    assert facts.detail_navigation_source_count == 1
    assert facts.breadcrumb_count == 1
    assert facts.primary_subject_count == 0
    refs = dict(facts.selector_summaries)["detail_navigation_source"]
    assert len(refs) == 1 and refs[0].startswith("node:sha256:")


def test_native_controls_and_poster_prove_loaded_shell_without_resource_url():
    facts = extract_subject_region_facts(
        snapshot_from_html(
            '<main><h1>主题</h1><video controls poster="/poster.png" '
            'width="960" height="540"></video></main>'
        )
    )
    assert facts.primary_player_count == 1
    assert facts.primary_subject_kind == "video"


def test_geometry_and_sparse_prose_keep_dominant_article_player_primary():
    snapshot = snapshot_from_html(
        '<article id="subject"><h1>主题</h1>'
        '<div><video id="media" src="/sample.mp4" controls></video></div><p>'
        + "文" * 160
        + "</p></article>"
    )
    facts = extract_subject_region_facts(
        snapshot,
        element_geometry={
            "#subject": {"x": 0, "y": 0, "width": 1000, "height": 800},
            "#media": {"x": 20, "y": 60, "width": 960, "height": 540},
        },
    )
    assert facts.primary_subject_kind == "video"
    assert facts.primary_player_count == 1
    assert facts.inline_article_media_count == 0


def test_sparse_snapshot_ancestor_tokens_exclude_recommendation_media():
    facts = extract_subject_region_facts(
        {
            "elements": [
                {
                    "tag": "video",
                    "selector": "#media",
                    "visible": True,
                    "src": "/sample.mp4",
                    "ancestor_class_tokens": ["recommendations"],
                    "parent_selector": "#missing",
                },
            ]
        }
    )
    assert facts.primary_player_count == 0


def test_vendor_tokens_are_auxiliary_and_audited_on_confirmed_player():
    facts = extract_subject_region_facts(
        snapshot_from_html(
            '<article><h1>主题</h1><div class="dplayer">'
            '<video src="/sample.mp4"></video></div></article>'
        )
    )
    assert facts.primary_player_count == 1
    assert facts.player_vendor_tokens == ("dplayer",)
    assert "dplayer" in dict(facts.token_summaries)["primary_media_player"]


@pytest.mark.parametrize(
    "html",
    [
        '<div class="dplayer"></div>',
        '<main><iframe src="/widget"></iframe></main>',
        '<main><iframe src="/widget"></iframe><button>播放</button></main>',
        '<main><video src="javascript:alert(1)" controls></video></main>',
    ],
)
def test_shell_or_opaque_resource_alone_is_insufficient(html):
    assert extract_subject_region_facts(snapshot_from_html(html)).primary_player_count == 0


def test_video_and_multiple_source_resources_count_one_player():
    facts = extract_subject_region_facts(
        snapshot_from_html(
            '<article><h1>主题</h1><video id="media" src="/one.mp4">'
            '<source src="/one.webm"><source src="/two.mp4"></video></article>'
        )
    )
    assert facts.primary_player_count == 1
    refs = dict(facts.selector_summaries)["primary_media_player"]
    assert len(refs) == 1 and refs[0].startswith("video:sha256:")


def test_missing_geometry_keeps_semantic_player_but_does_not_invent_geometry():
    snapshot = load_fixture("vendor_neutral_iframe_player.html")
    for node in snapshot["elements"]:
        node.pop("rect", None)
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_player_count == 1
    assert "media_geometry_dominant" not in dict(facts.token_summaries)["primary_media_player"]


def test_repeated_layout_panels_do_not_collapse_distinct_cards():
    facts = extract_subject_region_facts(
        snapshot_from_html(
            '<main><section><ul><li><a href="/one">一</a></li>'
            '<li><a href="/two">二</a></li></ul></section>'
            '<section><ul><li><a href="/three">三</a></li>'
            '<li><a href="/four">四</a></li></ul></section></main>'
        )
    )
    assert facts.repeated_primary_card_count == 4


def test_explicit_web_navigation_false_overrides_legacy_navigation_flag():
    facts = extract_subject_region_facts(
        {
            "elements": [
                {
                    "selector": "#media",
                    "tag": "video",
                    "visible": True,
                    "src": "/sample.mp4",
                    "within_web_navigation": False,
                    "within_navigation": True,
                },
            ]
        }
    )
    assert facts.primary_player_count == 1


def test_ancestor_preview_and_ad_tokens_exclude_sparse_media():
    facts = extract_subject_region_facts(
        {
            "elements": [
                {
                    "selector": "#preview",
                    "tag": "video",
                    "visible": True,
                    "src": "/sample.mp4",
                    "ancestor_class_tokens": ["video-preview"],
                },
                {
                    "selector": "#ad",
                    "tag": "video",
                    "visible": True,
                    "src": "/ad.mp4",
                    "ancestor_class_tokens": ["advertisement"],
                },
            ]
        }
    )
    assert facts.primary_player_count == 0


@pytest.mark.parametrize("parent", [None, "#missing"])
def test_sparse_prose_and_media_without_known_root_degrade_safely(parent):
    facts = extract_subject_region_facts(
        {
            "elements": [
                {
                    "selector": "#p",
                    "tag": "p",
                    "visible": True,
                    "text": "文" * 900,
                    "parent_selector": parent,
                },
                {
                    "selector": "#video",
                    "tag": "video",
                    "visible": True,
                    "src": "/sample.mp4",
                    "parent_selector": parent,
                },
            ]
        }
    )
    assert facts.primary_subject_kind == "unknown"
    assert facts.primary_player_count == 0


@pytest.mark.parametrize("width,height", [(80, 45), (80, 60), (200, 140)])
def test_tiny_wrapper_cannot_promote_tiny_media(width, height):
    snapshot = load_fixture("vendor_neutral_iframe_player.html")
    facts = extract_subject_region_facts(
        snapshot,
        element_geometry={
            "#main": {"x": 0, "y": 0, "width": 1000, "height": 900},
            "#subject": {"x": 0, "y": 0, "width": 1000, "height": 700},
            "#screen": {"x": 0, "y": 0, "width": width, "height": height},
            "#media": {"x": 0, "y": 0, "width": width, "height": height},
        },
    )
    assert facts.primary_player_count == 0
    assert "media_geometry_dominant" not in repr(facts.token_summaries)


@pytest.mark.parametrize("root", ["main", 'div role="article"', 'div id="root"'])
def test_generic_prose_wrappers_share_one_proven_subject(root):
    close_tag = root.split()[0]
    snapshot = snapshot_from_html(
        f"<{root}><div><p>"
        + "文" * 300
        + "</p></div><div><p>"
        + "章" * 300
        + f"</p></div></{close_tag}>"
    )
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_count == 1
    assert facts.primary_subject_kind == "article"
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length == 600


@pytest.mark.parametrize("root", ["article", "main"])
def test_non_paragraph_body_text_keeps_video_inline_and_deduplicates_containers(root):
    snapshot = snapshot_from_html(
        f"<{root}><div><div>" + "文" * 900 + "</div></div>"
        '<video src="/sample.mp4" width="320" height="180"></video>'
        f"</{root}>"
    )
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_kind == "article"
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length == 900
    assert facts.primary_player_count == 0
    assert facts.inline_article_media_count == 1


def test_non_paragraph_body_fallback_excludes_secondary_and_navigation_text():
    snapshot = snapshot_from_html(
        "<main><article><div>" + "文" * 900 + "</div>"
        '<video src="/sample.mp4" width="320" height="180"></video>'
        '<section class="recommendations"><div>' + "荐" * 700 + "</div></section>"
        "<nav><div>" + "导" * 700 + "</div></nav></article></main>"
    )
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_article_text_length == 900
    assert facts.primary_article_body_count == 1
    assert facts.primary_subject_count == 1


@pytest.mark.parametrize(
    "selector",
    [
        "#private-body-marker",
        ".private-body-marker",
        '[data-title="private-body-marker"]',
        '[data-title="escaped\\]private-body-marker"]',
        "#https://private.example/body?token=private-body-marker",
    ],
)
def test_audit_fingerprints_all_dynamic_selector_values(selector):
    node = {
        "selector": selector,
        "tag": "article",
        "visible": True,
        "text": "private-body-marker" * 60,
        "class_tokens": ["private-body-marker"],
    }
    snapshot = {
        "elements": [
            node,
            {
                "selector": selector + " > p",
                "tag": "p",
                "parent_selector": selector,
                "visible": True,
                "text": "private-body-marker" * 60,
            },
        ]
    }
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_count == 1
    audit = repr((facts.selector_summaries, facts.token_summaries))
    assert "private-body-marker" not in audit
    assert "private.example" not in audit
    assert "https://" not in audit
    assert "token=" not in audit
    assert facts == extract_subject_region_facts({"elements": list(reversed(snapshot["elements"]))})


def test_absolute_media_size_alone_does_not_claim_relative_dominance():
    facts = extract_subject_region_facts(load_fixture("video_primary_with_recommendations.html"))
    assert facts.primary_player_count == 1
    assert "media_geometry_dominant" not in repr(facts.token_summaries)


def test_truncated_secondary_text_cannot_leave_false_container_body():
    facts = extract_subject_region_facts(
        {
            "elements": [
                {"selector": "#main", "tag": "main", "visible": True, "text_length": 900},
                {"selector": "#nav", "tag": "nav", "visible": True, "parent_selector": "#main"},
                {
                    "selector": "#short",
                    "tag": "p",
                    "visible": True,
                    "parent_selector": "#nav",
                    "text": "荐" * 160,
                },
            ]
        }
    )
    assert facts.primary_article_body_count == 0
    assert facts.primary_subject_count == 0


def test_subject_counter_does_not_cover_unproven_recommendation_exclusion():
    snapshot = snapshot_from_html(
        '<main id="main"><section><h2>相关推荐</h2><div>' + "荐" * 900 + "</div></section></main>"
    )
    snapshot["elements"][0]["web_subject_text_length"] = 900
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_count == 0
    assert facts.primary_article_body_count == 0


def test_trusted_article_absorbs_nested_repeated_links_and_flagged_ads():
    """文章内重复链接和已标记广告不能把可信正文反判成列表。"""

    facts = extract_subject_region_facts(
        {
            "elements": [
                {
                    "selector": "#subject",
                    "tag": "article",
                    "visible": True,
                    "text_length": 1100,
                    "web_subject_text_length": 900,
                },
                {
                    "selector": "#subject > a:nth-of-type(1)",
                    "tag": "a",
                    "visible": True,
                    "parent_selector": "#subject",
                    "href": "https://one.example/item",
                    "repeat_group_size": 2,
                    "within_repeated_structure": True,
                },
                {
                    "selector": "#subject > a:nth-of-type(2)",
                    "tag": "a",
                    "visible": True,
                    "parent_selector": "#subject",
                    "href": "https://two.example/item",
                    "repeat_group_size": 2,
                    "within_repeated_structure": True,
                },
                {
                    "selector": "#subject > a:nth-of-type(3)",
                    "tag": "a",
                    "visible": True,
                    "parent_selector": "#subject",
                    "href": "https://ads.example/landing",
                    "repeat_group_size": 2,
                    "within_repeated_structure": True,
                    "is_advertisement": True,
                },
            ]
        }
    )

    assert facts.primary_subject_kind == "article"
    assert facts.primary_subject_count == 1
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length == 900
    assert facts.repeated_primary_card_count == 0
