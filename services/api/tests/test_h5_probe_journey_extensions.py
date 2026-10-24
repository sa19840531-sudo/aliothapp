"""原表100/105/58/123行的阶段合同及反例，不重跑历史任务。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_probe_journey import verified_display_subjects
from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.application.observation_packet import _compact_ui_snapshot
from services.api.app.application.rule_judge import RuleJudgeDefinition, _h5_gap_interaction
from services.api.tests.test_h5_pair_probe_targets import _snapshot


def scroll_snapshot(top=1200):
    return {
        "schema_version": "ui-snapshot.v2",
        "url": "https://fixture.invalid/list",
        "viewport": {"width": 390, "height": 600, "scroll_y": top},
        "document": {"scroll_height": 1800, "time_origin_ms": 123},
        "elements": [
            {
                "selector": "#top",
                "tag": "button",
                "text": "返回顶部",
                "aria_label": "返回顶部",
                "interactive": True,
                "class_tokens": ["back-to-top"],
                "ancestor_class_tokens": [],
                "h5_probe_local_display": True,
                "rect": {"x": 300, "y": 500, "width": 50, "height": 40},
                "hit_test": {"center_hits_target": True},
            }
        ],
    }


def packet(snapshot, *, actions=None):
    return SimpleNamespace(
        packet_payload={
            "identity": {"target_platform": "h5"},
            "page": {"surface_kind": "web", "page_type": "video_list", "page_capture_id": "after"},
            "facts": {"ui_snapshot": snapshot, "action_log": actions or []},
        }
    )


def definition(code, group):
    return RuleJudgeDefinition(
        rule_id="rule",
        rule_code=code,
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": group}},
    )


@pytest.mark.parametrize(
    "goal,page",
    [
        ("media_fullscreen_pair", "post_list"),
        ("payment_plan_selection", "payment"),
    ],
)
def test_judge_projection_does_not_lose_trusted_display_target(goal, page):
    original = _snapshot(goal)
    left = select_probe_target(original, goal=goal, page_type=page)
    right = select_probe_target(_compact_ui_snapshot(original), goal=goal, page_type=page)
    assert right is not None
    assert probe_subject_key(right) == probe_subject_key(left)


@pytest.mark.parametrize(
    "code,goal",
    [
        ("alioth_spec_new_4", "scroll_button_feedback"),
        ("alioth_spec_new_20", "scroll_back_to_top"),
    ],
)
@pytest.mark.parametrize("valid", [True, False])
def test_verified_scroll_sequence_advances_to_real_button_not_another_scroll(code, goal, valid):
    frames = [dict(scroll_snapshot(top), index=index) for index, top in enumerate([0, 600, 1200])]
    snapshot = {
        "schema_version": "ui-snapshot-sequence.v1",
        "sequence": True,
        "capture_role": "h5_vertical_scroll_collection",
        "frames": frames,
    }
    actions = [
        {
            "action": "scroll",
            "state_diff": {
                "scroll_effect": {
                    "protocol_version": "scroll-effect.v1",
                    "observed": valid,
                    "container_selector": "document",
                    "scroll_top_before": 0,
                    "scroll_top_after": 1200,
                }
            },
        }
    ]
    request = _h5_gap_interaction(
        definition(code, "list-scroll"), packet(snapshot, actions=actions)
    )
    assert request.parameters["goal_capability"] == (goal if valid else "vertical_scroll")
    if valid:
        assert request.parameters["probe_subject_key"]


def test_action_pair_uses_current_bound_capture_not_old_probe_before():
    before, after = _snapshot("payment_plan_selection"), _snapshot("payment_plan_selection")
    before["elements"][0]["text"] = "旧卡 ¥10"
    snapshot = {
        "sequence": True,
        "source": "h5_bound_action_capture_roles",
        "frames": [
            {**before, "index": 0, "capture_id": "before"},
            {**after, "index": 1, "capture_id": "after"},
        ],
    }
    source = packet(snapshot)
    source.packet_payload["page"]["page_type"] = "payment"
    requirement = _h5_gap_interaction(definition("alioth_ops_4", "payment-entry"), source)
    assert requirement.parameters["probe_subject_key"] == probe_subject_key(after["elements"][0])
    snapshot["frames"][-1]["capture_id"] = "foreign"
    requirement = _h5_gap_interaction(definition("alioth_ops_4", "payment-entry"), source)
    assert "probe_subject_key" not in requirement.parameters


def native_snapshot():
    value = _snapshot("media_fullscreen_pair")
    element = value["elements"][0]
    element.update(
        tag="video",
        src="blob:https://fixture.invalid/media",
        image=None,
        h5_media_instance_id="doc:video:1",
        media={
            "ready_state": 4,
            "controls": True,
            "current_time": 0.2,
            "paused": False,
            "video_width": 320,
            "video_height": 180,
            "fullscreen_enabled": True,
        },
    )
    return value


@pytest.mark.parametrize(
    "bad",
    [None, "loading", "no_controls", "foreign", "covered", "form", "duplicate", "no_instance"],
)
def test_native_fullscreen_requires_actual_ready_unique_media_instance(bad):
    snapshot = native_snapshot()
    video = snapshot["elements"][0]
    if bad == "loading":
        video["media"]["ready_state"] = 0
    elif bad == "no_controls":
        video["media"]["controls"] = False
    elif bad == "foreign":
        video["href"] = "https://foreign.invalid"
    elif bad == "covered":
        video["hit_test"]["center_hits_target"] = False
    elif bad == "form":
        video["h5_probe_local_display"] = False
    elif bad == "duplicate":
        snapshot["elements"].append(deepcopy(video))
    elif bad == "no_instance":
        video.pop("h5_media_instance_id")
    selected = select_probe_target(snapshot, goal="native_video_fullscreen", page_type="post_list")
    assert bool(selected) is (bad is None)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "submit",
        "carousel_only",
        "unknown_structure",
        "foreign",
        "form",
        "not_interactive",
        "duplicate",
    ],
)
def test_custom_membership_card_is_not_order_button_or_carousel_position(bad):
    snapshot = _snapshot("payment_plan_selection")
    card = snapshot["elements"][0]
    card.update(
        role=None,
        class_tokens=["swiper-slide"],
        interactive=True,
        ancestor_class_tokens=["swiper-wrapper", "card-box", "member"],
        h5_probe_plan_label="月卡 ¥30",
    )
    if bad == "submit":
        card["text"] = card["h5_probe_plan_label"] = "立即支付 ¥30"
    elif bad == "carousel_only":
        card["interactive"] = False
        card["class_tokens"].append("swiper-slide-active")
    elif bad == "unknown_structure":
        card["ancestor_class_tokens"] = ["unknown"]
    elif bad == "foreign":
        card["href"] = "https://foreign.invalid"
    elif bad == "form":
        card["h5_probe_local_display"] = False
    elif bad == "not_interactive":
        card["interactive"] = False
    elif bad == "duplicate":
        snapshot["elements"].append({**card, "selector": "#other"})
    selected = select_probe_target(snapshot, goal="payment_plan_selection", page_type="payment")
    assert bool(selected) is (bad is None)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "other_document",
        "other_url",
        "unverified",
        "bad_protocol",
        "not_executed",
        "missing_isolation",
        "denied_outbound",
        "missing_outbound_count",
        "false_count",
    ],
)
def test_selected_card_history_needs_real_same_document_action_not_class_name(bad):
    snapshot = _snapshot("payment_plan_selection")
    snapshot.update(url="https://fixture.invalid/member", document={"time_origin_ms": 123})
    key = probe_subject_key(snapshot["elements"][0])
    effect = {
        "protocol_version": "h5-probe-display-effect.v1",
        "goal": "payment_plan_selection",
        "subject_key": key,
        "verified": True,
        "url": snapshot["url"],
        "document_time_origin_ms": 123,
        "readonly_isolation": True,
        "readonly_denied_count": 0,
    }
    action = {"result": "probe_effect_observed", "state_diff": {"h5_probe_effect": effect}}
    if bad == "other_document":
        effect["document_time_origin_ms"] = 999
    elif bad == "other_url":
        effect["url"] = "https://other.invalid"
    elif bad == "unverified":
        effect["verified"] = False
    elif bad == "bad_protocol":
        effect["protocol_version"] = "model_summary"
    elif bad == "not_executed":
        action["result"] = "candidate"
    elif bad == "missing_isolation":
        effect.pop("readonly_isolation")
    elif bad == "denied_outbound":
        effect["readonly_denied_count"] = 1
    elif bad == "missing_outbound_count":
        effect.pop("readonly_denied_count")
    elif bad == "false_count":
        effect["readonly_denied_count"] = False
    payload = {"facts": {"action_log": [], "journey": [{"action_log": [action]}]}}
    assert verified_display_subjects(payload, goal="payment_plan_selection", snapshot=snapshot) == (
        {key} if bad is None else set()
    )


@pytest.mark.parametrize("offscreen", [True, False])
def test_payment_gap_reveals_horizontal_missing_card_but_not_covered_inside_card(offscreen):
    snapshot = _snapshot("payment_plan_selection")
    snapshot["elements"][0]["rect"]["x"] = 700 if offscreen else 20
    snapshot["elements"][0]["hit_test"]["center_hits_target"] = False
    source = packet(snapshot)
    source.packet_payload["page"]["page_type"] = "payment"
    requirement = _h5_gap_interaction(definition("alioth_ops_4", "payment-entry"), source)
    assert requirement.parameters["goal_capability"] == (
        "payment_plan_reveal" if offscreen else "payment_plan_selection"
    )


@pytest.mark.parametrize(
    "field,value",
    [("media", "invalid"), ("h5_scroll_regions", ["invalid"]), ("document", "invalid")],
)
def test_legacy_malformed_optional_proof_remains_unknown_instead_of_crashing(field, value):
    snapshot = native_snapshot()
    if field == "media":
        snapshot["elements"][0][field] = value
        assert (
            select_probe_target(snapshot, goal="native_video_fullscreen", page_type="post_list")
            is None
        )
    else:
        from services.api.app.application.h5_probe_journey import probe_scroll_context

        snapshot[field] = value
        assert probe_scroll_context(snapshot) is None


@pytest.mark.parametrize("bad", ["key", "goal", "page", "protocol", "operation"])
async def test_display_private_contract_fails_closed_before_browser_read(bad):
    from services.api.app.infrastructure.browser.h5_probe_display_execution import (
        execute_display_probe,
    )
    from services.api.app.infrastructure.browser.page_capture import (
        BlockedPageCaptureActionError,
        PageCaptureMode,
        PageCaptureRequest,
    )

    contract = {
        "protocol_version": "h5-probe-display.v1",
        "goal": "payment_plan_selection",
        "page_type": "payment",
        "subject_key": "a" * 64,
    }
    if bad == "key":
        contract["subject_key"] = None
    elif bad == "goal":
        contract["goal"] = []
    elif bad == "page":
        contract["page_type"] = {}
    elif bad == "protocol":
        contract["protocol_version"] = "model_summary"
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=AssertionError("不能读取或触碰浏览器")))
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        session_id="h5:fixture",
        selector="#slot",
        trigger_action="scroll" if bad == "operation" else "click",
        _h5_probe_display_contract=contract,
    )
    with pytest.raises(BlockedPageCaptureActionError):
        await execute_display_probe(page=page, request=request, action_trace=[{}], timeout_ms=1000)
    page.evaluate.assert_not_awaited()
