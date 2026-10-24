"""共享S35内核的原始几何、正式动作、独立重置和响应合同测试。"""

import copy

import pytest

from services.api.app.application.green_fact_bindings import GreenFactView
from services.api.app.application.green_layout_stability_rules import judge_interaction_subitem


def fact(values, *, issues=(), actions=("click-1", "reset-1", "click-2"), subject="list-1"):
    return GreenFactView(subject, issues, values, ("before", "after"), actions, ("raw",))


def layout_values():
    rect = {"x": 10, "y": 20, "width": 100, "height": 40}
    target = {
        "target_id": "button",
        "rect": rect,
        "enabled": True,
        "required_in_current_state": True,
        "activation_regions": [rect],
        "hit_regions_complete": True,
        "owner_layer_id": "content",
        "hit_regions": [{"rect": rect, "receiver_id": "button", "ancestor_ids": []}],
    }
    frame = {
        "at_ms": 0,
        "clock_id": "native-clock",
        "viewport": {"x": 0, "y": 0, "width": 390, "height": 600},
        "interactive_target_ids": ["button"],
        "targets": [target],
        "layers": [],
    }
    final = copy.deepcopy(frame)
    final["at_ms"] = 20
    return {
        "layout_window": {
            "clock_id": "native-clock",
            "start_ms": 0,
            "end_ms": 20,
            "complete": True,
        },
        "layout_frames": [frame, final],
    }


def response_values(*, changed=False):
    expected = {"kind": "panel_state", "subject_id": "panel", "state": {"expanded": True}}
    record = {
        "action_id": "click-1",
        "session_id": "session",
        "target_id": "button",
        "operation": "click",
        "clock_id": "native-clock",
        "started_at_ms": 1,
        "finished_at_ms": 5,
        "expected_response": expected,
        "input_event": {
            "action_id": "click-1",
            "operation": "click",
            "target_id": "button",
            "clock_id": "native-clock",
            "at_ms": 2,
            "source": "browser_native_event",
            "is_trusted": True,
        },
        "response": {
            "kind": "panel_state",
            "subject_id": "panel",
            "state": {"expanded": not changed},
            "observed_at_ms": 6,
        },
        "response_window": {
            "clock_id": "native-clock",
            "start_ms": 0,
            "end_ms": 10,
            "complete": True,
        },
    }
    return {"response_actions": [record]}


def stability_values(*, changed=False):
    first = response_values()["response_actions"][0]
    first["response"]["subject_id"] = "button"
    first["preconditions"] = {
        "schema_id": "panel-prestate.v1",
        "complete": True,
        "required_keys": ["route", "expanded", "account"],
        "values": {"route": "/list", "expanded": False, "account": "user-1"},
    }
    second = copy.deepcopy(first)
    second.update(
        action_id="click-2",
        started_at_ms=20,
        finished_at_ms=25,
        reset={
            "action_id": "reset-1",
            "clock_id": "native-clock",
            "session_id": "session",
            "started_at_ms": 11,
            "finished_at_ms": 19,
            "restored_values": copy.deepcopy(first["preconditions"]["values"]),
        },
    )
    second["response"].update(observed_at_ms=26, state={"expanded": not changed})
    second["input_event"].update(action_id="click-2", at_ms=21)
    second["response_window"].update(start_ms=20, end_ms=30)
    return {"repeat_safety": "read_only", "repeat_actions": [first, second]}


def test_complete_unobstructed_layout_and_native_descendant_hit_pass():
    values = layout_values()
    for frame in values["layout_frames"]:
        frame["targets"][0]["hit_regions"][0].update(
            receiver_id="button-label", ancestor_ids=["button"]
        )
    assert judge_interaction_subitem("usable_layout", [fact(values)]).status == "pass"


def test_persistently_fully_blocked_required_control_is_explicit_failure():
    values = layout_values()
    for frame in values["layout_frames"]:
        frame["targets"][0]["hit_regions"][0]["receiver_id"] = "other-content"
    item = judge_interaction_subitem("usable_layout", [fact(values)])
    assert item.status == "fail" and not item.missing
    assert item.capture_ids == ("before", "after")


