"""绿色32只读消费回归；手工原件夹具与真实浏览器生产器集成分别验证。"""

import copy
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_navigation_judge import judge_green_navigation
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)
from services.api.tests.test_green_shared_rule_judge import definition, service


def navigation(widths=(130, 130, 130), *, container=True, text_clip=False):
    """使用已存在的 dom/ui-snapshot.v2 字段，不增加采集端的新合同。"""
    count = len(widths)
    labels = [f"分类{index}" for index in range(count)]
    dom = (
        '<html><body><nav id="categories" role="navigation">'
        + "".join(
            f'<a id="tab{index}" href="/{index}">{label}</a>' for index, label in enumerate(labels)
        )
        + "</nav></body></html>"
    )
    rows = []
    left = 0
    for index, (label, width) in enumerate(zip(labels, widths, strict=True)):
        rows.append(
            {
                "selector": f"#tab{index}",
                "parent_selector": "#categories",
                "tag": "a",
                "href": f"https://fixture.test/{index}",
                "text": label,
                "interactive": True,
                "visible": True,
                "within_navigation": True,
                "repeat_group_size": count,
                "rect": {"x": left, "y": 0, "width": width, "height": 40},
                "style": {
                    "overflow_x": "hidden" if text_clip else "visible",
                    "white_space": "nowrap",
                    "text_overflow": "ellipsis" if text_clip else "clip",
                },
                "text_metrics": {
                    "horizontal_overflow": text_clip,
                    "client_width": width,
                    "scroll_width": width + 100 if text_clip else width,
                },
            }
        )
        left += width
    if container:
        rows.append(
            {
                "selector": "#categories",
                "tag": "nav",
                "role": "navigation",
                "rect": {"x": 0, "y": 0, "width": 390, "height": 40},
                "style": {"overflow_x": "auto"},
                "text_metrics": {"scroll_width": left, "client_width": 390},
            }
        )
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "url": "https://fixture.test/",
        "viewport": {
            "width": 390,
            "height": 844,
            "visual_viewport_scale": 1,
            "scroll_x": 0,
            "scroll_y": 0,
        },
        "elements": rows,
        "element_count": len(rows),
        "element_limit_reached": False,
    }
    return dom, snapshot


def bundle(dom, snapshot):
    def artifact(identity, kind, payload, capture="capture-1"):
        return TrustedEvidenceArtifact(identity, kind, capture, "primary", "", payload)

    return TrustedEvidenceBundle(
        "packet-1",
        "capture-1",
        {"primary": "capture-1"},
        (
            artifact("ui-original", "ui_snapshot", json.dumps(snapshot).encode()),
            artifact("dom-original", "dom", dom.encode()),
            artifact("image-original", "screenshot", b"controlled-unit-image-placeholder"),
            artifact("unused-text", "visible_text", b"unrelated"),
        ),
    )


def subitem(outcome, key):
    return next(item for item in outcome["subitems"] if item["key"] == key)


def test_complete_unequal_slots_fail_without_container_but_keep_initial_gap():
    outcome = judge_green_navigation(bundle(*navigation((100, 190, 100), container=False)))
    item = subitem(outcome, "equal_width_navigation")
    assert outcome["execution_status"] == item["status"] == "fail"
    assert item["confidence"] == outcome["confidence"] == 1
    assert item["evidence_refs"] == ["ui-original", "dom-original"]
    assert subitem(outcome, "initial_next_label")["status"] == "evidence_insufficient"
    assert outcome["missing_evidence"]


def test_equal_slots_only_pass_when_existing_container_is_also_proven():
    for container, expected in ((True, "pass"), (False, "evidence_insufficient")):
        outcome = judge_green_navigation(bundle(*navigation(container=container)))
        assert subitem(outcome, "equal_width_navigation")["status"] == expected
        assert outcome["execution_status"] == "evidence_insufficient"
        assert subitem(outcome, "fit_navigation_spacing")["status"] == "skipped"


