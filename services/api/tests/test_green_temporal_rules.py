"""共享内核的业务事实分支测试；正式原件绑定另由绑定器回归证明。"""

import copy
from dataclasses import replace

import pytest

from services.api.app.application.green_fact_bindings import GreenFactView
from services.api.app.application.green_rule_catalog import GREEN_RULES
from services.api.app.application.green_temporal_rules import judge_temporal_rule


def target(kind="retry"):
    return {"kind": kind, "visible": True, "enabled": True, "hit": "target", "target_id": kind}


def case():
    temporal = {
        "window": {"start_ms": 0, "end_ms": 100, "clock_id": "clock", "coverage": "complete"},
        "state": {
            "module_id": "module",
            "request_id": "request",
            "outcome": "failure",
            "messages": [{"text": "网络异常，请重试", "visible": True}],
            "targets": [target(), target("guidance")],
            "illustrations": [{"visible": True, "role": "empty_state", "image_state": "decoded"}],
        },
        "fault": {
            "kind": "offline",
            "module_id": "module",
            "request_id": "request",
            "business_type": "payment",
        },
        "recovery": {
            "trigger_action_id": "action",
            "source_request_id": "request",
            "request_id": "retry-request",
            "outcome": "success",
            "line_before": "line-a",
            "line_after": "line-b",
        },
        "support": {"targets": [target("customer_service")]},
        "payment": {
            "phase": "critical",
            "start_ms": 10,
            "end_ms": 90,
            "clock_id": "clock",
            "events": [],
        },
        "loading": {
            "start_ms": 10,
            "end_ms": 90,
            "clock_id": "clock",
            "feedback": [
                {"start_ms": 10, "end_ms": 90, "kind": "progress", "visible": True},
            ],
        },
        "success": {
            "outcome": "success",
            "event_id": "business-result",
            "at_ms": 10,
            "clock_id": "clock",
            "baseline_ids": [],
            "feedback": [
                {
                    "kind": "toast",
                    "text": "操作成功",
                    "event_id": "new-toast",
                    "at_ms": 20,
                    "clock_id": "clock",
                    "business_event_id": "business-result",
                    "visible": True,
                }
            ],
        },
        "handoff": {
            "ad_presence": "present",
            "ad_id": "ad",
            "content_id": "content",
            "ad_started_at_ms": 0,
            "end": {"kind": "ended", "media_id": "ad", "at_ms": 50, "clock_id": "clock"},
            "preload": {
                "state": "ready",
                "content_id": "content",
                "started_at_ms": 10,
                "ready_at_ms": 40,
                "clock_id": "clock",
            },
            "first_frame": {
                "content_id": "content",
                "at_ms": 60,
                "clock_id": "clock",
                "artifact_id": "frame-png",
                "capture_id": "capture",
                "event_artifact_id": "presented-event",
            },
            "segments": [
                {"state": "ad", "media_id": "ad", "start_ms": 50, "end_ms": 55},
                {"state": "content", "media_id": "content", "start_ms": 55, "end_ms": 60},
            ],
        },
    }
    return GreenFactView(
        "module/request",
        (),
        {
            "temporal": temporal,
            "png_artifact_ids": ("frame-png",),
            "png_capture_ids": {"frame-png": "capture"},
            "presented_frame_events": (
                {
                    "event_artifact_id": "presented-event",
                    "capture_id": "capture",
                    "artifact_id": "frame-png",
                    "content_id": "content",
                    "at_ms": 60,
                    "clock_id": "clock",
                },
            ),
        },
        ("capture",),
        ("action",),
        ("temporal-original", "frame-png", "presented-event"),
    )


def changed(view, path, value):
    data = copy.deepcopy(view.values)
    row = data["temporal"]
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value
    return replace(view, values=data)


def judge(rule, key, *views):
    return judge_temporal_rule(rule, views or (case(),), (key,))[0]


PASS_CASES = [
    ("s72", "empty_state_meaning"),
    ("s72", "load_failure_meaning"),
    ("s49", "failure_reason"),
    ("s49", "retry_entry"),
    ("s49", "business_recovery"),
    ("s49", "video_line_recovery"),
    ("s49", "account_payment_support"),
    ("s49", "payment_not_interrupted"),
    ("s73", "loading_feedback"),
    ("s73", "empty_state_guidance"),
    ("s73", "business_success_feedback"),
    ("62", "natural_end_handoff"),
    ("62", "manual_close_handoff"),
    ("62", "content_preload"),
    ("62", "presented_first_frame"),
]


