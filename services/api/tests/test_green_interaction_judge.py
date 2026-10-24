"""现有H5展示协议的只读判法；受控原件不代替真实三端产品验收。"""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_evidence_contracts import GreenEvidenceSet
from services.api.app.application.green_interaction_judge import judge_green_correct_response
from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.infrastructure.browser.h5_probe_display_execution import _effect
from services.api.tests.test_green_process_judge import (
    change_original,
    original,
    process_case,
)

GOALS = ("payment_plan_selection", "media_fullscreen_pair")


def extended_case(goal):
    """字段来自page_capture及h5_probe_display_execution已有生产器。"""
    fixture, evidence = process_case()
    before = evidence.captures[0].originals[0].object_payload()
    source = {
        "selector": "#source",
        "tag": "div",
        "role": "radio",
        "text": "月卡 ¥30",
        "class_tokens": ["plan-option"],
        "ancestor_class_tokens": ["vip-plans"],
        "h5_probe_local_display": True,
        "form_associated": False,
        "rect": {"x": 20, "y": 100, "width": 120, "height": 90},
        "hit_test": {"center_hits_target": True},
        "visible": True,
        "disabled": False,
        "aria_checked": "false",
    }
    page_type = "payment"
    if goal != "payment_plan_selection":
        page_type = "post_list"
        source.update(
            tag="video" if goal == "native_video_fullscreen" else "img",
            role=None,
            text="",
            src="https://controlled.invalid/content/"
            + ("video.mp4" if goal.startswith("native") else "image.png"),
            class_tokens=[],
            ancestor_class_tokens=["post-content"],
            image={"complete": True, "natural_width": 120, "natural_height": 90},
            h5_media_instance_id="native-instance-1",
            media={
                "controls": True,
                "fullscreen_enabled": True,
                "ready_state": 4,
                "video_width": 1280,
                "video_height": 720,
            },
        )
    before.update(
        elements=[source],
        element_count=1,
        document_state={"fullscreen_active": False, "fullscreen_tag": None},
        h5_probe_preview_roots=[],
        h5_probe_preview_root_states=[],
        h5_probe_preview_roots_truncated=False,
    )
    subject = select_probe_target(before, goal=goal, page_type=page_type)
    assert subject is not None
    after = copy.deepcopy(before)
    previous_after = evidence.captures[1].originals[0].object_payload()
    after.update(captured_at=previous_after["captured_at"], performance_now_ms=20)
    current = after["elements"][0]
    if goal == "native_video_fullscreen":
        after["document_state"].update(fullscreen_active=True, fullscreen_tag="video")
        current["rect"] = {"x": 0, "y": 0, "width": 390, "height": 600}
    elif goal == "payment_plan_selection":
        current["aria_checked"] = "true"
    else:
        current = copy.deepcopy(source)
        current.update(
            selector="#preview",
            h5_probe_preview_root="preview-root-1",
            rect={"x": 0, "y": 0, "width": 390, "height": 600},
        )
        after["elements"].append(current)
        after.update(
            element_count=2,
            h5_probe_preview_roots=["preview-root-1"],
            h5_probe_preview_root_states=[{"root": "preview-root-1", "visible": True}],
        )
    subject_key = probe_subject_key(subject)
    intent = json.loads(evidence.actions[0].intent_payload)
    intent["parameters"].update(
        goal_capability=goal, source_page_type=page_type, probe_subject_key=subject_key
    )
    effect = {
        "protocol_version": "h5-probe-display-effect.v1",
        "goal": goal,
        "subject_key": subject_key,
        "verified": True,
        "before_selector": source["selector"],
        "before_rect": source["rect"],
        "before_resource": source.get("src"),
        "after_selector": current["selector"],
        "after_rect": current["rect"],
        "after_resource": current.get("src"),
        "readonly_isolation": goal == "payment_plan_selection",
        "readonly_denied_count": 0,
        "url": after["url"],
        "document_time_origin_ms": after["document"]["time_origin_ms"],
        "preview_root_id": current.get("h5_probe_preview_root"),
    }
    trace = [
        {
            "action": "click",
            "selector": source["selector"],
            "result": "probe_effect_observed",
            "state_diff": {"h5_probe_effect": effect},
        }
    ]
    captures = (
        replace(
            evidence.captures[0],
            originals=(original(evidence.captures[0].capture_id, "ui_snapshot", before),),
        ),
        replace(
            evidence.captures[1],
            originals=(
                original(evidence.captures[1].capture_id, "ui_snapshot", after),
                original(evidence.captures[1].capture_id, "action_trace", trace),
            ),
        ),
    )
    return fixture, replace(
        evidence,
        captures=captures,
        actions=(replace(evidence.actions[0], intent_payload=json.dumps(intent).encode()),),
    )


