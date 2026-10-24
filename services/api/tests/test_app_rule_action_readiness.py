"""真实广告关闭反例与业务动作正例；就绪不能替代业务结论。"""

from copy import deepcopy

import pytest

from services.api.app.application.app_rule_action_readiness import (
    app_rule_action_requirement,
    assess_app_rule_action_readiness,
)
from services.api.app.application.app_surface_evidence import project_ui_snapshot_from_hierarchy

SCOPE = {"task_id": "task", "agent_run_id": "run", "session_id": "session", "finalization_epoch": 2}


def rule(group, tools, **thresholds):
    return {
        "judge_config": {
            "thresholds": thresholds,
            "capability_config": {
                "batch_group": group,
                "requires_journey": True,
                "deterministic_tools": tools,
            },
        }
    }


BOTTOM = rule(
    "list-scroll", ["layout_geometry_assertion_handler"], max_bottom_blank_viewport_ratio=0.15
)
RETURN = rule(
    "list-scroll", ["control_behavior_assertion_tool"], back_to_top_min_scroll_viewports=1
)
RESPONSE = rule("list-scroll", ["interaction_state_diff_tool", "network_event_summary_tool"])
NAV = rule("visual-nav", ["state_snapshot_tool", "interaction_state_diff_tool"])


def capture(identifier, role, sequence, action_id):
    return {
        **SCOPE,
        "capture_id": identifier,
        "capture_role": role,
        "capture_status": "captured",
        "surface_kind": "android",
        "package_name": "com.hj.ttthree",
        "page_type": "home",
        "page_state": "stable",
        "session_command_sequence": sequence,
        "action_execution_id": action_id,
        "screenshot_artifact_id": f"screenshot:{identifier}",
        "manifest_quality_status": "pass",
        "manifest_screenshot_artifact_ids": [f"screenshot:{identifier}"],
        "ui_snapshot": {"elements": []},
    }


def pair(sequence=1, operation="scroll", **extra):
    identifier = f"action:{sequence}"
    before = capture(f"before:{sequence}", "before_action", sequence, identifier)
    after = capture(f"after:{sequence}", "after_action", sequence, identifier)
    after["parent_capture_id"] = before["capture_id"]
    action = {
        **SCOPE,
        "action_execution_id": identifier,
        "action_type": operation,
        "status": "completed",
        "command_sequence": sequence,
        "source_capture_id": before["capture_id"],
        "result_capture_id": after["capture_id"],
        "execution_outcome": "succeeded",
        "verification_outcome": "effect_confirmed",
        **extra,
    }
    return before, after, action


def assess(candidate, *pairs):
    return assess_app_rule_action_readiness(
        candidate,
        primary_capture_id=pairs[-1][1]["capture_id"],
        scope=SCOPE,
        captures=[capture for before, after, _ in pairs for capture in (before, after)],
        actions=[action for _, _, action in pairs],
    )


def geometry(capture, y, height=3000, viewport=1000):
    capture["ui_snapshot"].update(
        {"viewport": {"scroll_y": y, "height": viewport}, "document": {"scroll_height": height}}
    )


def native_snapshot(*, return_top=False):
    control = (
        '<node class="android.widget.Button" text="返回顶部" clickable="true" '
        'bounds="[800,800][950,950]"/>'
        if return_top
        else ""
    )
    return project_ui_snapshot_from_hierarchy(
        '<hierarchy><node class="android.widget.ScrollView" scrollable="true" '
        'bounds="[0,0][1000,1000]">'
        + control
        + "</node></hierarchy>",
        package_name="com.hj.ttthree",
    )


@pytest.mark.parametrize("candidate", [BOTTOM, RETURN])
def test_real_native_scroll_can_reach_judge_without_fabricating_bottom(candidate):
    before, after, action = pair(verification_outcome="no_effect")
    before["ui_snapshot"] = native_snapshot()
    after["ui_snapshot"] = native_snapshot()
    result = assess(candidate, (before, after, action))
    assert result.ready
    assert result.missing_evidence == ("native_scroll_position_unavailable",)
    assert "scroll_y" not in after["ui_snapshot"]["viewport"]
    assert "document" not in after["ui_snapshot"]


def test_native_visible_return_top_needs_attempt_but_no_effect_is_judgeable():
    scroll = pair()
    scroll[0]["ui_snapshot"] = native_snapshot()
    scroll[1]["ui_snapshot"] = native_snapshot(return_top=True)
    assert not assess(RETURN, scroll).ready
    click = pair(
        sequence=2, operation="click", semantic_target="返回顶部", verification_outcome="no_effect"
    )
    click[0]["ui_snapshot"] = native_snapshot(return_top=True)
    click[1]["ui_snapshot"] = native_snapshot(return_top=True)
    result = assess(RETURN, scroll, click)
    assert result.ready
    assert result.missing_evidence == ("native_scroll_position_unavailable",)


