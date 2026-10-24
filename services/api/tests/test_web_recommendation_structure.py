"""命名空间推荐组件与正文归属，保留真正独立的主列表冲突。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts
from services.api.tests.test_web_four_site_feature_regressions import _node


@pytest.mark.parametrize("component", [
    "widget-recommend", "widget__related", "recommendations", "widget-recommend-grid",
])
def test_namespaced_recommendation_cards_do_not_conflict_with_article(component):
    nodes = [
        _node("#main", "main"),
        _node("#article", "article", "#main", web_subject_text_length=900),
        *[
            _node(f"#card-{i}", "a", "#main", href=f"https://example.test/items/{i}",
                  repeat_group_size=3, ancestor_class_tokens=[component])
            for i in range(3)
        ],
    ]
    result = extract_subject_region_facts({"elements": nodes})
    assert result.primary_article_body_count == 1
    assert result.repeated_primary_card_count == 0
    assert result.secondary_recommendation_card_count == 3
    # 普通卡片和伪似推荐子串不能被正文吞掉。
    for node in nodes[2:]:
        node["ancestor_class_tokens"] = ["unrelated", "recommendationary"]
    result = extract_subject_region_facts({"elements": nodes})
    assert result.repeated_primary_card_count == 3
    assert result.secondary_recommendation_card_count == 0


def test_model_structure_counts_exclude_recommendations_only_after_body_is_proven():
    nodes = [
        _node("#main", "main"),
        _node("#article", "article", "#main", web_subject_text_length=900),
        *[
            _node(f"#card-{i}", "a", "#main", href=f"https://example.test/items/{i}",
                  text=f"作品{i}", interactive=True,
                  repeat_group_size=3, within_repeated_structure=True,
                  rect={"x": 0, "y": 100 + i * 40, "width": 200, "height": 30},
                  ancestor_class_tokens=["widget-recommend"])
            for i in range(3)
        ],
    ]
    snapshot = {"capture_id": "capture-ready", "elements": nodes}
    before = web_page_structure_facts(
        {**snapshot, "elements": [nodes[0], *nodes[2:]]},
        origin="https://example.test", current_url="https://example.test/list",
    )
    assert before.primary_detail_link_count == 3
    after = web_page_structure_facts(
        snapshot, origin="https://example.test", current_url="https://example.test/article",
    )
    assert after.primary_detail_link_count == after.media_card_count == 0
    assert after.subject_region_facts.secondary_recommendation_card_count == 3


def test_recommendation_exclusion_preserves_global_obstruction_and_advertisement_facts():
    nodes = [
        _node("#article", "article", web_subject_text_length=900),
        _node("#recommend", "div", class_tokens=["widget-recommend"],
              web_obstruction_layer_structural=True, is_advertisement=True),
    ]
    facts = web_page_structure_facts({"elements": nodes})
    assert facts.top_level_obstruction_count == 1
    assert facts.advertisement_element_count == 1