@pytest.mark.parametrize("goal", GOALS)
async def test_existing_producer_effect_and_formal_originals_support_only_actual_response(goal):
    _, evidence = extended_case(goal)
    before, after = (capture.originals[0].object_payload() for capture in evidence.captures)
    intent = json.loads(evidence.actions[0].intent_payload)
    source = select_probe_target(
        before, goal=goal, page_type=intent["parameters"]["source_page_type"]
    )
    page = SimpleNamespace(
        locator=lambda _selector: SimpleNamespace(
            evaluate=AsyncMock(
                return_value=True,
            )
        )
    )
    # 与现有生产器使用相同原件形状交叉验证；不运行浏览器或产生新的业务操作。
    observed, _ = await _effect(page, goal, before, after, source, probe_subject_key(source))
    assert observed is True
    item = judge_green_correct_response(evidence)
    assert item.status == "pass" and not item.missing
    assert item.action_ids == ("action-1",)
    assert item.capture_ids == tuple(capture.capture_id for capture in evidence.captures)


@pytest.mark.parametrize("goal", GOALS)
@pytest.mark.parametrize(
    "fault",
    [
        "unverified",
        "missing_trace",
        "wrong_resource",
        "clock",
        "identity",
        "diagnostic",
        "truncated",
        "denied",
    ],
)
def test_effect_claim_never_replaces_originals_identity_clock_or_readonly_safety(goal, fault):
    _, evidence = extended_case(goal)
    if fault == "missing_trace":
        capture = evidence.captures[1]
        evidence = replace(
            evidence,
            captures=(
                evidence.captures[0],
                replace(
                    capture,
                    originals=tuple(
                        item for item in capture.originals if item.artifact_type != "action_trace"
                    ),
                ),
            ),
        )
    elif fault in {"unverified", "wrong_resource", "diagnostic", "denied"}:

        def mutate_trace(entries):
            effect = entries[0]["state_diff"]["h5_probe_effect"]
            if fault == "unverified":
                effect["verified"] = False
            elif fault == "wrong_resource":
                effect["after_resource"] = "https://controlled.invalid/another"
            elif fault == "diagnostic":
                entries[0]["diagnostic_capture"] = True
            else:
                effect["readonly_denied_count"] = 1

        evidence = change_original(evidence, 1, "action_trace", mutate_trace)
    elif fault == "clock":
        evidence = change_original(
            evidence,
            1,
            "ui_snapshot",
            lambda value: value["document"].update(
                time_origin_ms=5678,
            ),
        )
    elif fault == "truncated":
        evidence = change_original(
            evidence,
            1,
            "ui_snapshot",
            lambda value: value.update(
                element_limit_reached=True,
            ),
        )
    else:
        evidence = replace(
            evidence,
            captures=(
                evidence.captures[0],
                replace(
                    evidence.captures[1],
                    action_id="another-action",
                ),
            ),
        )
    assert judge_green_correct_response(evidence).status == "evidence_insufficient"