@pytest.mark.parametrize("candidate", [BOTTOM, RETURN, RESPONSE, NAV])
def test_real_close_ad_pair_cannot_supply_business_journey(candidate):
    before, after, action = pair(operation="click", semantic_target="关闭广告弹窗")
    before.update(page_state="popup", overlay_signature="大图推广弹窗")
    after.update(page_state="popup", overlay_signature="至尊终身卡促销弹层")
    after["surface_facts"] = {"popup_visible": True, "topmost_ad_popup": {"is_ad": True}}
    assert not assess(candidate, (before, after, action)).ready


@pytest.mark.parametrize("candidate", [BOTTOM, RETURN, RESPONSE, NAV])
def test_cleared_ad_stable_is_not_business_action(candidate):
    before, after, action = pair(operation="click", semantic_target="关闭广告弹窗")
    before.update(page_state="popup", overlay_signature="advertisement")
    assert not assess(candidate, (before, after, action)).ready


def test_bottom_requires_real_position_not_scrollable_or_no_effect():
    before, after, action = pair(verification_outcome="no_effect")
    after["ui_snapshot"]["elements"] = [{"scrollable": True}]
    result = assess(BOTTOM, (before, after, action))
    assert result.missing_evidence == ("trusted_bottom_position",)
    geometry(after, y=1000)
    assert not assess(BOTTOM, (before, after, action)).ready
    geometry(after, y=2000)
    # 即便动作无效果或底部存在大面积空白，只要材料齐全就可交 Judge 判缺陷。
    assert assess(BOTTOM, (before, after, action)).ready


def test_short_page_can_be_judged_without_fake_scroll():
    before, after, action = pair(operation="observe")
    geometry(after, y=0, height=800)
    assert assess(BOTTOM, (before, after, action)).ready


def test_temporal_snapshot_uses_latest_real_position():
    before, after, action = pair()
    geometry(after, y=2000)
    last = deepcopy(after["ui_snapshot"])
    after["ui_snapshot"] = {"sequence": True, "frames": [before["ui_snapshot"], last]}
    assert assess(BOTTOM, (before, after, action)).ready


def test_return_top_missing_control_is_judgeable_after_bottom_context():
    before, after, action = pair()
    geometry(after, y=2000)
    assert assess(RETURN, (before, after, action)).ready


def test_visible_return_top_requires_attempt_but_not_success():
    scroll = pair(sequence=1)
    geometry(scroll[1], y=2000)
    scroll[1]["ui_snapshot"]["elements"] = [{"clickable": True, "text": "返回顶部"}]
    assert assess(RETURN, scroll).missing_evidence == ("return_to_top_action_pair",)
    click = pair(
        sequence=2, operation="click", semantic_target="返回顶部", verification_outcome="no_effect"
    )
    geometry(click[0], y=2000)
    geometry(click[1], y=2000)
    assert assess(RETURN, scroll, click).ready


def test_scroll_then_business_control_no_effect_can_reach_judge():
    scroll = pair(sequence=1)
    assert not assess(RESPONSE, scroll).ready
    click = pair(
        sequence=2, operation="click", semantic_target="筛选", verification_outcome="no_effect",
        requested_capture_id=scroll[1]["capture_id"],
    )
    assert assess(RESPONSE, scroll, click).ready


def test_top_tab_attempt_can_reach_judge_even_when_selection_does_not_change():
    click = pair(
        operation="click",
        target_expected_state_hint="home_top_tab:最新",
        verification_outcome="no_effect",
    )
    assert assess(NAV, click).ready
    click[2]["target_expected_state_hint"] = "tab:我的"
    assert not assess(NAV, click).ready


@pytest.mark.parametrize(
    "key,value",
    [("agent_run_id", "old-run"), ("session_id", "other-session"), ("finalization_epoch", 1)],
)
def test_historical_scope_cannot_fill_business_action(key, value):
    click = pair(operation="click", target_expected_state_hint="home_top_tab:最新")
    for part in click:
        part[key] = value
    assert not assess(NAV, click).ready


def test_wrong_parent_or_screenshot_manifest_rejected():
    valid = pair(operation="click", target_expected_state_hint="home_top_tab:最新")
    broken_parent = deepcopy(valid)
    broken_parent[1]["parent_capture_id"] = "other-before"
    assert not assess(NAV, broken_parent).ready
    broken_manifest = deepcopy(valid)
    broken_manifest[0]["manifest_screenshot_artifact_ids"] = ["wrong-screenshot"]
    assert not assess(NAV, broken_manifest).ready


def test_prior_unrelated_list_scroll_cannot_fill_current_object():
    scroll = pair(sequence=1)
    click = pair(sequence=6, operation="click", semantic_target="筛选")
    assert not assess(RESPONSE, scroll, click).ready


