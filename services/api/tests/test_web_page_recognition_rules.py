"""机器规则必须阻止宽泛 token、URL 与模型提示越过结构门槛。"""

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
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.page.web_page_catalog import (
    WebPageTypeCatalogError,
    load_web_page_type_catalog,
    parse_web_page_type_catalog,
)


def facts(**overrides):
    overrides.setdefault("subject_region_facts", WebSubjectRegionFacts(
        repeated_primary_card_count=overrides.get("repeated_primary_card_count", 2),
    ))
    return replace(
        WebPageStructureFacts(
            navigation_element_count=0,
            repeated_link_count=2,
            primary_detail_link_count=2,
            repeated_primary_card_count=2,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=(),
        ),
        **overrides,
    )


class Suggestion:
    def __init__(self, page_type):
        self.page_type = page_type
        self.payload = None

    async def classify(self, **kwargs):
        self.payload = kwargs
        return WebPageAssessment(page_type=self.page_type, confidence=0.99, evidence=("截图",))


async def classify(page_type, structure, url="https://example.org/category"):
    assessor = Suggestion(page_type)
    result = await WebPageClassifier(
        assessor=assessor, catalog=load_web_page_type_catalog()
    ).classify(
        WebReadyPageCapture(
            capture_id="c1",
            web_capture_stage="web_page_ready_baseline",
            screenshot_artifact_id="s1",
            dom_artifact_id="d1",
            visible_text_artifact_id="t1",
            page_role="menu",
            url=url,
            structure_facts=structure,
        )
    )
    return result, assessor.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_type", "token"),
    [
        ("content_list", "item"),
        ("content_list", "card"),
        ("video_detail", "video-list"),
        ("video_detail", "source"),
        ("video_detail", "player"),
        ("content_detail", "detail"),
    ],
)
async def test_generic_token_cannot_be_signed_by_confident_model(page_type, token):
    result, _ = await classify(
        page_type, facts(dom_tokens=(token,), single_subject_element_count=1,
                         repeated_primary_card_count=0)
    )
    assert result.page_type == "unknown"


@pytest.mark.asyncio
async def test_home_requires_root_and_aggregate_structure_and_exposes_rule_audit():
    result, payload = await classify("home", facts(), url="https://example.org/")
    assert result.page_type == "home"
    assert payload["structure_facts"]["rule_evaluations"]["home"]["required_satisfied"] is True
    empty, _ = await classify(
        "home", facts(repeated_primary_card_count=0), url="https://example.org/"
    )
    assert empty.page_type == "unknown"
    nonroot, _ = await classify("home", facts())
    assert nonroot.page_type == "unknown"


@pytest.mark.asyncio
async def test_home_single_player_conflict_survives_missing_positive_structure():
    result, _ = await classify(
        "home",
        facts(repeated_primary_card_count=0, single_subject_element_count=1, player_root_count=1),
        url="https://example.org/",
    )
    assert result.page_type == "unknown"
    assert "single_player_conflicts_with_home" in result.conflicts


@pytest.mark.asyncio
async def test_home_single_long_body_conflicts_even_without_article_tag():
    result, _ = await classify(
        "home",
        facts(single_subject_element_count=1, paragraph_text_length=600),
        url="https://example.org/",
    )
    assert result.page_type == "unknown"
    assert "long_body_conflicts_with_home" in result.conflicts


@pytest.mark.asyncio
async def test_home_article_cards_do_not_count_as_single_article_conflict():
    structure = facts(
        repeated_primary_card_count=29,
        primary_detail_link_count=29,
        single_subject_element_count=1,
        article_body_count=1,
        subject_region_facts=WebSubjectRegionFacts(
            primary_subject_count=0,
            primary_article_body_count=0,
            repeated_primary_card_count=29,
        ),
    )
    result, _ = await classify("home", structure, url="https://example.org/")
    assert result.page_type == "home"
    assert "single_article_conflicts_with_home" not in result.conflicts


@pytest.mark.asyncio
async def test_home_single_primary_article_remains_strong_conflict():
    structure = facts(
        single_subject_element_count=1,
        article_body_count=1,
        subject_region_facts=WebSubjectRegionFacts(
            primary_subject_count=1,
            primary_subject_kind="article",
            primary_article_body_count=1,
        ),
    )
    result, _ = await classify("home", structure, url="https://example.org/")
    assert result.page_type != "home"
    assert "single_article_conflicts_with_home" in (
        result.model_result["rule_evaluation"]["strong_conflicts"]
    )