@pytest.mark.parametrize("goal", GOALS)
def test_actual_opposite_or_no_response_stays_unknown_without_complete_failure_window(goal):
    _, evidence = extended_case(goal)

    def mutate(value):
        if goal == "native_video_fullscreen":
            value["document_state"]["fullscreen_active"] = False
        elif goal == "payment_plan_selection":
            value["elements"][0]["aria_checked"] = "false"
        else:
            value["elements"][1]["image"]["complete"] = False

    evidence = change_original(evidence, 1, "ui_snapshot", mutate)
    item = judge_green_correct_response(evidence)
    assert item.status == "evidence_insufficient" and item.missing


@pytest.mark.parametrize("fault", ["isolation", "class_only", "same_semantics"])
def test_payment_selection_needs_real_semantic_change_and_server_readonly_isolation(fault):
    _, evidence = extended_case("payment_plan_selection")
    if fault == "isolation":
        evidence = change_original(
            evidence,
            1,
            "action_trace",
            lambda entries: entries[0]["state_diff"]["h5_probe_effect"].update(
                readonly_isolation=False
            ),
        )
    else:

        def mutate(value):
            source = value["elements"][0]
            source["aria_checked"] = "false"
            if fault == "class_only":
                source["class_tokens"].append("selected")

        evidence = change_original(evidence, 1, "ui_snapshot", mutate)
    assert judge_green_correct_response(evidence).status == "evidence_insufficient"


@pytest.mark.parametrize("fault", ["preexisting", "duplicate", "wrong_image", "incomplete_roots"])
def test_image_preview_requires_unique_new_root_and_exact_source_resource(fault):
    _, evidence = extended_case("media_fullscreen_pair")

    def mutate(value):
        if fault == "preexisting":
            value["h5_probe_preview_roots"] = []
            value["h5_probe_preview_root_states"] = []
        elif fault == "duplicate":
            value["elements"].append(copy.deepcopy(value["elements"][1]))
            value["element_count"] += 1
        elif fault == "wrong_image":
            value["elements"][1]["src"] = "https://controlled.invalid/other-image.png"
        else:
            value["h5_probe_preview_roots_truncated"] = True

    evidence = change_original(evidence, 1, "ui_snapshot", mutate)
    assert judge_green_correct_response(evidence).status == "evidence_insufficient"


def test_old_local_response_is_preserved_but_later_unknown_action_and_missing_facts_remain():
    _, evidence = process_case()
    assert judge_green_correct_response(evidence).status == "pass"
    evidence = replace(evidence, issues=("green_action_window_not_frozen:later",))
    item = judge_green_correct_response(evidence)
    assert item.status == "evidence_insufficient"
    assert "green_action_window_not_frozen:later" in item.missing
    assert item.action_ids == ("action-1",)


@pytest.mark.parametrize("platform", [None, "web", "app"])
def test_display_protocol_labels_do_not_override_real_task_platform(platform):
    _, evidence = extended_case("payment_plan_selection")
    assert judge_green_correct_response(replace(evidence, target_platform=platform)).status == (
        "evidence_insufficient"
    )


def test_arbitrary_evidence_or_reused_formal_action_cannot_pass():
    with pytest.raises(ValueError, match="权威只读"):
        judge_green_correct_response(
            GreenEvidenceSet(
                task_id="task",
                run_id="run",
                epoch=0,
                input_set_id="set",
                input_hash="hash",
                scope_key="green_interaction_stability.v1",
            )
        )
    _, evidence = extended_case("payment_plan_selection")
    item = judge_green_correct_response(replace(evidence, actions=evidence.actions * 2))
    assert item.status == "evidence_insufficient"
    assert "independent_formal_action_identity" in item.missing


def test_driver_native_fullscreen_success_is_not_a_product_click_response():
    _, evidence = extended_case("native_video_fullscreen")
    item = judge_green_correct_response(evidence)
    assert item.status == "evidence_insufficient" and item.missing