def applicable(key):
    view = case()
    if key in {"empty_state_meaning", "empty_state_guidance"}:
        view = changed(view, ("state", "outcome"), "empty")
        view = changed(view, ("state", "messages"), [{"text": "暂无数据", "visible": True}])
    if key == "video_line_recovery":
        view = changed(view, ("fault", "business_type"), "video")
    if key == "manual_close_handoff":
        view = changed(view, ("handoff", "end", "kind"), "closed")
        view = changed(view, ("handoff", "end", "action_id"), "action")
    return view


@pytest.mark.parametrize("rule,key", PASS_CASES)
def test_every_declared_temporal_subcheck_has_a_positive_business_branch(rule, key):
    item = judge(rule, key, applicable(key))
    assert item.status == "pass" and not item.missing
    assert item.capture_ids == ("capture",) and item.action_ids == ("action",)


FAIL_MUTATIONS = [
    ("s72", "empty_state_meaning", ("state", "messages"), []),
    ("s72", "load_failure_meaning", ("state", "messages"), [{"text": "暂无数据", "visible": True}]),
    ("s49", "failure_reason", ("state", "messages"), [{"text": "加载失败", "visible": True}]),
    ("s49", "retry_entry", ("state", "targets"), []),
    ("s49", "business_recovery", ("recovery", "outcome"), "failure"),
    ("s49", "video_line_recovery", ("recovery", "outcome"), "failure"),
    ("s49", "account_payment_support", ("support", "targets"), []),
    (
        "s49",
        "payment_not_interrupted",
        ("payment", "events"),
        [
            {
                "kind": "business_blocked",
                "origin": "system",
                "caused_by": "support",
                "at_ms": 50,
                "clock_id": "clock",
            }
        ],
    ),
    ("s73", "loading_feedback", ("loading", "feedback"), []),
    ("s73", "business_success_feedback", ("success", "feedback"), []),
    ("62", "natural_end_handoff", ("handoff", "segments", 0, "state"), "blank"),
    ("62", "manual_close_handoff", ("handoff", "segments", 0, "state"), "error"),
    ("62", "content_preload", ("handoff", "preload", "ready_at_ms"), 51),
]


@pytest.mark.parametrize("rule,key,path,value", FAIL_MUTATIONS)
def test_observed_business_counterexample_fails_current_subcheck(rule, key, path, value):
    assert judge(rule, key, changed(applicable(key), path, value)).status == "fail"


def test_empty_guidance_and_actual_first_frame_have_negative_branches():
    view = changed(applicable("empty_state_guidance"), ("state", "illustrations"), [])
    view = changed(view, ("state", "targets"), [])
    assert judge("s73", "empty_state_guidance", view).status == "fail"
    view = changed(
        case(),
        ("handoff", "first_frame"),
        {
            "state": "render_failed",
            "error_code": "decoder_error",
            "content_id": "content",
            "at_ms": 60,
            "clock_id": "clock",
        },
    )
    assert judge("62", "presented_first_frame", view).status == "fail"


@pytest.mark.parametrize("rule,key", PASS_CASES)
def test_reliable_condition_exclusion_is_skipped_not_pass(rule, key):
    view = applicable(key)
    if rule == "s72" or key == "empty_state_guidance":
        view = changed(view, ("state", "outcome"), "nonempty")
    elif rule == "s49":
        view = changed(view, ("fault", "kind"), "none")
        view = changed(view, ("fault", "business_type"), "general")
    elif key == "loading_feedback":
        view = changed(view, ("loading", "end_ms"), 10)
    elif key == "business_success_feedback":
        view = changed(view, ("success", "outcome"), "failure")
    else:
        view = changed(view, ("handoff", "ad_presence"), "absent")
    assert judge(rule, key, view).status == "skipped"


@pytest.mark.parametrize("rule,key", PASS_CASES)
def test_reader_gap_or_absent_view_cannot_become_business_pass(rule, key):
    view = replace(applicable(key), issues=("original_checksum_changed",))
    assert judge(rule, key, view).status == "evidence_insufficient"
    assert judge_temporal_rule(rule, (), (key,))[0].status == "evidence_insufficient"


@pytest.mark.parametrize(
    "key,path,value",
    [
        ("failure_reason", ("state", "request_id"), "other-request"),
        ("business_recovery", ("recovery", "source_request_id"), "other-request"),
        ("business_recovery", ("recovery", "trigger_action_id"), "foreign-action"),
        ("video_line_recovery", ("fault", "business_type"), "unknown"),
        ("payment_not_interrupted", ("payment", "phase"), "unknown"),
    ],
)
def test_fault_branch_identity_and_business_classification_are_mandatory(key, path, value):
    assert (
        judge("s49", key, changed(applicable(key), path, value)).status == "evidence_insufficient"
    )


