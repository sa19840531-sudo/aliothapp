"""仅使用正式元素矩形验证二至四类等分分布，不构造文字或初始状态事实。"""

import pytest

from services.api.app.application.green_navigation_judge import judge_green_navigation
from services.api.tests.test_green_navigation_judge import bundle, navigation, subitem


@pytest.mark.parametrize("count", [2, 3, 4])
def test_equal_controls_centered_in_equal_container_regions_pass_distribution(count):
    dom, snapshot = navigation((60,) * count)
    for index, row in enumerate(snapshot["elements"][:count]):
        row["rect"]["x"] = (index + 0.5) * 390 / count - 30
    outcome = judge_green_navigation(bundle(dom, snapshot))
    item = subitem(outcome, "equal_width_navigation")
    assert item["status"] == "pass"
    assert item["evidence_refs"] == ["ui-original", "dom-original"]
    assert outcome["execution_status"] == "evidence_insufficient"
    assert subitem(outcome, "initial_next_label")["missing"]


@pytest.mark.parametrize("fault", ["unequal_spacing", "rounded_centers", "outside_viewport"])
def test_equal_widths_do_not_fill_unproven_distribution(fault):
    dom, snapshot = navigation((60,) * 3)
    for index, row in enumerate(snapshot["elements"][:3]):
        row["rect"]["x"] = 35 + index * 130
    if fault == "unequal_spacing":
        snapshot["elements"][1]["rect"]["x"] += 10
    elif fault == "rounded_centers":
        snapshot["elements"][1]["rect"]["x"] += 0.01
    else:
        for row in snapshot["elements"]:
            row["rect"]["x"] += 20
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert subitem(outcome, "equal_width_navigation")["status"] == "evidence_insufficient"
    assert outcome["execution_status"] == "evidence_insufficient"


def test_contiguous_equal_slots_outside_the_visible_viewport_do_not_pass():
    dom, snapshot = navigation()
    for row in snapshot["elements"]:
        row["rect"]["x"] += 20
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert subitem(outcome, "equal_width_navigation")["status"] == "evidence_insufficient"


def test_five_equal_centered_controls_still_require_real_text_bounds():
    dom, snapshot = navigation((60,) * 5)
    for index, row in enumerate(snapshot["elements"][:5]):
        row["rect"]["x"] = (index + 0.5) * 78 - 30
    outcome = judge_green_navigation(bundle(dom, snapshot))
    item = subitem(outcome, "fit_navigation_spacing")
    assert item["status"] == "evidence_insufficient"
    assert any("text_bounds" in gap for gap in item["missing"])
    assert item["evaluated"] is False
