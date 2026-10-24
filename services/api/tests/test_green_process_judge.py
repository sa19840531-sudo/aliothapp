"""受控原件夹具验证只读判法；不代表三端真实业务采集或产品验收。"""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from services.api.app.application.green_evidence_contracts import (
    GreenAction,
    GreenCapture,
    GreenOriginal,
    _verified_evidence_set,
)
from services.api.app.application.green_process_judge import judge_green_process_subitems
from services.api.app.application.green_rule_evidence import (
    _MISSING_FACTS,
    evaluate_green_aggregate_context,
)
from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.tests.test_green_process_evidence import NOW, FixtureConnection, context


def original(capture_id, artifact_type, payload):
    content = json.dumps(payload).encode()
    return GreenOriginal(
        artifact_type + capture_id,
        artifact_type,
        capture_id,
        "sha256:" + hashlib.sha256(content).hexdigest(),
        content,
    )


def process_case(goal="scroll_button_feedback", *, conflict=False):
    fixture = FixtureConnection()
    button = {
        "selector": "#control",
        "tag": "button",
        "role": "button",
        "text": "返回顶部" if goal == "scroll_back_to_top" else "详情",
        "rect": {"x": 10, "y": 100, "width": 180, "height": 40},
        "interactive": True,
        "visible": True,
        "h5_probe_local_display": True,
        "hit_test": {"center_hits_target": True},
        "class_tokens": ["van-collapse-item__title"],
        "aria_expanded": "false",
        "aria_controls": "region",
        "h5_probe_controlled_region": {
            "unique": True,
            "id": "region",
            "visible": False,
            "width": 200,
            "height": 0,
        },
    }
    before = {
        "schema_version": "ui-snapshot.v2",
        "captured_at": NOW.isoformat(),
        "performance_now_ms": 10,
        "url": "https://controlled.invalid/list",
        "document": {"time_origin_ms": 1234, "scroll_height": 2000},
        "viewport": {"width": 390, "height": 600, "scroll_y": 1400},
        "element_count": 1,
        "element_limit_reached": False,
        "elements": [button],
    }
    subject = select_probe_target(before, goal=goal, page_type="video_list")
    assert subject is not None
    after = copy.deepcopy(before)
    after.update(captured_at=(NOW + timedelta(milliseconds=10)).isoformat(), performance_now_ms=20)
    if goal == "scroll_back_to_top":
        after["viewport"]["scroll_y"] = 0
    else:
        target = after["elements"][0]
        target["aria_expanded"] = "true"
        target["h5_probe_controlled_region"].update(
            visible=not conflict,
            height=1 if conflict else 100,
        )
    subject_key = probe_subject_key(subject)
    trace = [
        {
            "action": "click",
            "selector": "#control",
            "result": "probe_effect_observed",
            "state_diff": {
                "h5_probe_effect": {
                    "protocol_version": "h5-probe-display-effect.v1",
                    "goal": goal,
                    "subject_key": subject_key,
                    "verified": True,
                    "before_selector": "#control",
                    "after_selector": "#control",
                    "before_rect": button["rect"],
                    "after_rect": after["elements"][0]["rect"],
                    "url": after["url"],
                    "document_time_origin_ms": 1234,
                    "readonly_denied_count": 0,
                }
            },
        }
    ]
    intent = fixture.actions[0]["intent_payload"]
    intent.update(
        session_id="h5:controlled",
        parameters={
            "target_mode": "h5_probe_display_target",
            "goal_capability": goal,
            "source_page_type": "video_list",
            "probe_subject_key": subject_key,
        },
    )
    captures = tuple(
        GreenCapture(
            fixture.ids["before" if index == 0 else "after"],
            "h5:controlled",
            "before_action" if index == 0 else "after_action",
            "action-1",
            2,
            fixture.ids["origin" if index == 0 else "before"],
            NOW + timedelta(milliseconds=index * 10),
            "manifest-" + str(index),
            1,
            tuple(
                original(fixture.ids["before" if index == 0 else "after"], kind, payload)
                for kind, payload in (
                    [("ui_snapshot", before)]
                    if index == 0
                    else [("ui_snapshot", after), ("action_trace", trace)]
                )
            ),
        )
        for index in range(2)
    )
    action = GreenAction(
        "action-1",
        "h5:controlled",
        fixture.ids["before"],
        fixture.ids["after"],
        2,
        NOW + timedelta(milliseconds=1),
        NOW + timedelta(milliseconds=2),
        json.dumps(intent).encode(),
    )
    evidence = _verified_evidence_set(
        task_id=fixture.ids["task"],
        run_id=fixture.ids["run"],
        epoch=0,
        input_set_id=fixture.input_set.input_set_id,
        input_hash=fixture.input_set.input_set_hash,
        scope_key=fixture.input_set.scope_key,
        fingerprint="controlled-originals",
        target_platform="h5",
        captures=captures,
        actions=(action,),
    )
    return fixture, evidence


def judge(fixture, evidence):
    return judge_green_process_subitems(
        contract=fixture.rule["judge_config"]["green_rule_contract"],
        evidence=evidence,
        required_facts=_MISSING_FACTS,
    )


@pytest.mark.parametrize("platform", [None, "app", "web"])
def test_h5_probe_labels_cannot_override_authoritative_task_platform(platform):
    fixture, evidence = process_case()
    item = judge(fixture, replace(evidence, target_platform=platform))[0]
    assert item.status == "evidence_insufficient"
    assert item.missing == ("trusted_h5_task_identity",)