@pytest.mark.parametrize(
    "key",
    [
        "retry_entry",
        "failure_reason",
        "payment_not_interrupted",
        "loading_feedback",
        "empty_state_guidance",
        "business_success_feedback",
        "natural_end_handoff",
        "presented_first_frame",
    ],
)
def test_partial_window_cannot_prove_absence_or_complete_process(key):
    rule = (
        "s49"
        if key in {"retry_entry", "failure_reason", "payment_not_interrupted"}
        else ("62" if key in {"natural_end_handoff", "presented_first_frame"} else "s73")
    )
    view = changed(applicable(key), ("window", "coverage"), "partial")
    if key == "retry_entry":
        view = changed(view, ("state", "targets"), [])
    if key == "failure_reason":
        view = changed(view, ("state", "messages"), [])
    if key == "empty_state_guidance":
        view = changed(view, ("state", "illustrations"), [])
        view = changed(view, ("state", "targets"), [])
    assert judge(rule, key, view).status == "evidence_insufficient"


@pytest.mark.parametrize("fault", ["old", "unrelated", "early", "wrong_clock"])
def test_success_feedback_must_be_new_related_and_in_the_same_clock(fault):
    view = case()
    path = ("success", "feedback", 0)
    if fault == "old":
        view = changed(view, ("success", "baseline_ids"), ["new-toast"])
    elif fault == "unrelated":
        view = changed(view, (*path, "business_event_id"), "other-business")
    elif fault == "early":
        view = changed(view, (*path, "at_ms"), 9)
    else:
        view = changed(view, (*path, "clock_id"), "other-clock")
    expected = "evidence_insufficient" if fault == "wrong_clock" else "fail"
    assert judge("s73", "business_success_feedback", view).status == expected


def test_loading_intervals_merge_without_inventing_a_product_delay_threshold():
    view = changed(
        case(),
        ("loading", "feedback"),
        [
            {"start_ms": 10, "end_ms": 30, "kind": "spinner", "visible": True},
            {"start_ms": 30, "end_ms": 90, "kind": "text", "visible": True},
        ],
    )
    assert judge("s73", "loading_feedback", view).status == "pass"
    view = changed(view, ("loading", "feedback", 1, "start_ms"), 30.0001)
    assert judge("s73", "loading_feedback", view).status == "fail"


@pytest.mark.parametrize("fault", ["foreign_png", "not_png", "wrong_content", "clock", "gap"])
def test_media_cannot_replace_actual_first_frame_or_full_handoff_with_summary(fault):
    view = case()
    if fault == "foreign_png":
        view = changed(view, ("handoff", "first_frame", "artifact_id"), "foreign")
    elif fault == "not_png":
        view = replace(view, values={"temporal": view.get("temporal"), "png_artifact_ids": ()})
    elif fault == "wrong_content":
        view = changed(view, ("handoff", "first_frame", "content_id"), "other-content")
    elif fault == "clock":
        view = changed(view, ("handoff", "first_frame", "clock_id"), "other-clock")
    else:
        view = changed(view, ("handoff", "segments", 1, "start_ms"), 56)
    assert judge("62", "natural_end_handoff", view).status == "evidence_insufficient"


def test_support_entry_or_open_event_does_not_equal_payment_interruption():
    view = changed(
        case(),
        ("payment", "events"),
        [
            {
                "kind": "support_open",
                "origin": "system",
                "at_ms": 50,
                "clock_id": "clock",
            }
        ],
    )
    assert judge("s49", "payment_not_interrupted", view).status == "pass"


def test_failure_and_missing_other_instance_are_both_preserved():
    bad = changed(case(), ("state", "targets"), [])
    missing = replace(case(), subject_id="other", issues=("other_original_missing",))
    item = judge("s49", "retry_entry", case(), bad, missing)
    assert item.status == "fail" and "other_original_missing" in item.missing


def test_all_declared_subchecks_route_and_unknown_values_are_not_accepted():
    for rule in GREEN_RULES:
        if rule.key in {"s72", "s49", "s73", "62"}:
            assert len(judge_temporal_rule(rule.key, (case(),), rule.subchecks)) == len(
                rule.subchecks
            )
    malformed = changed(case(), ("window", "start_ms"), True)
    assert judge("s73", "loading_feedback", malformed).status == "evidence_insufficient"
    with pytest.raises(ValueError, match="不支持"):
        judge_temporal_rule("s72", (case(),), ("content_preload",))


