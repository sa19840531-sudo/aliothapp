"""验证视频信息流首页的可信选中导航事实。"""

from copy import deepcopy

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts


def _entry(text: str, href: str, *, active: bool = False) -> dict[str, object]:
    return {
        "tag": "a",
        "selector": f"#site-nav > a:nth-of-type({1 if active else 2})",
        "text": text,
        "href": href,
        "class_tokens": ["nav-entry", "is-active"] if active else ["nav-entry"],
        "navigation_container_id": "#site-nav",
        "within_web_navigation": True,
        "top_navigation_position_proven": True,
        "visible": True,
        "interactive": True,
        "hit_test": {
            "center_hits_target": True,
            "visual_occlusion_detected": False,
        },
        "rect": {"x": 20, "y": 20, "width": 90, "height": 40},
    }


def _snapshot() -> dict[str, object]:
    return {
        "url": "https://example.test/",
        "viewport": {"width": 1200, "height": 800},
        "elements": [
            _entry("首页", "https://example.test/", active=True),
            _entry("精选", "https://example.test/featured"),
            _entry("分类", "https://example.test/categories"),
        ],
    }


def test_canonical_root_with_visible_active_home_nav_is_proven() -> None:
    facts = web_page_structure_facts(
        _snapshot(),
        origin="https://example.test",
        current_url="https://example.test/",
    )

    assert facts.navigation_structure_facts.visible_category_entry_count == 3
    assert getattr(facts, "active_home_navigation", False) is True


@pytest.mark.parametrize(
    "case",
    (
        "inactive",
        "external_href",
        "business_query",
        "occluded",
        "missed_hit",
        "offscreen",
        "duplicate_active",
        "human_verification",
        "snapshot_origin_changed",
        "not_root",
    ),
)
def test_active_home_nav_rejects_unsafe_or_ambiguous_evidence(case: str) -> None:
    snapshot = deepcopy(_snapshot())
    home = snapshot["elements"][0]
    current_url = "https://example.test/"
    if case == "inactive":
        home["class_tokens"] = ["nav-entry"]
    elif case == "external_href":
        home["href"] = "https://other.test/"
    elif case == "business_query":
        home["href"] = "https://example.test/?content_id=1"
    elif case == "occluded":
        home["occluded"] = True
    elif case == "missed_hit":
        home["hit_test"]["center_hits_target"] = False
    elif case == "offscreen":
        home["rect"]["x"] = -10
    elif case == "duplicate_active":
        snapshot["elements"].append(_entry("首页", "https://example.test/", active=True))
    elif case == "human_verification":
        snapshot["human_verification_present"] = True
    elif case == "snapshot_origin_changed":
        snapshot["url"] = "https://other.test/"
    elif case == "not_root":
        current_url = "https://example.test/video/1"

    facts = web_page_structure_facts(
        snapshot,
        origin="https://example.test",
        current_url=current_url,
    )

    assert facts.active_home_navigation is False


def test_malformed_selected_attribute_does_not_promote_home() -> None:
    home = _entry("首页", "https://example.test/")
    home["aria_current"] = []
    snapshot = {
        "url": "https://example.test/",
        "viewport": {"width": 1200, "height": 800},
        "elements": [
            home,
            _entry("精选", "https://example.test/featured"),
            _entry("分类", "https://example.test/categories"),
        ],
    }

    facts = web_page_structure_facts(
        snapshot,
        origin="https://example.test",
        current_url="https://example.test/",
    )

    assert facts.active_home_navigation is False