def test_legal_modal_suspends_inert_background_but_keeps_own_controls_required():
    values = layout_values()
    for frame in values["layout_frames"]:
        background = frame["targets"][0]
        background["inert"] = True
        background["hit_regions"][0]["receiver_id"] = "modal"
        close = copy.deepcopy(background)
        close.update(target_id="close", owner_layer_id="modal", inert=False)
        close["hit_regions"][0]["receiver_id"] = "close"
        frame["targets"].append(close)
        frame["interactive_target_ids"].append("close")
        frame["layers"] = [
            {
                "layer_id": "modal",
                "kind": "modal_dialog",
                "active": True,
                "modal": True,
                "suspended_target_ids": ["button"],
            }
        ]
    assert judge_interaction_subitem("usable_layout", [fact(values)]).status == "pass"
    for frame in values["layout_frames"]:
        frame["targets"][1]["hit_regions"][0]["receiver_id"] = "foreign-content"
    assert judge_interaction_subitem("usable_layout", [fact(values)]).status == "fail"


@pytest.mark.parametrize(
    "fault",
    [
        "partial",
        "coverage",
        "conflicting",
        "clip",
        "census",
        "window",
        "transition",
        "modal_unknown",
        "frame_clock",
    ],
)
def test_ambiguous_layout_or_partial_occlusion_does_not_invent_failure_or_pass(fault):
    values = layout_values()
    for frame in values["layout_frames"]:
        target = frame["targets"][0]
        if fault in {"partial", "coverage"}:
            target["hit_regions"][0]["rect"] = {"x": 10, "y": 20, "width": 50, "height": 40}
            if fault == "partial":
                target["hit_regions"].append(
                    {
                        "rect": {"x": 60, "y": 20, "width": 50, "height": 40},
                        "receiver_id": "other",
                        "ancestor_ids": [],
                    }
                )
        elif fault == "conflicting":
            target["hit_regions"].append({**target["hit_regions"][0], "receiver_id": "other"})
        elif fault == "clip":
            frame["viewport"]["width"] = 50
        elif fault == "census":
            frame["interactive_target_ids"].append("missing-target")
        elif fault == "modal_unknown":
            frame["layers"] = [
                {
                    "layer_id": "layer",
                    "kind": "popover",
                    "active": True,
                    "modal": False,
                    "suspended_target_ids": ["button"],
                }
            ]
        elif fault == "frame_clock":
            frame["clock_id"] = "another-clock"
    if fault == "window":
        values["layout_window"]["complete"] = False
    if fault == "transition":
        values["layout_frames"][0]["targets"][0]["hit_regions"][0]["receiver_id"] = "animation"
    assert (
        judge_interaction_subitem("usable_layout", [fact(values)]).status == "evidence_insufficient"
    )


def test_disabled_current_layout_can_be_reliably_not_applicable():
    values = layout_values()
    for frame in values["layout_frames"]:
        frame["targets"][0]["enabled"] = False
    assert judge_interaction_subitem("usable_layout", [fact(values)]).status == "skipped"


def test_same_complete_prestate_independent_reset_and_same_response_pass():
    assert (
        judge_interaction_subitem("comparable_state_stability", [fact(stability_values())]).status
        == "pass"
    )


def test_comparable_repeated_action_with_different_response_is_failure():
    assert (
        judge_interaction_subitem(
            "comparable_state_stability", [fact(stability_values(changed=True))]
        ).status
        == "fail"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "same_id",
        "unknown_action",
        "different_prestate",
        "missing_key",
        "reset",
        "reset_id",
        "overlap",
        "clock",
        "window",
        "unsafe",
        "different_operation",
        "wrong_response_subject",
        "missing_response",
        "reset_clock",
        "reset_session",
    ],
)
def test_unproven_comparability_and_unsafe_operations_remain_unknown(fault):
    values = stability_values()
    first, second = values["repeat_actions"]
    if fault == "same_id":
        second["action_id"] = first["action_id"]
    elif fault == "unknown_action":
        second["action_id"] = "not-in-ledger"
    elif fault == "different_prestate":
        second["preconditions"]["values"]["account"] = "another-user"
    elif fault == "missing_key":
        del second["preconditions"]["values"]["expanded"]
    elif fault == "reset":
        second["reset"]["restored_values"]["expanded"] = True
    elif fault == "reset_id":
        second["reset"]["action_id"] = first["action_id"]
    elif fault == "overlap":
        second["reset"]["started_at_ms"] = 1
    elif fault == "clock":
        second["clock_id"] = "other-clock"
    elif fault == "window":
        second["response_window"]["complete"] = False
    elif fault == "unsafe":
        values["repeat_safety"] = "unsafe"
    elif fault == "different_operation":
        second["operation"] = "tap"
    elif fault == "wrong_response_subject":
        second["response"]["subject_id"] = "unrelated-control"
    elif fault == "reset_clock":
        second["reset"]["clock_id"] = "another-clock"
    elif fault == "reset_session":
        second["reset"]["session_id"] = "another-session"
    else:
        second.pop("response")
    item = judge_interaction_subitem("comparable_state_stability", [fact(values)])
    assert item.status == "evidence_insufficient" and item.missing