@pytest.mark.parametrize("token", ["not-video-player", "article-bodyguard", "source", "player"])
def test_snapshot_subject_detection_does_not_use_arbitrary_substrings(token):
    result = web_page_structure_facts(
        {
            "elements": [
                {"visible": True, "tag": "div", "class_tokens": [token], "selector": "#" + token}
            ]
        }
    )
    assert result.player_root_count == result.article_body_count == 0


@pytest.mark.parametrize(
    "mutation", ["missing", "fact", "operator", "value", "empty_group", "extra"]
)
def test_catalog_rejects_invalid_machine_rules(mutation):
    catalog_path = Path(__file__).parents[1] / "app/domain/page/web_page_type_catalog.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    rules = {
        "required_all": [{"fact": "is_canonical_root_url", "operator": "eq", "value": True}],
        "required_any": [{"fact": "homepage_section_count", "operator": "gte", "value": 2}],
        "supporting": [],
        "strong_negative_any": [],
    }
    if mutation == "fact":
        rules["required_all"][0]["fact"] = "hostname"
    if mutation == "operator":
        rules["required_all"][0]["operator"] = "eval"
    if mutation == "value":
        rules["required_all"][0]["value"] = 1
    if mutation == "empty_group":
        rules["strong_negative_any"] = [{"reason_code": "x", "all_of": []}]
    if mutation == "extra":
        rules["unknown"] = True
    if mutation == "missing":
        payload["page_types"][0].pop("recognition_rules", None)
    else:
        payload["page_types"][0]["recognition_rules"] = rules
    with pytest.raises(WebPageTypeCatalogError):
        parse_web_page_type_catalog(payload)


@pytest.mark.parametrize(
    "page_type", [
        "home", "content_list", "content_detail", "video_list", "video_detail",
        "activity", "search_page"
    ]
)
@pytest.mark.asyncio
async def test_top_layer_obstruction_overrides_every_page_type(page_type):
    structure = facts(
        dom_tokens=("content-grid",),
        video_element_count=1,
        article_body_count=1,
        top_level_obstruction_count=1,
    )
    result, payload = await classify(page_type, structure, url="https://example.org/")
    assert result.page_type == "unknown"
    assert "top_level_obstruction" in result.conflicts
    assert payload["structure_facts"]["rule_evaluations"][page_type]["strong_conflicts"]


def test_repeated_articles_and_navigation_ads_do_not_establish_detail_subject():
    snapshot = {
        "elements": [
            {
                "tag": "article",
                "visible": True,
                "within_repeated_structure": True,
                "class_tokens": ["article-card"],
            },
            {"tag": "video", "visible": True, "is_advertisement": True},
            {"tag": "p", "visible": True, "within_navigation": True, "text": "长文本" * 200},
        ]
    }
    result = web_page_structure_facts(snapshot)
    assert (
        result.article_body_count == result.player_root_count == result.paragraph_text_length == 0
    )


def test_actual_single_player_root_is_home_conflict_subject():
    result = web_page_structure_facts(
        {
            "elements": [
                {
                    "tag": "div",
                    "visible": True,
                    "selector": "#player-root",
                    "class_tokens": ["player-root"],
                }
            ]
        }
    )
    assert result.single_subject_element_count == 1


@pytest.mark.parametrize(
    "source,expected",
    [
        ({"tag": "source", "media_parent_tag": "video", "src": "/main.mp4"}, 1),
        ({"tag": "source", "media_parent_tag": "audio", "src": "/track.mp3"}, 0),
        ({"tag": "video", "src": "/main.mp4", "is_advertisement": True}, 0),
        ({"tag": "video", "src": "/main.mp4", "within_navigation": True}, 0),
        ({"tag": "video", "src": "  "}, 0),
    ],
)
def test_confirmed_video_resource_is_typed_and_excludes_nonbusiness(source, expected):
    result = web_page_structure_facts(
        {"elements": [{"visible": True, "selector": "#media", **source}]}
    )
    assert result.confirmed_primary_video_resource_count == expected