def test_rounding_resolution_is_a_gap_and_never_a_new_business_tolerance():
    outcome = judge_green_navigation(bundle(*navigation((129.997, 130.003, 130))))
    item = subitem(outcome, "equal_width_navigation")
    assert item["status"] == "evidence_insufficient"
    assert any("measurement_resolution" in gap for gap in item["missing"])


@pytest.mark.parametrize(
    "widths,status",
    [((100.0, 100.01), "evidence_insufficient"), ((100.0, 100.01001), "fail")],
)
def test_decimal_reading_boundary_is_not_changed_by_binary_subtraction(widths, status):
    outcome = judge_green_navigation(bundle(*navigation(widths, container=False)))
    assert subitem(outcome, "equal_width_navigation")["status"] == status


def test_five_fully_contained_label_overflow_is_not_proven_text_clipping():
    outcome = judge_green_navigation(bundle(*navigation((78,) * 5, text_clip=True)))
    item = subitem(outcome, "fit_navigation_spacing")
    assert outcome["execution_status"] == item["status"] == "evidence_insufficient"
    assert item["evaluated"] is False
    assert any("text_bounds" in gap for gap in item["missing"])
    assert subitem(outcome, "initial_next_label")["missing"]


def test_overflowing_navigation_is_not_failed_for_legal_outer_clipping():
    outcome = judge_green_navigation(bundle(*navigation((100,) * 5, text_clip=True)))
    assert outcome["execution_status"] == "evidence_insufficient"
    assert subitem(outcome, "fit_navigation_spacing")["status"] == "evidence_insufficient"
    scrolling = subitem(outcome, "overflow_scroll")
    assert scrolling["status"] == "evidence_insufficient"
    assert scrolling["evaluated"] is False
    assert scrolling["observations"]
    assert "实际可横向滚动" in scrolling["observations"][0]["reason"]


@pytest.mark.parametrize("fault", ["partial", "hidden", "truncated", "empty", "app", "budget"])
def test_unproven_complete_enumeration_or_browser_facts_never_pass(fault):
    dom, snapshot = navigation()
    if fault == "partial":
        snapshot["elements"].pop(1)
    elif fault == "hidden":
        dom = dom.replace("</nav>", '<a href="/hidden" hidden>隐藏分类</a></nav>')
    elif fault == "truncated":
        dom += "<!-- alioth:dom-truncated original_bytes=4000000 -->"
    elif fault == "empty":
        dom = "<html><body></body></html>"
        snapshot["elements"] = []
    elif fault == "app":
        snapshot["schema_version"] = "app-ui-snapshot.v1"
    else:
        snapshot["budget_truncated"] = True
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert outcome["execution_status"] == "evidence_insufficient"
    assert all(item["status"] != "pass" for item in outcome["subitems"])


def test_different_capture_originals_cannot_be_combined():
    original = bundle(*navigation())
    artifacts = tuple(
        replace(item, page_capture_id="another-capture") if item.artifact_type == "dom" else item
        for item in original.artifacts
    )
    outcome = judge_green_navigation(replace(original, artifacts=artifacts))
    assert all(not item["evaluated"] for item in outcome["subitems"])


def test_complete_one_group_failure_survives_another_group_missing():
    dom, snapshot = navigation((100, 190, 100))
    dom = dom.replace("</body>", '<nav><a href="/extra">未知分类</a></nav></body>')
    outcome = judge_green_navigation(bundle(dom, snapshot))
    item = subitem(outcome, "equal_width_navigation")
    assert outcome["execution_status"] == item["status"] == "fail"
    assert any("navigation:2" in gap for gap in item["missing"])