@pytest.mark.parametrize("changed,expected", [(False, "pass"), (True, "fail")])
def test_formal_declared_response_is_compared_to_native_actual_response(changed, expected):
    values = response_values(changed=changed)
    assert judge_interaction_subitem("correct_response", [fact(values)]).status == expected


@pytest.mark.parametrize(
    "fault",
    [
        "expected_boolean",
        "unknown_action",
        "missing_window",
        "missing_field",
        "unknown_clock",
        "driver_api_only",
        "synthetic_dom_event",
    ],
)
def test_response_requires_expected_contract_and_complete_native_observation(fault):
    values = response_values()
    action = values["response_actions"][0]
    if fault == "expected_boolean":
        action["expected_response"] = True
    elif fault == "unknown_action":
        action["action_id"] = "page-claimed-action"
    elif fault == "missing_window":
        action["response_window"]["complete"] = False
    elif fault == "missing_field":
        action["response"]["state"] = {"some_other_state": True}
    elif fault == "driver_api_only":
        action.pop("input_event")
    elif fault == "synthetic_dom_event":
        action["input_event"]["is_trusted"] = False
    else:
        action["clock_id"] = "another-clock"
    assert (
        judge_interaction_subitem("correct_response", [fact(values)]).status
        == "evidence_insufficient"
    )


@pytest.mark.parametrize("key", ["correct_response", "usable_layout", "comparable_state_stability"])
def test_known_failure_survives_other_subject_missing_and_binding_issues_do_not_pass(key):
    values = (
        response_values(changed=True)
        if key == "correct_response"
        else stability_values(changed=True)
        if key == "comparable_state_stability"
        else layout_values()
    )
    if key == "usable_layout":
        for frame in values["layout_frames"]:
            frame["targets"][0]["hit_regions"][0]["receiver_id"] = "other"
    item = judge_interaction_subitem(key, [fact(values), fact({}, subject="missing")])
    assert item.status == "fail" and item.missing
    assert (
        judge_interaction_subitem(key, [fact(values, issues=("original_hash_drift",))]).status
        == "evidence_insufficient"
    )


def test_no_fact_or_malformed_geometry_never_passes_and_unknown_key_is_rejected():
    assert judge_interaction_subitem("usable_layout", []).status == "evidence_insufficient"
    values = layout_values()
    values["layout_frames"][0]["targets"][0]["rect"]["width"] = float("nan")
    assert (
        judge_interaction_subitem("usable_layout", [fact(values)]).status == "evidence_insufficient"
    )
    with pytest.raises(ValueError, match="未声明"):
        judge_interaction_subitem("unknown", [])


def test_layout_partition_union_uses_exact_coverage_and_does_not_count_overlapping_area_twice():
    values = layout_values()
    for frame in values["layout_frames"]:
        target = frame["targets"][0]
        target["hit_regions"] = [
            {
                "rect": {"x": 10, "y": 20, "width": 50.01, "height": 40},
                "receiver_id": "button",
                "ancestor_ids": [],
            },
            {
                "rect": {"x": 60, "y": 20, "width": 50, "height": 40},
                "receiver_id": "button",
                "ancestor_ids": [],
            },
        ]
    assert judge_interaction_subitem("usable_layout", [fact(values)]).status == "pass"
    for frame in values["layout_frames"]:
        frame["targets"][0]["hit_regions"][0]["rect"]["width"] = 49.99
    assert (
        judge_interaction_subitem("usable_layout", [fact(values)]).status == "evidence_insufficient"
    )


def test_known_comparable_response_counterexample_survives_later_incomplete_action_window():
    values = stability_values(changed=True)
    third = copy.deepcopy(values["repeat_actions"][1])
    third.update(action_id="click-3", started_at_ms=40, finished_at_ms=45)
    third["response_window"].update(start_ms=40, end_ms=50, complete=False)
    values["repeat_actions"].append(third)
    item = judge_interaction_subitem(
        "comparable_state_stability",
        [
            fact(
                values,
                actions=("click-1", "reset-1", "click-2", "click-3"),
            )
        ],
    )
    assert item.status == "fail" and item.missing


def test_formal_expected_object_mismatch_is_a_response_counterexample():
    values = response_values()
    values["response_actions"][0]["response"]["subject_id"] = "another-panel"
    assert judge_interaction_subitem("correct_response", [fact(values)]).status == "fail"