@pytest.mark.parametrize(
    "group",
    [
        "player-controls",
        "content-lifecycle",
        "ad-fallback",
        "startup-ad-skip",
        "unknown-layout",
    ],
)
def test_undeclared_behavior_and_other_groups_keep_existing_behavior(group):
    candidate = rule(group, ["interaction_state_diff_tool"])
    assert app_rule_action_requirement(candidate) is None
    assert assess(candidate, pair(operation="observe")).ready


def test_requirement_uses_frozen_config_and_exposes_pending_demand():
    candidate = deepcopy(BOTTOM)
    candidate["rule_code"] = "renamed"
    result = assess(candidate, pair(operation="observe"))
    assert not result.ready
    assert result.to_exploration_requirement() == {
        "requirement": "list_bottom_observation",
        "missing_evidence": ["business_scroll_action_pair"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("attempted", [False, True])
async def test_publisher_and_facts_share_ready_without_marking_rule_complete(attempted):
    from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
    from services.api.app.application.app_rule_coverage_facts import project_app_rule_coverage_facts
    from services.api.tests.test_app_inspection_work_item_publish import (
        _FakeAnnotator,
        _FakeRule,
        _FakeSnapshotProvider,
        _FakeTaskQuery,
        _FakeWorkItemService,
    )

    before, after, action = pair(
        operation="click" if attempted else "observe",
        target_expected_state_hint="home_top_tab:最新" if attempted else "",
        verification_outcome="no_effect",
    )
    action_facts = {
        "primary_capture_id": after["capture_id"],
        "scope": SCOPE,
        "captures": [before, after],
        "actions": [action],
    }

    class Reader:
        calls = 0

        async def get_facts(self, **kwargs):
            self.calls += 1
            return action_facts

    reader = Reader()
    service, annotator = _FakeWorkItemService(), _FakeAnnotator()
    candidate_rules = [
        _FakeRule(rule_id=f"nav-{n}", rule_code=f"NAV-{n}", judge_config=NAV["judge_config"])
        for n in range(2)
    ]
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(candidate_rules),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,
        annotator=annotator,
        action_facts_reader=reader,
    )
    result = await publisher.publish_for_capture(
        task_id=SCOPE["task_id"],
        agent_run_id=SCOPE["agent_run_id"],
        capture_id=after["capture_id"],
        visual_page_type="home",
    )
    assert reader.calls == 1
    assert bool(service.commands) is attempted
    assert result.status == ("published" if attempted else "evidence_rules_deferred")
    frozen = [
        {
            **NAV,
            "rule_id": candidate.rule_id,
            "rule_code": candidate.rule_code,
            "applicability": "applicable",
            "applicable_page_types": ["home"],
        }
        for candidate in candidate_rules
    ]
    facts = project_app_rule_coverage_facts(
        {"rules": frozen, "inventory_hash": "locked"},
        scope=SCOPE,
        completed_rule_ids=set(),
        captures=[before, after],
        action_facts=action_facts,
    )
    assert facts["pending_rule_count"] == 2
    assert facts["deferred_rule_ids"] == []
    missing = facts["pending_rules"][0]["required_action_evidence"]["missing_evidence"]
    assert bool(missing) is (not attempted)


COVER = rule("cover-content", ["visual_semantic_compare_tool"])


def test_list_observation_is_not_a_cover_content_pair():
    before, after, action = pair(operation="observe")
    result = assess(COVER, (before, after, action))
    assert app_rule_action_requirement(COVER) == "cover_content_same_object"
    assert not result.ready
    assert result.missing_evidence == ("cover_content_list_observation_only",)


def test_same_cover_opened_to_playback_is_ready():
    before, after, action = pair(operation="click", semantic_target="同一部影片")
    before["page_type"] = "video_list"
    before["content_key"] = "同一部影片"
    after["page_type"] = "video_detail"
    after["content_key"] = "同一部影片"
    result = assess(COVER, (before, after, action))
    assert result.ready


def test_ad_image_is_not_accepted_as_cover_content():
    before, after, action = pair(operation="click", semantic_target="广告图")
    before["page_type"] = "content_list"
    after["page_type"] = "content_detail"
    after["is_ad"] = True
    result = assess(COVER, (before, after, action))
    assert not result.ready
    assert result.missing_evidence == ("cover_content_ad_material_mixed",)


def test_different_content_keys_stay_unpaired():
    before, after, action = pair(operation="click", semantic_target="封面甲")
    before["page_type"] = "content_list"
    before["content_key"] = "封面甲"
    after["page_type"] = "content_detail"
    after["content_key"] = "正文乙"
    result = assess(COVER, (before, after, action))
    assert not result.ready
    assert result.missing_evidence == ("cover_content_unpaired",)