def change_original(evidence, capture_index, artifact_type, mutate):
    capture = evidence.captures[capture_index]
    originals = list(capture.originals)
    for index, item in enumerate(originals):
        if item.artifact_type == artifact_type:
            payload = json.loads(item.payload)
            mutate(payload)
            originals[index] = original(capture.capture_id, artifact_type, payload)
    captures = list(evidence.captures)
    captures[capture_index] = replace(capture, originals=tuple(originals))
    return replace(evidence, captures=tuple(captures))


@pytest.mark.parametrize("goal", ["scroll_button_feedback", "scroll_back_to_top"])
def test_native_local_effect_is_limited_to_correct_response(goal):
    fixture, evidence = process_case(goal)
    items = judge(fixture, evidence)
    assert items[0].status == "pass" and not items[0].missing
    assert items[0].to_dict()["confidence"] == 1.0
    assert items[0].capture_ids and items[0].action_ids == ("action-1",)
    assert all(item.status == "evidence_insufficient" for item in items[1:])
    result = evaluate_green_aggregate_context(context(fixture, evidence))
    assert result.execution_status == "evidence_insufficient"
    assert result.review_status == "pending"


def test_opposite_visibility_without_settled_window_is_unknown_and_requires_review():
    fixture, evidence = process_case(conflict=True)
    result = evaluate_green_aggregate_context(context(fixture, evidence))
    assert result.execution_status == "evidence_insufficient"
    assert result.review_status == "pending" and result.review_reason
    assert result.error_code == "GREEN_NATIVE_PROCESS_FACTS_MISSING"
    assert result.wrapper_payload["green"]["subitems"][0]["evaluated"] is False
    assert result.wrapper_payload["green"]["missing_evidence"]
    assert "storage_path" not in json.dumps(result.wrapper_payload)


@pytest.mark.parametrize(
    "fault",
    [
        "native_effect_boolean_only",
        "no_response",
        "wrong_subject",
        "wrong_document",
        "late_snapshot",
        "truncated",
        "comparison_only",
        "duplicate_trace",
        "foreign_session",
        "broken_pair",
        "new_action",
    ],
)
def test_missing_native_causality_cannot_be_promoted_by_driver_success(fault):
    fixture, evidence = process_case()
    if fault in {
        "native_effect_boolean_only",
        "no_response",
        "wrong_subject",
        "comparison_only",
        "duplicate_trace",
    }:

        def mutate(trace):
            effect = trace[0]["state_diff"]["h5_probe_effect"]
            if fault == "native_effect_boolean_only":
                trace[0]["state_diff"]["h5_probe_effect"] = {"verified": True}
            elif fault == "no_response":
                trace[0]["result"] = "probe_effect_unverified"
            elif fault == "wrong_subject":
                effect["subject_key"] = "f" * 64
            elif fault == "comparison_only":
                trace[0]["comparison_only"] = True
            else:
                trace.append(copy.deepcopy(trace[0]))

        evidence = change_original(evidence, 1, "action_trace", mutate)
    elif fault in {"wrong_document", "late_snapshot", "truncated"}:

        def mutate(snapshot):
            if fault == "wrong_document":
                snapshot["document"]["time_origin_ms"] = 5678
            elif fault == "late_snapshot":
                snapshot["captured_at"] = (NOW - timedelta(seconds=1)).isoformat()
            else:
                snapshot["element_limit_reached"] = True

        evidence = change_original(evidence, 1, "ui_snapshot", mutate)
    elif fault == "foreign_session":
        evidence = replace(
            evidence, actions=(replace(evidence.actions[0], session_id="h5:foreign"),)
        )
    elif fault == "broken_pair":
        captures = list(evidence.captures)
        captures[1] = replace(captures[1], parent_capture_id="foreign-before")
        evidence = replace(evidence, captures=tuple(captures))
    else:
        evidence = replace(evidence, issues=("green_action_window_not_frozen:late-action",))
    assert judge(fixture, evidence)[0].status == "evidence_insufficient"


def test_noop_after_snapshot_is_unknown_even_when_effect_receipt_claims_verified():
    fixture, evidence = process_case()
    evidence = change_original(
        evidence,
        1,
        "ui_snapshot",
        lambda snapshot: snapshot["elements"][0].update(
            aria_expanded="false",
            h5_probe_controlled_region={
                "unique": True,
                "id": "region",
                "visible": False,
                "width": 200,
                "height": 0,
            },
        ),
    )
    assert judge(fixture, evidence)[0].status == "evidence_insufficient"


def test_unsettled_visibility_and_later_action_both_remain_unknown():
    fixture, evidence = process_case(conflict=True)
    evidence = replace(evidence, issues=("green_action_window_not_frozen:late-action",))
    item = judge(fixture, evidence)[0]
    assert item.status == "evidence_insufficient"
    assert "green_action_window_not_frozen:late-action" in item.missing


@pytest.mark.parametrize("rule_key", ["s72", "s49", "s73", "62"])
def test_local_display_success_does_not_prove_request_or_business_or_media_conditions(rule_key):
    _, evidence = process_case()
    fixture = FixtureConnection(rule_key)
    items = judge(fixture, evidence)
    assert all(item.status == "evidence_insufficient" and item.missing for item in items)
