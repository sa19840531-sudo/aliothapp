"""核验绑定导航事实的正式共享判规，覆盖三个分项及跨对象缺证保留。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from services.api.app.application.green_fact_bindings import GreenFactView
from services.api.app.application.green_navigation_rules import judge_navigation_subitem

KEYS = (
    "equal_width_navigation",
    "fit_navigation_spacing",
    "overflow_scroll",
    "initial_next_label",
)


def rect(x, y=0, width=20, height=20):
    return {"x": x, "y": y, "width": width, "height": height}


def navigation(*, count=5, overflow=False):
    width = 50 if overflow else 100
    clip = rect(0, width=width, height=40)
    siblings = [
        {
            "id": f"item-{index}",
            "ordinal": index + 1,
            "text": f"分类{index}",
            "slot_rect": rect(index * 20, width=20, height=40),
            "text_rects": [rect(index * 20 + 5, y=10, width=10)],
            "clip_rect": deepcopy(clip),
        }
        for index in range(count)
    ]
    return {
        "siblings_complete": True,
        "text_ranges_complete": True,
        "geometry_axis_aligned": True,
        "geometry_precision": "raw",
        "coordinate_space": "viewport_css_px",
        "document_id": "document-1",
        "viewport": {"width": width, "height": 600, "scale": 1},
        "container": {
            "id": "container-1",
            "selector": "#categories",
            "direction": "ltr",
            "rect": clip,
            "clip_rect": deepcopy(clip),
            "scroll_left": 0,
            "client_width": width,
            "scroll_width": count * 20,
        },
        "siblings": siblings,
        "initial": {"is_initial": True, "prior_horizontal_actions": 0, "scroll_left": 0},
        "scroll_probe": {
            "action_id": "action-1",
            "before_capture_id": "before",
            "after_capture_id": "after",
            "before_document_id": "document-1",
            "after_document_id": "document-1",
            "before_container_id": "container-1",
            "after_container_id": "container-1",
            "completed": True,
            "window_complete": True,
            "gesture_delivered": True,
            "interrupted": False,
            "position_kind": "scroll_left",
            "direction": "forward",
            "before_scroll_left": 0,
            "after_scroll_left": 10,
        },
    }


def view(data=None, *, subject="navigation-1", issues=()):
    return GreenFactView(
        subject,
        issues,
        {"navigation": navigation() if data is None else data},
        ("before", "after"),
        ("action-1",),
        ("ui-original", "gesture-original"),
    )


def judge(key, data):
    return judge_navigation_subitem(key, (view(data),))


def test_five_complete_labels_with_equal_real_text_gaps_pass():
    outcome = judge("fit_navigation_spacing", navigation())
    assert outcome.status == "pass" and not outcome.missing
    assert outcome.capture_ids == ("before", "after")


@pytest.mark.parametrize("fault", ["unequal_gap", "overlap", "actual_clipping"])
def test_fitting_navigation_reports_actual_text_or_spacing_counterexample(fault):
    data = navigation()
    label = data["siblings"][2]
    if fault == "unequal_gap":
        label["text_rects"][0]["x"] += 1
    elif fault == "overlap":
        label["text_rects"][0]["x"] -= 15
    else:
        label["clip_rect"] = rect(45, 10, 4, 20)
    assert judge("fit_navigation_spacing", data).status == "fail"


@pytest.mark.parametrize("fault", ["no_ranges", "missing_clip", "multiline", "rounded"])
def test_missing_text_paint_geometry_never_becomes_text_failure(fault):
    data = navigation()
    if fault == "no_ranges":
        data["text_ranges_complete"] = False
    elif fault == "missing_clip":
        data["siblings"][2].pop("clip_rect")
    elif fault == "multiline":
        data["siblings"][2]["text_rects"].append(rect(45, 40, 10, 20))
    else:
        data["geometry_precision"] = "rounded_2dp"
    outcome = judge("fit_navigation_spacing", data)
    assert outcome.status == "evidence_insufficient" and outcome.missing


def test_element_overflow_flags_do_not_substitute_real_text_clipping():
    data = navigation()
    for label in data["siblings"]:
        label["horizontal_overflow"] = True
        label["text_overflow"] = "ellipsis"
    assert judge("fit_navigation_spacing", data).status == "pass"
    data["text_ranges_complete"] = False
    assert judge("fit_navigation_spacing", data).status == "evidence_insufficient"


def test_overflowing_slots_exclude_fit_branch_and_fitting_slots_exclude_scroll_branch():
    assert judge("fit_navigation_spacing", navigation(overflow=True)).status == "skipped"
    assert judge("overflow_scroll", navigation()).status == "skipped"
    for key in ("fit_navigation_spacing", "overflow_scroll"):
        assert judge(key, navigation(count=3)).status == "skipped"


def test_real_same_container_gesture_position_change_proves_horizontal_scroll():
    outcome = judge("overflow_scroll", navigation(overflow=True))
    assert outcome.status == "pass" and outcome.action_ids == ("action-1",)


def test_delivered_nonboundary_gesture_with_complete_unchanged_position_fails():
    data = navigation(overflow=True)
    data["scroll_probe"]["after_scroll_left"] = 0
    assert judge("overflow_scroll", data).status == "fail"


@pytest.mark.parametrize(
    "fault",
    [
        "css_only",
        "interrupted",
        "incomplete_window",
        "undelivered",
        "wrong_container",
        "wrong_document",
        "foreign_action",
        "foreign_capture",
        "reused_capture",
        "no_offset",
        "out_of_range",
        "anchor_drift",
        "boundary",
        "unknown_mechanism",
    ],
)
def test_css_scope_or_incomplete_gesture_never_proves_scroll_behavior(fault):
    data = navigation(overflow=True)
    probe = data["scroll_probe"]
    if fault == "css_only":
        data.pop("scroll_probe")
        data["container"]["overflow_x"] = "auto"
    elif fault == "interrupted":
        probe["interrupted"] = True
    elif fault == "incomplete_window":
        probe["window_complete"] = False
    elif fault == "undelivered":
        probe["gesture_delivered"] = False
    elif fault == "wrong_container":
        probe["after_container_id"] = "other"
    elif fault == "wrong_document":
        probe["after_document_id"] = "other"
    elif fault == "foreign_action":
        probe["action_id"] = "other"
    elif fault == "foreign_capture":
        probe["after_capture_id"] = "other"
    elif fault == "reused_capture":
        probe["after_capture_id"] = "before"
    elif fault == "no_offset":
        probe.pop("after_scroll_left")
    elif fault == "out_of_range":
        probe["after_scroll_left"] = 60
    elif fault == "anchor_drift":
        data["container"]["scroll_left"] = 5
    elif fault == "boundary":
        probe.update(direction="backward", after_scroll_left=0)
    else:
        probe["position_kind"] = "transform_without_native_binding"
    assert judge("overflow_scroll", data).status == "evidence_insufficient"


def test_initial_next_label_requires_partial_real_text_not_merely_visible_slot():
    data = navigation(overflow=True)
    assert judge("initial_next_label", data).status == "pass"
    data["siblings"][2]["text_rects"] = [rect(50, 10, 10, 20)]
    assert judge("initial_next_label", data).status == "fail"


def test_no_remaining_right_label_reliably_excludes_initial_exposure_condition():
    assert judge("initial_next_label", navigation()).status == "skipped"


@pytest.mark.parametrize("fault", ["middle_frame", "prior_scroll", "scroll_drift", "missing_text"])
def test_zero_document_scroll_or_midway_capture_does_not_prove_initial_state(fault):
    data = navigation(overflow=True)
    if fault == "middle_frame":
        data["initial"]["is_initial"] = False
    elif fault == "prior_scroll":
        data["initial"]["prior_horizontal_actions"] = 1
    elif fault == "scroll_drift":
        data["initial"]["scroll_left"] = 5
    else:
        data["text_ranges_complete"] = False
    assert judge("initial_next_label", data).status == "evidence_insufficient"


@pytest.mark.parametrize("fault", ["partial", "duplicate", "axis", "clip", "huge_number"])
@pytest.mark.parametrize("key", KEYS)
def test_unverified_complete_navigation_geometry_never_passes(key, fault):
    data = navigation()
    if fault == "partial":
        data["siblings_complete"] = False
    elif fault == "duplicate":
        data["siblings"][1]["id"] = data["siblings"][0]["id"]
    elif fault == "axis":
        data["geometry_axis_aligned"] = False
    elif fault == "clip":
        data["container"]["clip_rect"]["width"] = 110
    else:
        data["siblings"][0]["slot_rect"]["x"] = 10**400
    assert judge(key, data).status == "evidence_insufficient"


def test_failure_does_not_drop_another_objects_missing_originals():
    data = navigation()
    data["siblings"][1]["clip_rect"] = rect(25, 10, 1, 20)
    outcome = judge_navigation_subitem(
        "fit_navigation_spacing",
        (
            view(data),
            view(subject="later-navigation", issues=("original_hash_mismatch",)),
        ),
    )
    assert outcome.status == "fail" and "original_hash_mismatch" in outcome.missing


def test_no_view_or_untyped_claim_keeps_missing_and_unsupported_subcheck_raises():
    for values in ((), ({"navigation": navigation()},)):
        assert (
            judge_navigation_subitem("fit_navigation_spacing", values).status
            == "evidence_insufficient"
        )
    with pytest.raises(ValueError, match="不支持"):
        judge_navigation_subitem("unsupported_navigation_subcheck", (view(),))


def test_skipped_and_positive_objects_aggregate_without_hiding_unknown_later_object():
    positive = view()
    excluded = view(navigation(count=3), subject="small-navigation")
    assert judge_navigation_subitem("fit_navigation_spacing", (positive, excluded)).status == "pass"
    broken = replace(view(), issues=("incomplete_later_capture",))
    outcome = judge_navigation_subitem("fit_navigation_spacing", (positive, excluded, broken))
    assert outcome.status == "evidence_insufficient" and outcome.missing


@pytest.mark.parametrize("count", [2, 3, 4])
def test_bound_equal_width_slots_centered_in_equal_regions_pass(count):
    data = navigation(count=count)
    for index, row in enumerate(data["siblings"]):
        row["slot_rect"]["x"] = (index + 0.5) * 100 / count - 10
    if count == 3:
        # 三等分使用可精确表示的容器宽度，不把舍入差异伪装成原始几何。
        data["viewport"]["width"] = 120
        for key in ("rect", "clip_rect"):
            data["container"][key]["width"] = 120
        for index, row in enumerate(data["siblings"]):
            row["slot_rect"]["x"] = index * 40 + 10
    assert judge("equal_width_navigation", data).status == "pass"


@pytest.mark.parametrize("fault", ["unequal_width", "unequal_spacing", "overlap"])
def test_bound_original_slot_counterexamples_fail_equal_distribution(fault):
    data = navigation(count=3)
    if fault == "unequal_width":
        data["siblings"][1]["slot_rect"]["width"] = 21
    elif fault == "unequal_spacing":
        data["siblings"][1]["slot_rect"]["x"] += 1
    else:
        data["siblings"][1]["slot_rect"]["x"] -= 1
    assert judge("equal_width_navigation", data).status == "fail"


def test_bound_equal_width_five_classes_excluded_and_incomplete_distribution_unknown():
    assert judge("equal_width_navigation", navigation()).status == "skipped"
    assert judge("equal_width_navigation", navigation(count=3)).status == "evidence_insufficient"