@pytest.mark.parametrize("area", ["loading", "payment"])
def test_interval_without_native_clock_remains_unknown(area):
    view = changed(case(), (area, "clock_id"), None)
    rule, key = (
        ("s73", "loading_feedback") if area == "loading" else ("s49", "payment_not_interrupted")
    )
    assert judge(rule, key, view).status == "evidence_insufficient"


def test_success_message_content_and_all_record_shapes_are_checked_before_pass():
    wrong = changed(case(), ("success", "feedback", 0, "text"), "操作失败")
    assert judge("s73", "business_success_feedback", wrong).status == "fail"
    missing = changed(case(), ("success", "feedback", 0, "text"), None)
    assert judge("s73", "business_success_feedback", missing).status == "evidence_insufficient"
    records = case().get("temporal")["success"]["feedback"] + [{"at_ms": "broken"}]
    malformed = changed(case(), ("success", "feedback"), records)
    assert judge("s73", "business_success_feedback", malformed).status == "evidence_insufficient"


def test_support_block_with_unknown_causality_cannot_prove_payment_safe():
    view = changed(
        case(),
        ("payment", "events"),
        [
            {
                "kind": "business_blocked",
                "at_ms": 50,
                "clock_id": "clock",
            }
        ],
    )
    assert judge("s49", "payment_not_interrupted", view).status == "evidence_insufficient"


def test_preload_late_start_and_explicit_absence_are_counterexamples():
    view = changed(case(), ("handoff", "preload", "started_at_ms"), 51)
    assert judge("62", "content_preload", view).status == "fail"
    view = changed(case(), ("handoff", "preload", "state"), "not_started")
    assert judge("62", "content_preload", view).status == "fail"
    view = changed(view, ("window", "coverage"), "partial")
    assert judge("62", "content_preload", view).status == "evidence_insufficient"


def test_s72_ambiguous_copy_goes_to_review_and_failure_placeholder_needs_full_window():
    view = changed(case(), ("state", "messages"), [{"text": "暂时无法展示", "visible": True}])
    assert judge("s72", "load_failure_meaning", view).status == "manual_review"
    view = changed(view, ("state", "messages"), [{"text": "暂无数据", "visible": True}])
    view = changed(view, ("window", "coverage"), "partial")
    assert judge("s72", "load_failure_meaning", view).status == "evidence_insufficient"


@pytest.mark.parametrize(
    "fault",
    [
        "unrelated_png",
        "wrong_capture",
        "wrong_event",
        "wrong_media",
        "wrong_clock",
        "wrong_time",
        "duplicate_event",
        "missing_owner",
        "missing_events",
    ],
)
def test_first_frame_requires_exact_server_derived_native_event_and_png_owner(fault):
    view = case()
    data = copy.deepcopy(view.values)
    if fault == "unrelated_png":
        data["temporal"]["handoff"]["first_frame"].update(
            artifact_id="unrelated-png",
            capture_id="unrelated-capture",
        )
        data["png_artifact_ids"] += ("unrelated-png",)
        data["png_capture_ids"]["unrelated-png"] = "unrelated-capture"
        view = replace(
            view,
            capture_ids=view.capture_ids + ("unrelated-capture",),
            artifact_ids=view.artifact_ids + ("unrelated-png",),
        )
    elif fault == "wrong_capture":
        data["temporal"]["handoff"]["first_frame"]["capture_id"] = "foreign-capture"
    elif fault == "wrong_event":
        data["temporal"]["handoff"]["first_frame"]["event_artifact_id"] = "temporal-original"
    elif fault in {"wrong_media", "wrong_clock", "wrong_time"}:
        key, value = {
            "wrong_media": ("content_id", "foreign-content"),
            "wrong_clock": ("clock_id", "foreign-clock"),
            "wrong_time": ("at_ms", 61),
        }[fault]
        data["presented_frame_events"][0][key] = value
    elif fault == "duplicate_event":
        data["presented_frame_events"] *= 2
    elif fault == "missing_owner":
        data.pop("png_capture_ids")
    else:
        data.pop("presented_frame_events")
    view = replace(view, values=data)
    item = judge("62", "presented_first_frame", view)
    assert item.status == "evidence_insufficient"
    assert "matching_first_frame_native_event" in item.missing