def test_video_nested_in_repeated_card_is_not_primary_even_without_child_repeat_flag():
    result = web_page_structure_facts(
        {
            "elements": [
                {
                    "visible": True,
                    "tag": "div",
                    "selector": "#card",
                    "within_repeated_structure": True,
                },
                {
                    "visible": True,
                    "tag": "video",
                    "selector": "#video",
                    "parent_selector": "#card",
                    "src": "/preview.mp4",
                },
            ]
        }
    )
    assert result.confirmed_primary_video_resource_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_type,sections,want",
    [
        ("home", 2, "home"),
        ("activity", 2, "unknown"),
        ("home", 0, "unknown"),
        ("activity", 0, "activity"),
    ],
)
async def test_single_activity_and_aggregate_home_resolve_by_primary_structure(
    model_type, sections, want
):
    result, _ = await classify(
        model_type,
        facts(
            homepage_section_count=sections,
            activity_primary_region_count=1,
            activity_action_count=1,
        ),
        url="https://example.org/",
    )
    assert result.page_type == want


@pytest.mark.asyncio
async def test_direct_video_source_establishes_a_confirmed_video_subject():
    structure = web_page_structure_facts(
        {
            "elements": [
                {
                    "visible": True,
                    "tag": "source",
                    "media_parent_tag": "video",
                    "src": "/main.mp4",
                    "selector": "#source",
                },
            ]
        }
    )
    result, _ = await classify("video_detail", structure)
    assert result.page_type == "video_detail"


def test_open_search_role_pairs_text_input_with_submit_without_global_button_guess():
    structure = web_page_structure_facts(
        {
            "elements": [
                {"visible": True, "tag": "form", "role": "search", "selector": "#search"},
                {
                    "visible": True,
                    "tag": "input",
                    "type": "text",
                    "selector": "#query",
                    "parent_selector": "#search",
                },
                {
                    "visible": True,
                    "tag": "button",
                    "type": "submit",
                    "selector": "#submit",
                    "parent_selector": "#search",
                },
            ]
        }
    )
    assert structure.search_input_count == structure.search_trigger_count == 1


@pytest.mark.parametrize(
    "media",
    [
        {"src": "/background.mp4", "class_tokens": ["background-video"]},
        {"src": "/preview.mp4", "ancestor_class_tokens": ["video-preview"]},
        {"src": "javascript:play()"},
    ],
)
def test_background_preview_and_nonresource_src_are_not_confirmed_primary_media(media):
    result = web_page_structure_facts(
        {
            "elements": [
                {"visible": True, "tag": "video", "selector": "#media", **media},
            ]
        }
    )
    assert result.confirmed_primary_video_resource_count == 0


def test_activity_close_control_does_not_establish_participation_action():
    result = web_page_structure_facts(
        {
            "elements": [
                {
                    "visible": True,
                    "tag": "main",
                    "selector": "#campaign",
                    "class_tokens": ["campaign"],
                },
                {
                    "visible": True,
                    "tag": "button",
                    "selector": "#close",
                    "parent_selector": "#campaign",
                    "class_tokens": ["close"],
                },
            ]
        }
    )
    assert result.activity_primary_region_count == result.activity_action_count == 0


def test_search_trigger_must_belong_to_the_open_search_input_region():
    result = web_page_structure_facts(
        {
            "elements": [
                {"visible": True, "tag": "form", "role": "search", "selector": "#search"},
                {
                    "visible": True,
                    "tag": "input",
                    "type": "search",
                    "selector": "#query",
                    "parent_selector": "#search",
                },
                {
                    "visible": True,
                    "tag": "button",
                    "type": "submit",
                    "selector": "#unrelated-submit",
                },
            ]
        }
    )
    assert result.search_trigger_count == 0


@pytest.mark.asyncio
async def test_five_type_catalog_cannot_drop_machine_rules_and_use_legacy_gate():
    stripped = load_web_page_type_catalog().core
    capture = WebReadyPageCapture(
        capture_id="c1",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="s1",
        dom_artifact_id="d1",
        visible_text_artifact_id="t1",
        page_role="menu",
        structure_facts=facts(dom_tokens=("video-list",)),
    )
    with pytest.raises(ValueError, match="规则"):
        await WebPageClassifier(assessor=Suggestion("video_detail"), catalog=stripped).classify(
            capture
        )


@pytest.mark.parametrize("token", ["card", "item", "detail", "video", "source", "player"])
def test_generic_tokens_are_only_legal_as_supporting_rules(token):
    catalog_path = Path(__file__).parents[1] / "app/domain/page/web_page_type_catalog.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["page_types"][1]["recognition_rules"]["required_any"] = [
        {"fact": "dom_tokens", "operator": "contains_exact", "value": token}
    ]
    with pytest.raises(WebPageTypeCatalogError):
        parse_web_page_type_catalog(payload)