def test_source_self_report_cannot_fill_initial_or_text_bounds():
    dom, snapshot = navigation((78,) * 5)
    snapshot.update(
        {
            "complete_navigation_siblings": True,
            "initial_state": True,
            "green_navigation_pass": True,
            "text_bounds": [{"x": 1}],
        }
    )
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert outcome["execution_status"] == "evidence_insufficient"
    assert subitem(outcome, "initial_next_label")["evaluated"] is False
    assert subitem(outcome, "fit_navigation_spacing")["evaluated"] is False


async def test_existing_rule_judge_route_keeps_precise_sources_and_fail_confidence():
    original = bundle(*navigation((100, 190, 100), container=False))
    judge, request, reader = service(definition("32"))
    reader.prepare_observation_packet = AsyncMock(return_value=original)
    result = await judge.judge(request)
    assert result.execution_status == "fail" and result.confidence == 1
    items = result.judge_output["subresults"]
    assert items[0]["evidence_refs"] == ["ui-original", "dom-original"]
    assert "unused-text" not in items[0]["evidence_refs"]
    assert items[1]["applicability"] == "not_applicable"
    assert items[3]["evidence_refs"] == []
    assert "100.0、190.0、100.0" in result.judge_output["reasoning_summary"]
    tool = result.judge_output["tool_results"][0]
    assert tool["status"] == "ok" and tool["readonly"] is True
    assert tool["subitems"][0]["status"] == "fail"
    assert tool["subitems"][0]["evidence_refs"] == ["ui-original", "dom-original"]


def test_duplicate_semantic_targets_are_not_reused_as_two_classifications():
    dom, snapshot = navigation()
    for row in snapshot["elements"][:3]:
        row["href"] = "https://fixture.test/same"
        row["text"] = "同名"
    dom = dom.replace('href="/0"', 'href="/same"').replace('href="/1"', 'href="/same"')
    dom = dom.replace('href="/2"', 'href="/same"')
    dom = dom.replace("分类0", "同名").replace("分类1", "同名").replace("分类2", "同名")
    outcome = judge_green_navigation(bundle(dom, copy.deepcopy(snapshot)))
    assert subitem(outcome, "equal_width_navigation")["status"] == "evidence_insufficient"


def test_later_snapshot_cannot_hide_new_sibling_behind_old_dom_enumeration():
    dom, snapshot = navigation((130, 130, 130))
    extra = {
        **snapshot["elements"][0],
        "selector": "#later",
        "text": "新增分类",
        "href": "https://fixture.test/later",
    }
    snapshot["elements"].append(extra)
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert subitem(outcome, "equal_width_navigation")["status"] == "evidence_insufficient"
    assert any("sibling_drift" in gap for gap in outcome["missing_evidence"])


def test_equal_li_slots_are_measured_instead_of_variable_label_link_widths():
    dom, snapshot = navigation((40, 70, 90))
    dom = (
        '<html><body><nav role="navigation"><ul id="categories" role="tablist">'
        + "".join(
            f'<li><a id="tab{index}" href="/{index}">分类{index}</a></li>' for index in range(3)
        )
        + "</ul></nav></body></html>"
    )
    for index, row in enumerate(snapshot["elements"][:3]):
        row["parent_selector"] = f"#slot{index}"
        row["rect"]["x"] = index * 130
        snapshot["elements"].append(
            {
                "selector": f"#slot{index}",
                "parent_selector": "#categories",
                "tag": "li",
                "within_navigation": True,
                "rect": {"x": index * 130, "y": 0, "width": 130, "height": 40},
            }
        )
    snapshot["elements"][3]["tag"] = "ul"
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert subitem(outcome, "equal_width_navigation")["status"] == "pass"


def test_legacy_semantic_alignment_cannot_borrow_same_label_from_another_dom_region():
    dom, snapshot = navigation((100, 190, 100))
    dom = dom.replace("</body>", '<footer><a href="/0">分类0</a></footer></body>')
    outcome = judge_green_navigation(bundle(dom, snapshot))
    assert subitem(outcome, "equal_width_navigation")["status"] == "evidence_insufficient"
