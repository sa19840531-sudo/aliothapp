"""测试工作记忆：调用计数、卡死检测判定、参数归一化。"""

import asyncio
from types import SimpleNamespace

import pytest
from agent_framework.exceptions import ChatClientException
from pydantic import BaseModel

from services.api.app.agents.runtime import agent_factory as agent_factory_module
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.agents.runtime.work_dispatcher import prepare_work_dispatch
from services.api.app.agents.runtime.working_memory import WorkingMemory, normalize_arguments
from services.api.app.agents.tools import page_detection_tools


def test_modal_surface_state_does_not_increment_visited_logical_pages() -> None:
    memory = WorkingMemory()
    identity = "sha256:" + "1" * 64

    assert memory.record_observation(
        SimpleNamespace(
            metadata={
                "logical_surface_identity_fingerprint": identity,
                "logical_surface_state_fingerprint": "sha256:" + "2" * 64,
            }
        )
    )
    assert memory.record_observation(
        SimpleNamespace(
            metadata={
                "logical_surface_identity_fingerprint": identity,
                "logical_surface_state_fingerprint": "sha256:" + "3" * 64,
            }
        )
    )

    assert memory.visited_logical_page_count == 1
    assert memory.observed_surface_state_count == 2


def test_reserve_call_consumes_budget_and_records_history() -> None:
    memory = WorkingMemory()

    first = memory.reserve_call("tool_a", '{"x": 1}', max_steps=3, stuck_window=3)
    second = memory.reserve_call("tool_b", '{"x": 2}', max_steps=3, stuck_window=3)

    assert first.accepted is True
    assert second.accepted is True
    assert memory.step_count == 2
    assert memory.recent_call_signatures == [("tool_a", '{"x": 1}'), ("tool_b", '{"x": 2}')]


def test_reserve_call_rejects_without_exceeding_max_steps() -> None:
    memory = WorkingMemory()

    assert memory.reserve_call("tool_a", "{}", max_steps=1, stuck_window=3).accepted
    rejected = memory.reserve_call("tool_b", "{}", max_steps=1, stuck_window=3)

    assert rejected.accepted is False
    assert rejected.termination_reason == "max_steps_reached"
    assert memory.step_count == 1
    assert memory.recent_call_signatures == [("tool_a", "{}")]


def test_reserve_call_rejects_fourth_matching_call_as_stuck() -> None:
    memory = WorkingMemory()
    for _ in range(3):
        assert memory.reserve_call("tool_a", "{}", max_steps=10, stuck_window=3).accepted

    rejected = memory.reserve_call("tool_a", "{}", max_steps=10, stuck_window=3)

    assert rejected.accepted is False
    assert rejected.termination_reason == "stuck_detected"
    assert memory.step_count == 3


def test_reserve_call_allows_different_arguments() -> None:
    memory = WorkingMemory()
    for arguments in ('{"x": 1}', '{"x": 2}', '{"x": 3}', '{"x": 3}'):
        assert memory.reserve_call("tool_a", arguments, max_steps=10, stuck_window=3).accepted

    assert memory.step_count == 4


def test_reserve_call_isolates_repeated_rule_write_without_latching_run() -> None:
    memory = WorkingMemory()
    arguments = '{"page_capture_id":"capture-1"}'
    for _ in range(3):
        assert memory.reserve_call(
            "record_rule_executions_batch_tool",
            arguments,
            max_steps=10,
            stuck_window=3,
        ).accepted

    rejected = memory.reserve_call(
        "record_rule_executions_batch_tool",
        arguments,
        max_steps=10,
        stuck_window=3,
    )

    assert rejected.accepted is False
    assert rejected.termination_reason == "page_rule_stuck_isolated"
    assert memory.termination_reason is None
    assert memory.recent_call_signatures == []


def test_reserve_call_isolates_repeated_page_flow_without_latching_run() -> None:
    memory = WorkingMemory()
    arguments = '{"mode":"single_trigger","trigger_action":"click"}'
    for _ in range(3):
        assert memory.reserve_call(
            "page_capture_tool",
            arguments,
            max_steps=10,
            stuck_window=3,
        ).accepted

    rejected = memory.reserve_call(
        "page_capture_tool",
        arguments,
        max_steps=10,
        stuck_window=3,
    )

    assert rejected.accepted is False
    assert rejected.termination_reason == "page_flow_stuck_isolated"
    assert memory.termination_reason is None
    assert memory.recent_call_signatures == []


def test_reserve_call_disables_stuck_detection_when_window_is_zero() -> None:
    memory = WorkingMemory()
    for _ in range(5):
        assert memory.reserve_call("tool_a", "{}", max_steps=10, stuck_window=0).accepted

    assert memory.step_count == 5


def test_reserve_call_latches_existing_termination_reason() -> None:
    memory = WorkingMemory(termination_reason="stuck_detected")

    rejected = memory.reserve_call("tool_a", "{}", max_steps=10, stuck_window=3)

    assert rejected.accepted is False
    assert rejected.termination_reason == "stuck_detected"
    assert memory.step_count == 0


def test_normalize_arguments_handles_mapping_regardless_of_key_order() -> None:
    assert normalize_arguments({"b": 2, "a": 1}) == normalize_arguments({"a": 1, "b": 2})


def test_normalize_arguments_handles_pydantic_model() -> None:
    class Args(BaseModel):
        x: int

    assert normalize_arguments(Args(x=1)) == normalize_arguments({"x": 1})


def test_normalize_arguments_falls_back_to_repr_for_other_types() -> None:
    assert normalize_arguments(("a", "b")) == repr(("a", "b"))


def test_page_capture_becomes_current_page_without_marking_it_inspected() -> None:
    memory = WorkingMemory()

    assert memory.has_incomplete_page_inspection() is False

    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/home",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )

    snapshot = memory.model_snapshot()
    assert snapshot["current_page"]["page_capture_id"] == "capture-1"
    assert snapshot["current_page"]["status"] == "captured"
    assert snapshot["inspected_pages"] == []
    assert memory.has_incomplete_page_inspection() is True


def test_page_detection_result_is_bound_to_page_capture() -> None:
    memory = _memory_with_capture("capture-1")

    remembered = memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )

    assert remembered is True
    assert memory.page_detection_result("capture-1") == {
        "detection_status": "recognized",
        "page_type": "home",
        "rule_matching_allowed": True,
    }
    assert memory.page_detection_result("capture-2") is None
    assert memory.model_snapshot()["current_page"]["page_detection"] == {
        "detection_status": "recognized",
        "page_type": "home",
        "rule_matching_allowed": True,
    }


def test_rule_matching_dispatch_preserves_detected_page_state() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        page_state="popup",
        rule_matching_allowed=True,
    )

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is not None
    assert dispatch.kind == "rule_matching"
    assert dispatch.arguments == {
        "page_capture_id": "capture-1",
        "page_type": "video_list",
        "page_state": "popup",
    }


def test_ambiguous_current_page_restores_source_before_frontier_dispatch() -> None:
    memory = _memory_with_capture("capture-list")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-list",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".masonry-grid .data-item:visible",
                "target_keywords": ["nested_content", "bottom_tab:1"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "video_detail",
                "navigation_depth": 2,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-list",
        page_type="video_list",
        candidate_rules=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-ambiguous",
        page_url="https://example.com/ai",
        page_title="歧义页面",
        page_seq=2,
        artifact_ids=["artifact-ambiguous"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-ambiguous",
        detection_status="ambiguous",
        page_type=None,
        page_state="loaded",
        rule_matching_allowed=False,
    )

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is not None
    assert dispatch.kind == "page_exploration"
    assert dispatch.exploration_action_id != action_id
    assert dispatch.arguments == {
        "exploration_action_id": dispatch.exploration_action_id,
        "navigation_depth": 2,
        "mode": "goto",
        "url": "https://example.com/capture-list",
    }
    original = next(
        action
        for action in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if action["exploration_action_id"] == action_id
    )
    assert original["source_page_capture_id"] == "capture-list"
    abandoned = memory.audit_snapshot()["inactive_pages"]
    assert abandoned[0]["page_capture_id"] == "capture-ambiguous"
    assert abandoned[0]["abandon_reason"] == "page_detection_ambiguous"


def test_page_detection_candidates_enter_exploration_frontier() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )

    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "detail_entry",
                "target_keywords": ["detail"],
                "risk_level": "low",
                "purpose": "cover detail page rules",
                "expected_next_page_type": "content_detail",
            }
        ],
        next_page_hypotheses=[],
    )

    frontier = memory.model_snapshot()["exploration_frontier"]
    action = frontier["pending_actions"][0]
    assert frontier["pending_action_count"] == 1
    assert action["exploration_action_id"] == "exploration-1"
    assert action["source_page_capture_id"] == "capture-1"
    assert action["source_page_type"] == "home"
    assert action["action"] == "click"
    assert action["target"] == "detail_entry"
    assert action["expected_next_page_type"] == "content_detail"
    assert memory.has_pending_exploration() is True


def test_entry_detail_page_does_not_schedule_browser_history_return() -> None:
    memory = _memory_with_capture("capture-1")

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "return to source list after detail inspection",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )

    assert action_ids == []
    assert memory.has_pending_exploration() is False


def test_entry_detail_page_rejects_model_return_synonym() -> None:
    memory = _memory_with_capture("capture-1")

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "return",
                "target": "浏览器后退",
                "target_keywords": ["返回", "后退"],
                "risk_level": "low",
                "purpose": "结束终态验收，回到上一层继续探索",
                "expected_next_page_type": "home",
            }
        ],
        next_page_hypotheses=[],
    )

    assert action_ids == []
    assert memory.has_pending_exploration() is False


def test_entry_detail_page_rejects_browser_back_action_name() -> None:
    memory = _memory_with_capture("capture-1")

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "browser_back",
                "target": "返回",
                "target_keywords": ["返回", "后退"],
                "risk_level": "low",
                "purpose": "从入口详情页返回上一级继续探索",
                "expected_next_page_type": "content_list",
            }
        ],
        next_page_hypotheses=[],
    )

    assert action_ids == []
    assert memory.has_pending_exploration() is False


def test_popup_recapture_does_not_create_browser_history_return() -> None:
    memory = _memory_with_capture("capture-popup")
    close_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-popup",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "#ad-close",
                "target_keywords": ["关闭", "×"],
                "risk_level": "low",
                "purpose": "关闭最上层广告弹窗",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.mark_exploration_action_selected(close_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-stable",
        page_url="https://example.com/detail",
        page_title="弹窗关闭后的详情页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=close_action_id,
        supersedes_page_capture_id="capture-popup",
    )

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-stable",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "browser_back",
                "target": "返回",
                "target_keywords": ["返回", "后退"],
                "risk_level": "low",
                "purpose": "从详情页返回上一级继续探索",
            }
        ],
        next_page_hypotheses=[],
    )

    assert action_ids == []
    assert memory.prepare_next_exploration_action() is None


def test_non_executable_none_action_is_not_added_to_frontier() -> None:
    memory = _memory_with_capture("capture-1")

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "none",
                "target": "页面已终态",
                "target_keywords": ["无需继续导航", "页面加载正常"],
                "risk_level": "low",
                "purpose": "当前页面无需执行进一步动作",
            }
        ],
        next_page_hypotheses=[],
    )

    assert action_ids == []
    assert memory.has_pending_exploration() is False


def test_rule_judge_dispatch_is_bounded_to_one_strategy_category_batch() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    candidate_rules = [
        {
            "rule_id": f"rule-{index}",
            "rule_code": f"alioth_{index}",
            "category": "text",
            "execution_route": {
                "strategy": "hybrid",
                "ready": True,
                "tool_name": "rule_judge_tool",
            },
        }
        for index in range(1, 23)
    ]
    candidate_rules.append(
        {
            "rule_id": "rule-visual",
            "rule_code": "alioth_visual",
            "category": "visual",
            "execution_route": {
                "strategy": "hybrid",
                "ready": True,
                "tool_name": "rule_judge_tool",
            },
        }
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=candidate_rules,
    )

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is not None
    assert dispatch.kind == "rule_judge"
    assert dispatch.tool_choice == {
        "mode": "required",
        "required_function_name": "rule_judge_batch_tool",
    }
    assert "alioth_1" in dispatch.message
    assert "alioth_20" in dispatch.message
    assert "alioth_21" not in dispatch.message
    assert "alioth_visual" not in dispatch.message
    assert "evidence_artifact_ids" not in dispatch.message
    assert dispatch.session_dependency == "snapshot"


def test_deterministic_rule_dispatch_batches_across_categories() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[
            {
                "rule_code": "alioth_text",
                "category": "text",
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                },
            },
            {
                "rule_code": "alioth_visual",
                "category": "visual",
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                },
            },
        ],
    )

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is not None
    assert dispatch.tool_name == "rule_judge_batch_tool"
    assert dispatch.arguments == {
        "page_capture_id": "capture-1",
        "rules": [
            {"rule_code": "alioth_text"},
            {"rule_code": "alioth_visual"},
        ],
    }


def test_interaction_evidence_does_not_promote_normal_exploration_action() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=广告详情",
                "target_keywords": ["广告", "进入"],
                "risk_level": "low",
                "purpose": "进入广告落地页核对链路",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {
                "rule_code": "alioth_ad_journey",
                "category": "ad",
                "input_contract": {
                    "required_capture_roles": ["source", "landing"],
                },
                "execution_route": {
                    "strategy": "hybrid",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            }
        ],
    )
    assert memory.ensure_interaction_evidence_action(
        page_capture_id="capture-1",
        match_terms=["广告", "进入"],
        required_capture_roles=["source", "landing"],
    ) is None

    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_ad_journey",
    )
    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"
    assert action["mandatory"] is False
    assert action["purpose"] == "进入广告落地页核对链路"
    assert action["evidence_source_roles"] == []
    assert action["evidence_result_roles"] == []
    assert memory.frontier_action_capture_defaults(action_id) == {
        "action": "click",
        "target": "text=广告详情",
    }


@pytest.mark.parametrize(
    ("action", "target", "purpose"),
    [
        ("click", "#cta", "rule_evidence:interaction"),
        ("scroll", "page_coverage", "page_coverage_continue"),
        ("observe", "same_session", "transient_page_recovery:loading:attempt-1"),
        ("observe", "same_session", "inspectable_page_transition:startup_ad"),
        ("observe", "same_session", "post_interaction_page_observation"),
    ],
)
def test_legacy_supplemental_evidence_action_is_blocked_before_dispatch(
    action: str,
    target: str,
    purpose: str,
) -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": action,
                "target": target,
                "risk_level": "low",
                "purpose": purpose,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[
            {
                "rule_code": "alioth_legacy_supplement",
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                },
            }
        ],
    )
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
    )

    assert memory.mark_exploration_action_selected(action_id) is False
    assert memory.prepare_next_exploration_action() is None
    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_legacy_supplement",
    )
    snapshot = memory.audit_snapshot()
    inactive_action = next(
        item
        for item in snapshot["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"
    assert snapshot["degraded_page_count"] == 1
    assert inactive_action["failure_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"


def test_legacy_timed_capture_cannot_bypass_disabled_gate_with_resume_flag() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "click",
                "target": "#legacy",
                "risk_level": "low",
                "purpose": "旧版本页面恢复动作",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[
            {
                "rule_code": "alioth_legacy_timed_capture",
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                },
            }
        ],
    )
    legacy_action = memory._page_inspection_state._exploration_actions[action_id]
    legacy_action.action = "timed_capture"
    legacy_action.target = "temporal_trace"
    legacy_action.resumes_page_exploration = True

    assert memory.mark_exploration_action_selected(action_id) is False
    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_legacy_timed_capture",
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"


def test_state_change_evidence_does_not_create_interaction_or_reload_actions() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=关注",
                "target_keywords": ["关注"],
                "risk_level": "low",
                "purpose": "核对关注后的即时状态",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[
            {
                "rule_code": "alioth_8",
                "input_contract": {
                    "required_capture_roles": ["before", "primary", "refreshed"],
                },
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            }
        ],
    )
    memory.ensure_interaction_evidence_action(
        page_capture_id="capture-1",
        match_terms=["关注"],
        required_capture_roles=["before", "primary", "refreshed"],
    )
    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_8",
    )
    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"
    assert action["mandatory"] is False
    assert action["supersedes_source_page"] is False
    assert action["purpose"] == "核对关注后的即时状态"
    assert memory.frontier_action_capture_defaults(action_id) == {
        "action": "click",
        "target": "text=关注",
    }


def test_interaction_evidence_does_not_take_over_mandatory_page_transition() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".startup-ad .skip",
                "target_keywords": ["skip", "close"],
                "risk_level": "low",
                "purpose": "关闭开屏广告并进入业务页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {
                "rule_code": "alioth_24",
                "input_contract": {
                    "required_capture_roles": ["before", "primary"],
                },
                "execution_route": {
                    "strategy": "hybrid",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            }
        ],
    )

    assert (
        memory.ensure_interaction_evidence_action(
            page_capture_id="capture-1",
            match_terms=["skip", "close"],
            required_capture_roles=["before", "primary"],
        )
        is None
    )
    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_24",
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"
    assert memory.mandatory_page_action_capture_defaults(action_id) == {
        "action": "click",
        "target": ".startup-ad .skip",
    }
    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None
    pending_action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert pending_action["purpose"] == "关闭开屏广告并进入业务页"
    assert pending_action["mandatory"] is True
    assert pending_action["blocks_page_inspection"] is False
    assert pending_action["requires_source_inspection_complete"] is True
    assert pending_action["supersedes_source_page"] is False
    assert pending_action["evidence_source_roles"] == []
    assert pending_action["evidence_result_roles"] == []
    assert pending_action["resumes_page_exploration"] is False


def test_missing_interaction_candidate_dispatches_judge_instead_of_redetection() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[
            {
                "rule_code": "alioth_interaction",
                "input_contract": {
                    "required_capture_roles": ["before", "primary"],
                },
                "execution_route": {
                    "strategy": "hybrid",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            }
        ],
    )

    assert (
        memory.ensure_interaction_evidence_action(
            page_capture_id="capture-1",
            match_terms=[],
            required_capture_roles=["before", "primary"],
        )
        is None
    )
    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_interaction",
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is None


def test_startup_transition_reidentifies_landing_page_before_optimized_completion() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".loading-page .down-time:visible",
                "target_keywords": ["关闭", "倒计时"],
                "risk_level": "low",
                "purpose": "关闭开屏广告并进入业务页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {
                "rule_code": "alioth_18",
                "category": "ad",
                "input_contract": {
                    "required_artifacts": ["temporal_trace"],
                    "required_capture_roles": ["primary"],
                },
                "judge_config": {"engine_config": {"handler": "temporal_sequence_assertion"}},
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            },
            {
                "rule_code": "alioth_21",
                "category": "ad",
                "input_contract": {
                    "required_artifacts": ["action_trace", "ui_snapshot"],
                    "required_capture_roles": ["before", "primary"],
                },
                "judge_config": {"engine_config": {"handler": "overlay_dismissal_handler"}},
                "execution_route": {
                    "strategy": "rule_engine",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            },
        ],
    )
    assert (
        memory.ensure_interaction_evidence_action(
            page_capture_id="capture-1",
            match_terms=["关闭"],
            required_capture_roles=["before", "primary"],
        )
        is None
    )

    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None
    assert memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_18",
    )["execution_status"] == "evidence_insufficient"
    assert memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_21",
    )["execution_status"] == "evidence_insufficient"
    lifecycle_action = prepare_work_dispatch(memory)
    assert lifecycle_action is not None
    assert lifecycle_action.tool_name == "page_capture_tool"
    assert lifecycle_action.exploration_action_id == action_id
    assert lifecycle_action.arguments == {
        "exploration_action_id": action_id,
        "mode": "single_trigger",
        "trigger_action": "click",
        "selector": ".loading-page .down-time:visible",
    }

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/home",
        page_title="首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_id,
    )
    current_page = memory.model_snapshot()["current_page"]
    assert current_page["page_capture_id"] == "capture-2"
    assert current_page["page_detection"] is None
    assert current_page["page_type"] is None
    assert memory.has_runnable_work() is True

    detection = prepare_work_dispatch(memory)
    assert detection is not None
    assert detection.tool_name == "page_detection_tool"
    assert detection.arguments == {"page_capture_id": "capture-2"}


def test_legacy_promoted_lifecycle_action_does_not_inherit_source_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".startup-ad .skip",
                "risk_level": "low",
                "purpose": "关闭开屏广告并进入业务页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
        requires_source_inspection_complete=False,
    )

    # 模拟旧版本已经持久化的错误状态，验证升级后仍能安全恢复。
    legacy_action = memory._page_inspection_state._exploration_actions[action_id]
    legacy_action.purpose = "rule_evidence:关闭开屏广告"
    legacy_action.resumes_page_exploration = True

    dispatch = prepare_work_dispatch(memory)
    assert dispatch is not None
    assert dispatch.exploration_action_id == action_id
    assert "inherit_parent_semantics" not in dispatch.arguments

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/home",
        page_title="首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_id,
        supersedes_page_capture_id="capture-1",
    )
    current_page = memory.model_snapshot()["current_page"]
    assert current_page["page_capture_id"] == "capture-2"
    assert current_page["page_detection"] is None
    assert current_page["page_type"] is None


def test_interaction_evidence_does_not_take_over_normal_navigation() -> None:
    memory = _memory_with_capture("capture-1")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "第一个视频",
                "target_keywords": ["video", "nested_content"],
                "risk_level": "low",
                "purpose": "进入首个非广告视频详情",
            }
        ],
        next_page_hypotheses=[],
    )[0]

    assert (
        memory.ensure_interaction_evidence_action(
            page_capture_id="capture-1",
            match_terms=["广告", "advert", "promo"],
            required_capture_roles=["source", "primary"],
        )
        is None
    )
    assert memory.mandatory_page_action_capture_defaults(action_id) is None
    assert memory.frontier_action_capture_defaults(action_id) == {
        "action": "click",
        "target": "第一个视频",
    }


def test_snapshot_rules_can_run_in_background_while_frontier_advances() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "#detail",
                "risk_level": "low",
                "purpose": "进入详情",
            }
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {
                "rule_code": "alioth_static",
                "execution_route": {
                    "strategy": "semantic",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "snapshot",
                },
            }
        ],
    )

    assert memory.prepare_next_exploration_action() is None
    assert memory.defer_snapshot_page_inspection(page_capture_id="capture-1") is True
    action = memory.prepare_next_exploration_action()
    assert action is not None
    assert action["target"] == "#detail"


def test_inspectable_transition_is_evidence_insufficient_without_observation() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {
                "rule_code": "alioth_static",
                "execution_route": {
                    "strategy": "semantic",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "snapshot",
                },
            }
        ],
    )
    assert (
        memory.ensure_inspectable_page_transition(
            page_capture_id="capture-1",
            page_type="startup_ad",
        )
        is None
    )

    assert memory.defer_snapshot_page_inspection(page_capture_id="capture-1") is False
    assert prepare_work_dispatch(memory) is None
    result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_static",
    )
    assert result is not None
    assert result["execution_status"] == "evidence_insufficient"
    assert memory.audit_snapshot()["degraded_page_count"] == 1


def test_active_session_rule_is_dispatched_before_snapshot_rule() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="player",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="player",
        candidate_rules=[
            {
                "rule_code": "alioth_static",
                "category": "visual",
                "execution_route": {
                    "strategy": "semantic",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "snapshot",
                },
            },
            {
                "rule_code": "alioth_player",
                "category": "player",
                "execution_route": {
                    "strategy": "semantic",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            },
        ],
    )

    dispatch = prepare_work_dispatch(memory)
    assert dispatch is not None
    assert dispatch.session_dependency == "active_session"
    assert dispatch.arguments["rule_code"] == "alioth_player"
    assert "evidence_artifact_ids" not in dispatch.arguments
    assert "judge_input" not in dispatch.arguments


def test_unrouted_rules_are_closed_by_direct_auditable_write() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {
                "rule_id": "rule-1",
                "rule_code": "alioth_unrouted",
                "execution_route": {
                    "strategy": "semantic",
                    "ready": False,
                    "blocked_reason": "语义模型配置缺失",
                },
            }
        ],
    )

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is not None
    assert dispatch.kind == "rule_write"
    assert dispatch.tool_name == "record_rule_executions_batch_tool"
    assert dispatch.arguments == {
        "page_capture_id": "capture-1",
        "executions": [
            {
                "rule_id": "rule-1",
                "rule_code": "alioth_unrouted",
                "execution_status": "error",
                "result_summary": "规则执行路由未就绪，已停止自动判定。",
                "problem_desc": "语义模型配置缺失",
                "error_code": "RULE_JUDGE_ROUTE_NOT_READY",
                "error_message": "语义模型配置缺失",
            }
        ],
    }


async def test_optimized_converged_run_does_not_call_main_agent(monkeypatch) -> None:
    class FailingAgent:
        async def run(self, *args, **kwargs):
            raise AssertionError("结构化流水线收敛后不应再调用 Main Agent")

    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = WorkingMemory()
    scope = agent_factory_module.AgentRunScope(
        session=SimpleNamespace(),
        working_memory=memory,
        trace_sink=SimpleNamespace(),
        middleware=[],
    )

    await agent_factory_module._run_supervised_agent_loop(
        agent=FailingAgent(),
        messages="已完成",
        scope=scope,
        runtime_settings=SimpleNamespace(),
        background_tasks={},
    )


async def test_optimized_loop_queues_third_snapshot_page_before_background_completion(
    monkeypatch,
) -> None:
    class FailingAgent:
        async def run(self, *args, **kwargs):
            raise AssertionError("结构化派发不应回退到 Main Agent")

    class QueueMemory:
        def __init__(self) -> None:
            self.termination_reason = None
            self.progress = 0

        def model_snapshot(self):
            return {
                "current_page": None,
                "pending_pages": [],
                "exploration_frontier": {
                    "pending_actions": [],
                    "pending_action_count": 0,
                },
                "terminal_page_count": 0,
            }

        def defer_snapshot_page_inspection(self, *, page_capture_id):
            return bool(page_capture_id)

        def page_progress_signature(self):
            return self.progress

        def reset_continuation_count(self):
            return None

        def has_runnable_work(self):
            return False

        def has_page_capture_history(self):
            return True

    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                pipeline_mode="on",
                semantic_concurrency=2,
            )
        ),
    )
    dispatches = [
        agent_factory_module.WorkDispatch(
            message=f"judge {index}",
            tool_choice={"mode": "required"},
            kind="rule_judge",
            tool_name="rule_judge_tool",
            arguments={
                "page_capture_id": f"capture-{index}",
                "rule_code": f"alioth_{index}",
            },
            session_dependency="snapshot",
        )
        for index in range(3)
    ]
    dispatches.append(
        agent_factory_module.WorkDispatch(
            message="explore next page",
            tool_choice={"mode": "required"},
            kind="page_exploration",
            tool_name="page_capture_tool",
            arguments={"exploration_action_id": "exploration-1"},
        )
    )

    def prepare_dispatch(_memory):
        return dispatches.pop(0) if dispatches else None

    release_background = asyncio.Event()
    started_pages: list[str] = []
    background_tasks: dict[str, asyncio.Task[None]] = {}

    async def run_background_page(
        *,
        page_capture_id,
        initial_dispatch,
        scope,
        rule_judge_gate,
    ):
        del initial_dispatch, scope, rule_judge_gate
        started_pages.append(page_capture_id)
        await release_background.wait()

    async def run_direct_dispatch(*, dispatch, scope):
        assert dispatch.kind == "page_exploration"
        assert len(background_tasks) == 3
        assert not any(task.done() for task in background_tasks.values())
        scope.working_memory.progress += 1
        release_background.set()

    monkeypatch.setattr(
        agent_factory_module,
        "prepare_work_dispatch",
        prepare_dispatch,
    )
    monkeypatch.setattr(
        agent_factory_module,
        "_run_background_snapshot_page",
        run_background_page,
    )
    monkeypatch.setattr(
        agent_factory_module,
        "_run_direct_dispatch",
        run_direct_dispatch,
    )
    scope = agent_factory_module.AgentRunScope(
        session=SimpleNamespace(),
        working_memory=QueueMemory(),
        trace_sink=SimpleNamespace(),
        middleware=[],
    )

    await asyncio.wait_for(
        agent_factory_module._run_supervised_agent_loop(
            agent=FailingAgent(),
            messages="inspect pages",
            scope=scope,
            runtime_settings=SimpleNamespace(),
            background_tasks=background_tasks,
        ),
        timeout=1,
    )

    assert started_pages == ["capture-0", "capture-1", "capture-2"]


async def test_optimized_main_agent_progress_resets_session(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    initial_session = SimpleNamespace(name="initial")
    replacement_session = SimpleNamespace(name="replacement")

    async def run_direct_dispatch(*, dispatch, scope) -> None:
        del dispatch, scope

    async def run_agent_with_progress(**kwargs):
        assert kwargs["session"] is initial_session
        memory.record_page_rule_result(
            page_capture_id="capture-1",
            result={
                "execution_id": "execution-1",
                "rule_id": "rule-1",
                "rule_code": "alioth_1",
                "execution_status": "pass",
                "page_capture_id": "capture-1",
            },
            arguments={
                "rule_id": "rule-1",
                "rule_code": "alioth_1",
                "execution_status": "pass",
            },
        )
        return SimpleNamespace(text="")

    monkeypatch.setattr(
        agent_factory_module,
        "_run_direct_dispatch",
        run_direct_dispatch,
    )
    monkeypatch.setattr(
        agent_factory_module,
        "_run_agent_with_provider_retry",
        run_agent_with_progress,
    )
    monkeypatch.setattr(agent_factory_module, "AgentSession", lambda: replacement_session)
    scope = agent_factory_module.AgentRunScope(
        session=initial_session,
        working_memory=memory,
        trace_sink=SimpleNamespace(),
        middleware=[],
    )

    await agent_factory_module._run_supervised_agent_loop(
        agent=SimpleNamespace(),
        messages="inspect page",
        scope=scope,
        runtime_settings=SimpleNamespace(
            max_consecutive_infrastructure_failures=3,
            max_continuation_attempts=3,
        ),
        background_tasks={},
    )

    assert scope.session is replacement_session


async def test_optimized_lifecycle_landing_is_detected_before_direct_completion(
    monkeypatch,
) -> None:
    """生命周期点击形成的新采集必须完成识别，不能被优化流水线直接收口。"""

    class FailingAgent:
        async def run(self, *args, **kwargs):
            raise AssertionError("结构化落地页识别不应回退到 Main Agent")

    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".loading-page .down-time:visible",
                "target_keywords": ["关闭", "倒计时"],
                "risk_level": "low",
                "purpose": "关闭开屏广告并进入业务页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[],
    )
    lifecycle_dispatch = prepare_work_dispatch(memory)
    assert lifecycle_dispatch is not None
    assert lifecycle_dispatch.exploration_action_id == action_id
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/home",
        page_title="首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_id,
    )

    direct_dispatches: list[str] = []

    async def run_direct_dispatch(*, dispatch, scope) -> None:
        direct_dispatches.append(dispatch.kind)
        if dispatch.kind == "page_detection":
            assert dispatch.arguments == {"page_capture_id": "capture-2"}
            scope.working_memory.remember_page_detection(
                page_capture_id="capture-2",
                detection_status="recognized",
                page_type="home",
                page_state="stable",
                rule_matching_allowed=True,
            )
            return
        if dispatch.kind == "rule_matching":
            assert dispatch.arguments == {
                "page_capture_id": "capture-2",
                "page_type": "home",
                "page_state": "stable",
            }
            scope.working_memory.start_page_inspection(
                page_capture_id="capture-2",
                page_type="home",
                candidate_rules=[],
            )
            return
        raise AssertionError(f"未预期的结构化派发：{dispatch.kind}")

    monkeypatch.setattr(
        agent_factory_module,
        "_run_direct_dispatch",
        run_direct_dispatch,
    )
    scope = agent_factory_module.AgentRunScope(
        session=SimpleNamespace(),
        working_memory=memory,
        trace_sink=SimpleNamespace(),
        middleware=[],
    )

    await agent_factory_module._run_supervised_agent_loop(
        agent=FailingAgent(),
        messages="开始",
        scope=scope,
        runtime_settings=SimpleNamespace(),
        background_tasks={},
    )

    assert direct_dispatches == ["page_detection", "rule_matching"]
    inspected = memory.model_snapshot()["inspected_pages"]
    assert any(item["page_type"] == "home" for item in inspected)


async def test_optimized_main_agent_http_500_isolates_action_and_continues(
    monkeypatch,
) -> None:
    class FailingAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise ChatClientException("Error code: 500 - provider overloaded")

    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    monkeypatch.setattr(
        agent_factory_module,
        "PROVIDER_OVERLOAD_RETRY_BACKOFF_SECONDS",
        0,
    )
    memory = _memory_with_capture("capture-1")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".loading-page .down-time:visible",
                "risk_level": "low",
                "purpose": "关闭开屏广告",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
    )
    executed_actions: list[str] = []

    async def run_direct_dispatch(*, dispatch, scope) -> None:
        if dispatch.kind == "page_exploration":
            executed_actions.append(dispatch.exploration_action_id)
            if dispatch.exploration_action_id == action_id:
                return
            scope.working_memory.remember_page_capture(
                page_capture_id="capture-2",
                page_url="https://example.com/movie",
                page_title="广告关闭后的页面",
                page_seq=2,
                artifact_ids=["artifact-2"],
                exploration_action_id=dispatch.exploration_action_id,
                supersedes_page_capture_id="capture-1",
            )
            return
        if dispatch.kind == "page_detection":
            assert scope.working_memory.fail_page_inspection(
                page_capture_id="capture-2",
                failure_stage="test_terminal",
                failure_code="TEST_TERMINAL",
                failure_message="测试完成",
                retryable=False,
                missing_rule_status="manual_review",
            )

    monkeypatch.setattr(
        agent_factory_module,
        "_run_direct_dispatch",
        run_direct_dispatch,
    )
    agent = FailingAgent()
    scope = agent_factory_module.AgentRunScope(
        session=SimpleNamespace(),
        working_memory=memory,
        trace_sink=SimpleNamespace(),
        middleware=[],
    )

    await agent_factory_module._run_supervised_agent_loop(
        agent=agent,
        messages="质检页面",
        scope=scope,
        runtime_settings=SimpleNamespace(),
        background_tasks={},
    )

    snapshot = memory.audit_snapshot()
    frontier = snapshot["exploration_frontier"]
    actions = {
        item["exploration_action_id"]: item
        for item in [*frontier["inactive_actions"], *frontier["explored_actions"]]
    }
    assert agent.calls == 3
    assert executed_actions == [action_id, "exploration-2"]
    assert actions[action_id]["status"] == "failed"
    assert actions[action_id]["failure_code"] == "LLM_PROVIDER_HTTP_500"
    assert actions["exploration-2"]["status"] == "explored"
    assert memory.termination_reason is None


def test_optimized_main_agent_failure_degrades_only_current_rule_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {
                "rule_id": "rule-1",
                "rule_code": "alioth_1",
                "execution_route": {
                    "strategy": "semantic",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "active_session",
                },
            }
        ],
    )
    dispatch = prepare_work_dispatch(memory)
    assert dispatch is not None
    assert dispatch.kind == "rule_judge"

    assert agent_factory_module._isolate_optimized_provider_failure(
        working_memory=memory,
        dispatch=dispatch,
        failure_code="LLM_PROVIDER_HTTP_500",
        failure_message="模型 API 返回 HTTP 500（服务内部错误）。",
    )

    page = memory.audit_snapshot()["terminal_pages"][0]
    assert page["inspection_status"] == "degraded"
    assert page["result"]["status_counts"] == {"manual_review": 1}
    assert page["failure"]["code"] == "LLM_PROVIDER_HTTP_500"
    assert memory.termination_reason is None


async def test_background_snapshot_runner_finishes_multiple_batches(
    monkeypatch,
) -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {
                "rule_code": f"alioth_{index}",
                "category": "visual",
                "execution_route": {
                    "strategy": "semantic",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "snapshot",
                },
            }
            for index in range(21)
        ],
    )
    initial_dispatch = prepare_work_dispatch(memory)
    assert initial_dispatch is not None
    assert memory.defer_snapshot_page_inspection(page_capture_id="capture-1")
    batch_sizes: list[int] = []

    async def run_direct_dispatch(*, dispatch, scope):
        raw_rules = dispatch.arguments.get("rules")
        rules = raw_rules if isinstance(raw_rules, list) else [dispatch.arguments]
        batch_sizes.append(len(rules))
        for rule in rules:
            rule_code = rule["rule_code"]
            assert scope.working_memory.record_page_rule_result(
                page_capture_id="capture-1",
                result={
                    "execution_id": f"execution-{rule_code}",
                    "rule_code": rule_code,
                    "execution_status": "pass",
                },
                arguments={"rule_code": rule_code, "execution_status": "pass"},
            )

    monkeypatch.setattr(
        agent_factory_module,
        "_run_direct_dispatch",
        run_direct_dispatch,
    )
    await agent_factory_module._run_background_snapshot_page(
        page_capture_id="capture-1",
        initial_dispatch=initial_dispatch,
        scope=SimpleNamespace(working_memory=memory),
    )

    assert batch_sizes == [20, 1]
    inspected = memory.audit_snapshot()["inspected_pages"][0]
    assert inspected["completed_rule_count"] == 21


async def test_background_snapshot_queue_limits_judges_without_limiting_queued_pages(
    monkeypatch,
) -> None:
    runners = []
    for index in range(3):
        capture_id = f"capture-{index}"
        memory = _memory_with_capture(capture_id)
        memory.remember_page_detection(
            page_capture_id=capture_id,
            detection_status="recognized",
            page_type="home",
            rule_matching_allowed=True,
        )
        memory.start_page_inspection(
            page_capture_id=capture_id,
            page_type="home",
            candidate_rules=[
                {
                    "rule_code": f"alioth_{index}",
                    "category": "visual",
                    "execution_route": {
                        "strategy": "semantic",
                        "tool_name": "rule_judge_tool",
                        "session_dependency": "snapshot",
                    },
                }
            ],
        )
        initial_dispatch = prepare_work_dispatch(memory)
        assert initial_dispatch is not None
        assert memory.defer_snapshot_page_inspection(page_capture_id=capture_id)
        runners.append(
            (
                capture_id,
                initial_dispatch,
                SimpleNamespace(working_memory=memory),
            )
        )

    active_count = 0
    max_active_count = 0

    async def run_direct_dispatch(*, dispatch, scope):
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        await asyncio.sleep(0.01)
        rule_code = dispatch.arguments["rule_code"]
        capture_id = dispatch.arguments["page_capture_id"]
        assert scope.working_memory.record_page_rule_result(
            page_capture_id=capture_id,
            result={
                "execution_id": f"execution-{rule_code}",
                "rule_code": rule_code,
                "execution_status": "pass",
            },
            arguments={
                "rule_code": rule_code,
                "execution_status": "pass",
            },
        )
        active_count -= 1

    monkeypatch.setattr(
        agent_factory_module,
        "_run_direct_dispatch",
        run_direct_dispatch,
    )
    gate = asyncio.Semaphore(2)

    await asyncio.gather(
        *(
            agent_factory_module._run_background_snapshot_page(
                page_capture_id=capture_id,
                initial_dispatch=initial_dispatch,
                scope=scope,
                rule_judge_gate=gate,
            )
            for capture_id, initial_dispatch, scope in runners
        )
    )

    assert max_active_count == 2
    assert all(
        scope.working_memory.audit_snapshot()["inspected_page_count"] == 1
        for _, _, scope in runners
    )


def test_page_capture_with_exploration_action_marks_frontier_explored() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "detail_entry",
                "risk_level": "low",
                "purpose": "cover detail page rules",
                "expected_next_page_type": "content_detail",
            }
        ],
        next_page_hypotheses=[],
    )

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/detail",
        page_title="detail",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id="exploration-1",
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["explored_action_count"] == 1
    assert frontier["explored_actions"][0]["result_page_capture_id"] == "capture-2"
    assert memory.has_pending_exploration() is False


def test_high_risk_exploration_action_is_blocked_automatically() -> None:
    memory = _memory_with_capture("capture-1")

    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="payment",
        candidate_actions=[
            {
                "action": "click",
                "target": "confirm_payment",
                "risk_level": "high",
                "expected_next_page_type": "payment",
            }
        ],
        next_page_hypotheses=[],
    )

    frontier = memory.model_snapshot()["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["inactive_action_count"] == 1
    assert frontier["inactive_actions"][0]["status"] == "blocked"
    assert frontier["inactive_actions"][0]["failure_reason"] == "high_risk_action"
    assert memory.has_pending_exploration() is False
    assert memory.has_blocked_obstruction() is False


def test_blocked_obstruction_is_review_gap_instead_of_normal_completion() -> None:
    """遮挡关闭被阻断后虽然没有可执行动作，但底层页面仍属于未覆盖。"""
    memory = _memory_with_capture("capture-1")

    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-dialog .btn",
                "target_keywords": ["关闭", "X"],
                "risk_level": "high",
                "purpose": "关闭官方推荐下载广告弹窗",
                "expected_next_page_type": "video_list",
            }
        ],
        next_page_hypotheses=[],
    )
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        page_state="popup",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )

    assert memory.has_pending_exploration() is False
    assert memory.has_blocked_obstruction() is True
    assert memory.has_runnable_work() is False
    assert memory.requires_review() is True
    assert agent_factory_module._classify_termination(memory) == TerminationReason.INCOMPLETE


def test_obstruction_dismissal_recaptures_do_not_consume_logical_page_budget() -> None:
    """同一业务页连续关闭多层遮挡，只替换快照，不应挤占业务页面预算。"""
    memory = _memory_with_capture("capture-1")
    current_capture_id = "capture-1"

    for index in range(2, 6):
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": f".van-dialog .close-{index}",
                    "target_keywords": ["close_control"],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=False,
            supersedes_source_page=True,
            requires_source_inspection_complete=True,
        )
        next_capture_id = f"capture-{index}"
        memory.remember_page_capture(
            page_capture_id=next_capture_id,
            page_url="https://example.com/capture-1",
            page_title="视频列表",
            page_seq=index,
            artifact_ids=[f"artifact-{index}"],
            exploration_action_id=action_id,
            supersedes_page_capture_id=current_capture_id,
        )
        current_capture_id = next_capture_id

    assert memory.discovered_page_work_count() == 1


def test_candidate_action_and_hypothesis_are_deduplicated_by_semantics() -> None:
    memory = _memory_with_capture("capture-1")

    added_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "detail_entry",
                "target_keywords": ["详情"],
                "risk_level": "low",
                "expected_next_page_type": "content_detail",
            }
        ],
        next_page_hypotheses=[
            {
                "page_type": "content_detail",
                "how_to_enter": "点击内容详情入口",
                "target_keywords": ["详情"],
            }
        ],
    )

    assert added_ids == ["exploration-1"]
    assert memory.model_snapshot()["exploration_frontier"]["pending_action_count"] == 1


def test_natural_language_next_page_hypothesis_is_not_executable_frontier() -> None:
    memory = _memory_with_capture("capture-1")

    added_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="post_list",
        candidate_actions=[],
        next_page_hypotheses=[
            {
                "page_type": "content_detail",
                "how_to_enter": "点击首个低风险帖子卡片",
                "target_keywords": ["帖子", "详情"],
            }
        ],
    )

    assert added_ids == []
    assert memory.has_pending_exploration() is False


def test_nested_detail_is_depth_first_and_deduped_after_return() -> None:
    memory = _memory_with_capture("capture-1")
    bottom_action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "div.van-tabbar > div:nth-of-type(2)",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "div.van-tabbar > div:nth-of-type(3)",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[],
    )
    first_bottom = memory.prepare_next_exploration_action()
    assert first_bottom is not None
    assert first_bottom["exploration_action_id"] == bottom_action_ids[0]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/list",
        page_title="列表",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=bottom_action_ids[0],
    )
    nested_payload = {
        "action": "click",
        "target": ".masonry-grid:visible .data-item:visible",
        "target_keywords": ["nested_content", "bottom_tab:2"],
        "risk_level": "low",
        "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
        "expected_next_page_type": "content_detail",
        "navigation_depth": 2,
    }
    nested_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_actions=[nested_payload],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_rules=[],
    )

    nested = memory.prepare_next_exploration_action()
    assert nested is not None
    assert nested["exploration_action_id"] == nested_action_id
    memory.remember_page_capture(
        page_capture_id="capture-3",
        page_url="https://example.com/detail",
        page_title="详情",
        page_seq=3,
        artifact_ids=["artifact-3"],
        exploration_action_id=nested_action_id,
    )
    back_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-3",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "详情或播放页质检完成后返回来源列表继续探索",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-3",
        page_type="content_detail",
        candidate_rules=[],
    )

    back = memory.prepare_next_exploration_action()
    assert back is not None
    assert back["exploration_action_id"] == back_action_id
    memory.remember_page_capture(
        page_capture_id="capture-4",
        page_url="https://example.com/list",
        page_title="列表",
        page_seq=4,
        artifact_ids=["artifact-4"],
        exploration_action_id=back_action_id,
    )
    duplicate_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-4",
        page_type="content_list",
        candidate_actions=[nested_payload],
        next_page_hypotheses=[],
    )

    assert duplicate_ids == []
    assert memory.is_nested_return_capture("capture-4") is True
    memory.remember_page_detection(
        page_capture_id="capture-4",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )
    assert memory.abandon_revisited_page(
        page_capture_id="capture-4",
        reason="nested_detail_return_revisit",
    )
    remaining = memory.prepare_next_exploration_action()
    assert remaining is not None
    assert remaining["exploration_action_id"] == bottom_action_ids[1]
    inactive_pages = memory.audit_snapshot()["inactive_pages"]
    assert inactive_pages[0]["page_capture_id"] == "capture-4"
    assert inactive_pages[0]["abandon_reason"] == "nested_detail_return_revisit"
    assert inactive_pages[0]["inspection_status"] == "completed"


def test_model_detail_action_is_deduplicated_across_revisited_captures() -> None:
    memory = _memory_with_capture("capture-1")
    model_action = {
        "action": "click",
        "target": "风骚御姐",
        "target_keywords": ["内容标题", "首个卡片"],
        "risk_level": "low",
        "purpose": "点击第一个低风险内容卡片进入详情",
        "expected_next_page_type": "content_detail",
    }

    assert memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="post_list",
        candidate_actions=[model_action],
        next_page_hypotheses=[],
    ) == ["exploration-1"]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/capture-1",
        page_title="帖子列表",
        page_seq=2,
        artifact_ids=["artifact-2"],
        supersedes_page_capture_id="capture-1",
    )

    duplicate_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-2",
        page_type="post_list",
        candidate_actions=[
            {
                **model_action,
                "target_keywords": ["low_risk", "post", "detail"],
                "purpose": "进入内容详情",
            }
        ],
        next_page_hypotheses=[],
    )

    assert duplicate_ids == []


def test_bottom_navigation_is_deduplicated_across_page_captures_by_tab_target() -> None:
    memory = _memory_with_capture("capture-1")
    navigation_action = {
        "action": "click",
        "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
        "target_keywords": ["bottom_navigation"],
        "risk_level": "low",
        "purpose": "探索稳定页面中可见的底部导航业务分支",
        "navigation_depth": 1,
    }

    assert memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[navigation_action],
        next_page_hypotheses=[],
    ) == ["exploration-1"]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/product",
        page_title="首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        supersedes_page_capture_id="capture-1",
    )

    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-2",
            page_type="content_list",
            candidate_actions=[navigation_action],
            next_page_hypotheses=[],
        )
        == []
    )
    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-2",
            page_type="content_list",
            candidate_actions=[
                {
                    **navigation_action,
                    "target": (
                        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(1):visible"
                    ),
                }
            ],
            next_page_hypotheses=[],
        )
        == []
    )
    assert memory.remember_exploration_candidates(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_actions=[
            {
                **navigation_action,
                "target": ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(3):visible"),
            }
        ],
        next_page_hypotheses=[],
    ) == ["exploration-2"]


def test_semantic_entry_navigation_is_deduplicated_across_page_captures() -> None:
    memory = _memory_with_capture("capture-1")
    settings_action = {
        "action": "click",
        "target": "i.van-icon-setting-o:visible",
        "target_keywords": [
            "settings_entry",
            "evidence:class",
            "evidence:attributes",
        ],
        "risk_level": "low",
        "purpose": "探索个人页设置入口",
        "expected_next_page_type": "settings",
        "navigation_depth": 1,
    }

    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_actions=[settings_action],
        next_page_hypotheses=[],
    )[0]
    assert memory.mark_exploration_action_selected(action_id)
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/product",
        page_title="设置入口结果页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_id,
        supersedes_page_capture_id="capture-1",
    )

    memory.remember_page_capture(
        page_capture_id="capture-3",
        page_url="https://example.com/product",
        page_title="首页复访",
        page_seq=3,
        artifact_ids=["artifact-3"],
        supersedes_page_capture_id="capture-2",
    )

    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-3",
            page_type="profile",
            candidate_actions=[settings_action],
            next_page_hypotheses=[],
        )
        == []
    )


def test_semantic_entry_risk_is_monotonic_and_blocks_existing_candidate() -> None:
    memory = _memory_with_capture("capture-profile-1")
    low_risk_entry = {
        "action": "click",
        "target": "i.van-icon-setting-o:visible",
        "target_keywords": ["settings_entry"],
        "risk_level": "low",
        "purpose": "进入设置辅助页",
        "expected_next_page_type": "settings",
        "navigation_depth": 1,
    }
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-profile-1",
        page_type="profile",
        candidate_actions=[low_risk_entry],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-profile-2",
        page_url="https://example.com/profile",
        page_title="个人中心补采",
        page_seq=2,
        artifact_ids=["artifact-profile-2"],
    )

    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-profile-2",
            page_type="profile",
            candidate_actions=[
                {
                    **low_risk_entry,
                    "target": "去绑定",
                    "target_keywords": ["settings_entry", "去绑定", "绑定手机号"],
                    "risk_level": "high",
                }
            ],
            next_page_hypotheses=[],
        )
        == []
    )
    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-profile-2",
            page_type="profile",
            candidate_actions=[low_risk_entry],
            next_page_hypotheses=[],
        )
        == []
    )

    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert action["risk_level"] == "high"
    assert action["status"] == "blocked"
    assert action["failure_reason"] == "high_risk_action"
    assert memory.prepare_next_exploration_action() is None


def test_superseded_semantic_entry_can_be_requeued_on_latest_source_capture() -> None:
    memory = _memory_with_capture("capture-profile-1")
    settings_action = {
        "action": "click",
        "target": "i.van-icon-setting-o:visible",
        "target_keywords": [
            "settings_entry",
            "profile_local_action",
            "page_type:settings",
            "evidence:class",
            "evidence:attributes",
        ],
        "risk_level": "low",
        "purpose": "open settings from profile attribute evidence",
        "expected_next_page_type": "settings",
        "navigation_depth": 1,
    }

    assert memory.remember_exploration_candidates(
        page_capture_id="capture-profile-1",
        page_type="profile",
        candidate_actions=[settings_action],
        next_page_hypotheses=[],
    ) == ["exploration-1"]
    memory.remember_page_capture(
        page_capture_id="capture-profile-2",
        page_url="https://example.com/profile?refresh=1",
        page_title="profile refresh",
        page_seq=2,
        artifact_ids=["artifact-2"],
        supersedes_page_capture_id="capture-profile-1",
    )

    old_action = next(
        action
        for action in memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
        if action["exploration_action_id"] == "exploration-1"
    )
    assert old_action["status"] == "skipped"
    assert old_action["failure_reason"] == "source_page_superseded"
    assert memory.remember_exploration_candidates(
        page_capture_id="capture-profile-2",
        page_type="profile",
        candidate_actions=[settings_action],
        next_page_hypotheses=[],
    ) == ["exploration-2"]

    frontier = memory.model_snapshot()["exploration_frontier"]
    assert frontier["pending_action_count"] == 1
    latest_action = frontier["pending_actions"][0]
    assert latest_action["source_page_capture_id"] == "capture-profile-2"
    assert latest_action["target"] == "i.van-icon-setting-o:visible"


@pytest.mark.parametrize(
    ("target", "entry_keyword", "expected_page_type"),
    [
        ("个人中心签到入口", "activity_entry", "activity"),
        ("i.van-icon-setting-o:visible", "settings_entry", "settings"),
        ("个人中心会员支付入口", "profile_vip_payment_entry", "payment"),
    ],
)
def test_profile_local_action_fails_when_result_page_type_not_reached(
    target: str,
    entry_keyword: str,
    expected_page_type: str,
) -> None:
    memory = _memory_with_capture("capture-profile-1")
    profile_local_action = {
        "action": "click",
        "target": target,
        "target_keywords": [
            entry_keyword,
            "profile_local_action",
            f"page_type:{expected_page_type}",
            "evidence:structure",
        ],
        "risk_level": "low",
        "purpose": "从个人中心打开专属页面",
        "expected_next_page_type": expected_page_type,
        "navigation_depth": 1,
    }

    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-profile-1",
        page_type="profile",
        candidate_actions=[profile_local_action],
        next_page_hypotheses=[],
    )[0]
    assert memory.mark_exploration_action_selected(action_id)
    memory.remember_page_capture(
        page_capture_id="capture-profile-2",
        page_url="https://example.com/profile",
        page_title="profile unchanged",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_id,
        supersedes_page_capture_id="capture-profile-1",
    )

    assert memory.remember_page_detection(
        page_capture_id="capture-profile-2",
        detection_status="recognized",
        page_type="profile",
        page_state="stable",
        rule_matching_allowed=True,
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    failed_action = next(
        item for item in frontier["inactive_actions"] if item["exploration_action_id"] == action_id
    )
    assert failed_action["status"] == "failed"
    assert failed_action["failure_code"] == "EXPECTED_PAGE_TYPE_NOT_REACHED"
    assert failed_action["failure_reason"] == "expected_page_type_not_reached"
    assert failed_action["result_page_capture_id"] == "capture-profile-2"
    assert all(item["exploration_action_id"] != action_id for item in frontier["explored_actions"])


def test_semantic_entry_action_stays_explored_when_result_page_type_reached() -> None:
    memory = _memory_with_capture("capture-profile-1")
    settings_action = {
        "action": "click",
        "target": "i.van-icon-setting-o:visible",
        "target_keywords": [
            "settings_entry",
            "profile_local_action",
            "page_type:settings",
            "evidence:class",
            "evidence:attributes",
        ],
        "risk_level": "low",
        "purpose": "open settings from profile attribute evidence",
        "expected_next_page_type": "settings",
        "navigation_depth": 1,
    }

    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-profile-1",
        page_type="profile",
        candidate_actions=[settings_action],
        next_page_hypotheses=[],
    )[0]
    assert memory.mark_exploration_action_selected(action_id)
    memory.remember_page_capture(
        page_capture_id="capture-settings",
        page_url="https://example.com/profile/edit",
        page_title="settings",
        page_seq=2,
        artifact_ids=["artifact-settings"],
        exploration_action_id=action_id,
        supersedes_page_capture_id="capture-profile-1",
    )

    assert memory.remember_page_detection(
        page_capture_id="capture-settings",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    explored_action = next(
        item for item in frontier["explored_actions"] if item["exploration_action_id"] == action_id
    )
    assert explored_action["status"] == "explored"
    assert explored_action["result_page_capture_id"] == "capture-settings"


def test_pending_bottom_navigation_rebinds_to_latest_same_tabbar_capture() -> None:
    memory = _memory_with_capture("capture-1")
    profile_action = {
        "action": "click",
        "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible",
        "target_keywords": ["bottom_navigation"],
        "risk_level": "low",
        "purpose": "探索稳定页面中可见的底部导航业务分支",
        "expected_next_page_type": "profile",
        "navigation_depth": 1,
    }
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[profile_action],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/movie",
        page_title="视频列表补采",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )

    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-2",
            page_type="video_list",
            candidate_actions=[profile_action],
            next_page_hypotheses=[],
        )
        == []
    )
    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert action["source_page_capture_id"] == "capture-2"


def test_business_navigation_survives_supplemental_capture_and_stays_global() -> None:
    memory = _memory_with_capture("capture-1")
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="post_list",
        candidate_actions=[
            {
                "action": "goto",
                "target": "https://example.com/product",
                "target_keywords": [
                    "business_navigation",
                    "internal_navigation",
                    "path:/product",
                ],
                "risk_level": "low",
                "purpose": "探索真实同源主导航中的未访问业务分支",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/",
        page_title="列表补采",
        page_seq=2,
        artifact_ids=["artifact-2"],
        supersedes_page_capture_id="capture-1",
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert [item["exploration_action_id"] for item in frontier["pending_actions"]] == action_ids
    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["action"] == "goto"


def test_nested_content_stays_ahead_of_global_business_navigation() -> None:
    memory = _memory_with_capture("capture-1")
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="post_list",
        candidate_actions=[
            {
                "action": "goto",
                "target": "https://example.com/movie",
                "target_keywords": [
                    "business_navigation",
                    "internal_navigation",
                    "path:/movie",
                ],
                "risk_level": "low",
                "purpose": "探索真实同源主导航中的未访问业务分支",
                "expected_next_page_type": "video_list",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": 'a[href="/archives/1"]:visible',
                "target_keywords": ["nested_content", "page_type:post_list"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "content_detail",
                "navigation_depth": 2,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="post_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]


def test_internal_navigation_path_is_deduplicated_after_return_capture() -> None:
    memory = _memory_with_capture("capture-1")
    navigation_action = {
        "action": "click",
        "target": 'a[href="./detail.html"]:visible',
        "target_keywords": [
            "内容详情",
            "internal_navigation",
            "path:/detail.html",
        ],
        "risk_level": "low",
        "purpose": "进入内容详情页测试",
        "expected_next_page_type": "content_detail",
        "navigation_depth": 1,
    }

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[navigation_action],
        next_page_hypotheses=[],
    )
    assert action_ids == ["exploration-1"]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/detail.html",
        page_title="内容详情",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_ids[0],
        supersedes_page_capture_id="capture-1",
    )
    memory.remember_page_capture(
        page_capture_id="capture-3",
        page_url="https://example.com/index.html",
        page_title="首页",
        page_seq=3,
        artifact_ids=["artifact-3"],
        supersedes_page_capture_id="capture-2",
    )

    duplicate_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-3",
        page_type="home",
        candidate_actions=[navigation_action],
        next_page_hypotheses=[],
    )

    assert duplicate_ids == []
    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert frontier["explored_action_count"] == 1


def test_internal_navigation_path_deduplicates_click_and_goto_forms() -> None:
    memory = _memory_with_capture("capture-1")
    goto_action = {
        "action": "goto",
        "target": "https://example.com/movie",
        "target_keywords": [
            "business_navigation",
            "internal_navigation",
            "path:/movie",
        ],
        "risk_level": "low",
        "purpose": "探索真实同源主导航中的未访问业务分支",
        "expected_next_page_type": "video_list",
        "navigation_depth": 1,
    }

    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[goto_action],
        next_page_hypotheses=[],
    )
    assert action_ids == ["exploration-1"]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/product",
        page_title="产品",
        page_seq=2,
        artifact_ids=["artifact-2"],
        supersedes_page_capture_id="capture-1",
    )

    duplicate_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": 'a[href="/movie"]:visible',
                "target_keywords": [
                    "business_navigation",
                    "internal_navigation",
                    "path:/movie",
                ],
                "risk_level": "low",
                "purpose": "探索真实同源主导航中的未访问业务分支",
                "expected_next_page_type": "video_list",
                "navigation_depth": 2,
            }
        ],
        next_page_hypotheses=[],
    )

    assert duplicate_ids == []
    assert memory.audit_snapshot()["exploration_frontier"]["pending_action_count"] == 1


def test_closure_skips_pending_actions_and_abandons_uninspected_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "detail_entry",
                "risk_level": "low",
                "expected_next_page_type": "content_detail",
            }
        ],
        next_page_hypotheses=[],
    )

    result = memory.close_exploration_items(
        action_status="skipped",
        reason="max_pages_reached",
        exploration_action_ids=[],
        close_all_pending_actions=True,
        page_capture_ids=[],
        abandon_all_pending_pages=True,
    )

    snapshot = memory.audit_snapshot()
    assert result["closed_action_ids"] == ["exploration-1"]
    assert result["abandoned_page_capture_ids"] == ["capture-1"]
    assert snapshot["pending_page_count"] == 0
    assert snapshot["inactive_pages"][0]["status"] == "abandoned"
    assert snapshot["inactive_pages"][0]["abandon_reason"] == "max_pages_reached"
    assert snapshot["exploration_frontier"]["inactive_actions"][0]["status"] == "skipped"
    assert memory.has_incomplete_page_inspection() is False
    assert memory.has_pending_exploration() is False


def test_closure_cannot_abandon_partially_inspected_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )

    result = memory.close_exploration_items(
        action_status="skipped",
        reason="max_pages_reached",
        exploration_action_ids=[],
        close_all_pending_actions=False,
        page_capture_ids=["capture-1"],
        abandon_all_pending_pages=False,
    )

    assert result["abandoned_page_capture_ids"] == []
    assert result["rejected_page_capture_ids"] == ["capture-1"]
    assert memory.has_incomplete_page_inspection() is True


def test_page_moves_to_inspected_pages_after_all_candidate_rules_have_results() -> None:
    memory = _memory_with_capture("capture-1")
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_1"},
            {"rule_id": "rule-2", "rule_code": "alioth_2"},
        ],
    )

    memory.record_page_rule_result(
        page_capture_id="capture-1",
        result={
            "execution_id": "execution-1",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "pass",
        },
        arguments={
            "result_summary": "通过",
            "confidence": 0.95,
            "evidence_artifact_ids": ["artifact-1"],
        },
    )

    in_progress = memory.model_snapshot()
    assert in_progress["current_page"]["status"] == "inspecting"
    assert in_progress["current_page"]["completed_rule_count"] == 1
    assert memory.has_incomplete_page_inspection() is True

    memory.record_page_rule_result(
        page_capture_id="capture-1",
        result={
            "execution_id": "execution-2",
            "rule_id": "rule-2",
            "rule_code": "alioth_2",
            "execution_status": "fail",
        },
        arguments={
            "problem_desc": "存在缺陷",
            "evidence_artifact_ids": ["artifact-2"],
        },
    )

    completed = memory.audit_snapshot()
    assert completed["current_page"] is None
    assert completed["inspected_page_count"] == 1
    page = completed["inspected_pages"][0]
    assert page["status"] == "completed"
    assert page["result"]["overall_status"] == "fail"
    assert page["result"]["status_counts"] == {"fail": 1, "pass": 1}
    assert page["result"]["rules"][1]["problem_desc"] == "存在缺陷"
    assert memory.has_incomplete_page_inspection() is False


def test_page_without_candidate_rules_is_completed_with_explicit_result() -> None:
    memory = _memory_with_capture("capture-empty")

    started = memory.start_page_inspection(
        page_capture_id="capture-empty",
        page_type="settings",
        candidate_rules=[],
    )

    snapshot = memory.model_snapshot()
    assert started is True
    assert snapshot["current_page"] is None
    assert snapshot["inspected_pages"][0]["result"]["overall_status"] == ("no_applicable_rules")
    assert memory.has_incomplete_page_inspection() is False


def test_switching_pages_keeps_unfinished_inspection_pending() -> None:
    memory = _memory_with_capture("capture-1")
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )

    snapshot = memory.model_snapshot()
    assert snapshot["current_page"]["page_capture_id"] == "capture-2"
    assert snapshot["pending_page_count"] == 1
    assert snapshot["pending_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["pending_pages"][0]["status"] == "pending"
    assert memory.has_incomplete_page_inspection() is True


def test_supplemental_capture_supersedes_unmatchable_capture_without_blocking_completion() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="insufficient",
        page_type=None,
        rule_matching_allowed=False,
    )

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/capture-2",
        page_title="补采页面",
        page_seq=2,
        artifact_ids=["artifact-2"],
        supersedes_page_capture_id="capture-1",
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    memory.record_page_rule_result(
        page_capture_id="capture-2",
        result={"rule_id": "rule-1", "execution_status": "pass"},
        arguments={"evidence_artifact_ids": ["artifact-2"]},
    )

    snapshot = memory.audit_snapshot()
    assert memory.has_incomplete_page_inspection() is False
    assert snapshot["pending_page_count"] == 0
    assert snapshot["inactive_page_count"] == 1
    assert snapshot["inactive_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["inactive_pages"][0]["status"] == "superseded"
    assert snapshot["inactive_pages"][0]["superseded_by_page_capture_id"] == "capture-2"


def test_new_page_capture_does_not_supersede_unmatchable_without_explicit_link() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="insufficient",
        page_type=None,
        rule_matching_allowed=False,
    )

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/detail",
        page_title="detail page",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )

    snapshot = memory.model_snapshot()
    assert snapshot["inactive_page_count"] == 0
    assert snapshot["pending_page_count"] == 1
    assert snapshot["pending_pages"][0]["page_capture_id"] == "capture-1"
    assert memory.has_incomplete_page_inspection() is True


def test_switching_pages_keeps_uninspected_capture_pending() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="settings",
        candidate_rules=[],
    )

    snapshot = memory.model_snapshot()

    assert snapshot["current_page"] is None
    assert snapshot["pending_page_count"] == 1
    assert snapshot["pending_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["pending_pages"][0]["status"] == "pending"
    assert snapshot["pending_pages"][0]["page_type"] is None
    assert memory.has_incomplete_page_inspection() is True


def test_needs_review_status_takes_precedence_over_fail() -> None:
    memory = _memory_with_capture("capture-1")
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_1"},
            {"rule_id": "rule-2", "rule_code": "alioth_2"},
        ],
    )
    for rule_id, status in (("rule-1", "fail"), ("rule-2", "manual_review")):
        memory.record_page_rule_result(
            page_capture_id="capture-1",
            result={"rule_id": rule_id, "execution_status": status},
            arguments={},
        )

    page = memory.model_snapshot()["inspected_pages"][0]
    assert page["result"]["overall_status"] == "needs_review"


def test_page_failure_terminalizes_missing_rules_without_closing_frontier() -> None:
    memory = _memory_with_capture("capture-1")
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_1"},
            {"rule_id": "rule-2", "rule_code": "alioth_2"},
        ],
    )
    memory.record_page_rule_result(
        page_capture_id="capture-1",
        result={"rule_id": "rule-1", "execution_status": "pass"},
        arguments={},
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[{"action": "click", "target": "next-page"}],
        next_page_hypotheses=[],
    )

    assert memory.degrade_page_inspection(
        page_capture_id="capture-1",
        failure_stage="rule_persistence",
        failure_code="DATABASE_WRITE_FAILED",
        failure_message="规则结果写入失败",
        retryable=True,
    )

    snapshot = memory.audit_snapshot()
    page = snapshot["terminal_pages"][0]
    assert memory.has_incomplete_page_inspection() is False
    assert memory.has_pending_exploration() is True
    assert memory.has_runnable_work() is True
    assert page["inspection_status"] == "degraded"
    assert page["quality_status"] == "needs_review"
    assert page["completed_rule_count"] == 2
    assert page["result"]["status_counts"] == {"error": 1, "pass": 1}


def test_stale_selector_restores_source_page_before_execution() -> None:
    memory = _memory_with_capture("capture-source")
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_rules=[],
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tab:nth-of-type(4)",
                "target_keywords": ["分类"],
                "risk_level": "low",
                "purpose": "探索页内 Tab",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-latest",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-latest"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-latest",
        page_type="settings",
        candidate_rules=[],
    )

    restore = memory.prepare_next_exploration_action()

    assert restore is not None
    assert restore["action"] == "goto"
    assert restore["target"] == "https://example.com/capture-source"
    assert restore["origin"] == "recovery"
    memory.remember_page_capture(
        page_capture_id="capture-restored",
        page_url="https://example.com/capture-source",
        page_title="视频",
        page_seq=3,
        artifact_ids=["artifact-restored"],
        exploration_action_id=restore["exploration_action_id"],
    )
    assert (
        memory.rebind_restored_source_action_if_ready(
            page_capture_id="capture-restored",
            detection_status="recognized",
            page_type="startup_ad",
            page_state="countdown",
        )
        is None
    )
    pending_before_ready = memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
    not_yet_rebound = next(
        item for item in pending_before_ready if item["exploration_action_id"] == action_id
    )
    assert not_yet_rebound["source_page_capture_id"] == "capture-source"

    memory.remember_page_detection(
        page_capture_id="capture-restored",
        detection_status="recognized",
        page_type="video_list",
        page_state="stable",
        rule_matching_allowed=True,
    )
    assert (
        memory.rebind_restored_source_action_if_ready(
            page_capture_id="capture-restored",
            detection_status="recognized",
            page_type="video_list",
            page_state="stable",
        )
        == "ready_reused"
    )
    pending = memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
    rebound = next(item for item in pending if item["exploration_action_id"] == action_id)
    assert rebound["source_page_capture_id"] == "capture-restored"


def test_revisited_matching_page_reuses_inspection_after_source_restore() -> None:
    memory = _memory_with_capture("capture-returned")
    memory.remember_page_detection(
        page_capture_id="capture-returned",
        detection_status="recognized",
        page_type="post_list",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-returned",
        page_type="post_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "筛选",
                "target_keywords": ["筛选"],
                "risk_level": "low",
                "purpose": "切换筛选条件",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.abandon_revisited_page(
        page_capture_id="capture-returned",
        reason="nested_detail_return_revisit",
    )
    memory.remember_page_capture(
        page_capture_id="capture-latest",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-latest"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-latest",
        page_type="settings",
        candidate_rules=[],
    )

    restore = memory.prepare_next_exploration_action()
    assert restore is not None
    assert restore["action"] == "goto"
    assert restore["target"] == "https://example.com/capture-returned"
    memory.remember_page_capture(
        page_capture_id="capture-restored",
        page_url="https://example.com/capture-returned",
        page_title="帖子列表",
        page_seq=3,
        artifact_ids=["artifact-restored"],
        exploration_action_id=restore["exploration_action_id"],
    )

    assert (
        memory.rebind_restored_source_action_if_ready(
            page_capture_id="capture-restored",
            detection_status="recognized",
            page_type="post_list",
            page_state="stable",
        )
        == "ready_reused"
    )
    pending = memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
    rebound = next(item for item in pending if item["exploration_action_id"] == action_id)
    assert rebound["source_page_capture_id"] == "capture-restored"
    assert memory.has_started_page_inspection("capture-restored") is False
    restored_page = next(
        page
        for page in memory.audit_snapshot()["inactive_pages"]
        if page["page_capture_id"] == "capture-restored"
    )
    assert restored_page["inspection_status"] == "completed"


def test_reached_page_type_expires_redundant_stale_page_tab() -> None:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs .van-tab:nth-of-type(7)",
                "target_keywords": ["page_tab_navigation", "activity"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "activity",
            },
            {
                "action": "click",
                "target": ".van-tabbar-item:nth-of-type(7)",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "profile",
            },
        ],
        next_page_hypotheses=[],
    )
    stale_tab_id, profile_action_id = action_ids
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_rules=[],
    )
    # 本地页签现在优先于离开当前页的全局导航；此用例直接模拟另一条导航
    # 已先被执行，验证目标页面被其他路径触达后遗留页签仍会正确收口。
    memory.remember_page_capture(
        page_capture_id="capture-profile",
        page_url="https://example.com/mine",
        page_title="我的",
        page_seq=2,
        artifact_ids=["artifact-profile"],
        exploration_action_id=profile_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-profile",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    activity_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "积分任务",
                "target_keywords": ["积分任务", "任务", "签到福利"],
                "risk_level": "low",
                "purpose": "从个人中心进入任务/活动类入口",
                "expected_next_page_type": "activity",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_rules=[],
    )
    assert memory.prepare_next_exploration_action()["exploration_action_id"] == activity_action_id
    memory.remember_page_capture(
        page_capture_id="capture-activity",
        page_url="https://example.com/task",
        page_title="积分任务",
        page_seq=3,
        artifact_ids=["artifact-activity"],
        exploration_action_id=activity_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-activity",
        detection_status="recognized",
        page_type="activity",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-activity",
        page_type="activity",
        candidate_rules=[],
    )

    assert memory.prepare_next_exploration_action() is None
    stale_tab = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == stale_tab_id
    )
    assert stale_tab["status"] == "skipped"
    assert stale_tab["result_page_capture_id"] == "capture-activity"
    assert stale_tab["failure_code"] == "EXPECTED_PAGE_TYPE_ALREADY_REACHED"


def test_stale_cross_page_type_tab_does_not_reload_source_page() -> None:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".activity-tab",
                "target_keywords": ["page_tab_navigation", "activity"],
                "risk_level": "low",
                "purpose": "探索活动页签",
                "expected_next_page_type": "activity",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_rules=[],
    )

    memory.remember_page_capture(
        page_capture_id="capture-profile",
        page_url="https://example.com/profile",
        page_title="个人中心",
        page_seq=2,
        artifact_ids=["artifact-profile"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-profile",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_rules=[],
    )

    assert memory.prepare_next_exploration_action() is None

    snapshot = memory.audit_snapshot()["exploration_frontier"]
    stale_tab = next(
        item for item in snapshot["inactive_actions"] if item["exploration_action_id"] == action_id
    )
    assert stale_tab["status"] == "failed"
    assert stale_tab["failure_code"] == "LOCAL_PAGE_ACTION_SOURCE_LEFT"
    assert all(item.get("origin") != "recovery" for item in snapshot["pending_actions"])


def test_stale_local_action_restores_source_with_browser_back_when_available() -> None:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    local_action_id, navigation_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "系统设置",
                "target_keywords": ["系统设置"],
                "risk_level": "low",
                "purpose": "从个人中心进入系统设置",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "积分任务",
                "target_keywords": ["积分任务"],
                "risk_level": "low",
                "purpose": "从个人中心进入任务页面",
                "expected_next_page_type": "activity",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(navigation_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-activity",
        page_url="https://example.com/task",
        page_title="积分任务",
        page_seq=2,
        artifact_ids=["artifact-activity"],
        exploration_action_id=navigation_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-activity",
        detection_status="recognized",
        page_type="activity",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-activity",
        page_type="activity",
        candidate_rules=[],
    )

    restore = memory.prepare_next_exploration_action()

    assert restore is not None
    assert restore["action"] == "back"
    assert restore["target"] == "browser_history"
    assert "source_restore_history" in restore["target_keywords"]
    original = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == local_action_id
    )
    assert original["source_page_capture_id"] == "capture-source"


def test_same_location_stale_action_rebinds_to_latest_stable_capture_without_restore() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-source",
        page_url="https://example.com/editInfo",
        page_title="个人资料",
        page_seq=1,
        artifact_ids=["artifact-source"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="settings",
        candidate_actions=[
            {
                "action": "click",
                "target": "查看头像",
                "risk_level": "low",
                "purpose": "查看头像说明",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="settings",
        candidate_rules=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-latest",
        page_url="https://example.com/editInfo",
        page_title="个人资料",
        page_seq=2,
        artifact_ids=["artifact-latest"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-latest",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-latest",
        page_type="settings",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_id
    assert selected["source_page_capture_id"] == "capture-latest"
    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert all(item["origin"] != "recovery" for item in frontier["pending_actions"])


@pytest.mark.parametrize(
    ("latest_url", "latest_page_state"),
    [
        ("https://example.com/editInfo?tab=security", "stable"),
        ("https://example.com/editInfo?tab=profile", "unknown"),
    ],
)
def test_same_location_stale_action_requires_same_query_and_explicit_stable_state(
    latest_url: str,
    latest_page_state: str,
) -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-source",
        page_url="https://example.com/editInfo?tab=profile",
        page_title="个人资料",
        page_seq=1,
        artifact_ids=["artifact-source"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="settings",
        candidate_actions=[
            {
                "action": "click",
                "target": "查看头像",
                "risk_level": "low",
                "purpose": "查看头像说明",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="settings",
        candidate_rules=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-latest",
        page_url=latest_url,
        page_title="设置子页",
        page_seq=2,
        artifact_ids=["artifact-latest"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-latest",
        detection_status="recognized",
        page_type="settings",
        page_state=latest_page_state,
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-latest",
        page_type="settings",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] != action_id
    assert selected["origin"] == "recovery"
    original = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert original["source_page_capture_id"] == "capture-source"


def test_source_restore_back_uses_current_navigation_edge_depth() -> None:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="profile",
        page_state="stable",
        rule_matching_allowed=True,
    )
    local_action_id, navigation_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "查看资料说明",
                "risk_level": "low",
                "purpose": "查看当前页面资料说明",
            },
            {
                "action": "click",
                "target": "积分任务",
                "target_keywords": ["积分任务"],
                "risk_level": "low",
                "purpose": "从个人中心进入任务页面",
                "expected_next_page_type": "activity",
                "navigation_depth": 2,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(navigation_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-activity",
        page_url="https://example.com/task",
        page_title="积分任务",
        page_seq=2,
        artifact_ids=["artifact-activity"],
        exploration_action_id=navigation_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-activity",
        detection_status="recognized",
        page_type="activity",
        page_state="stable",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-activity",
        page_type="activity",
        candidate_rules=[],
    )

    restore = memory.prepare_next_exploration_action()

    assert restore is not None
    assert restore["action"] == "back"
    assert restore["navigation_depth"] == 2
    original = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == local_action_id
    )
    assert original["navigation_depth"] is None


@pytest.mark.parametrize(
    ("source_page_type", "auxiliary_page_type", "entry_keyword"),
    [
        ("profile", "settings", "settings_entry"),
    ],
)
def test_auxiliary_page_without_bottom_navigation_returns_to_real_source(
    source_page_type: str,
    auxiliary_page_type: str,
    entry_keyword: str,
) -> None:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type=source_page_type,
        page_state="stable",
        rule_matching_allowed=True,
    )
    entry_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type=source_page_type,
        candidate_actions=[
            {
                "action": "click",
                "target": f"进入{auxiliary_page_type}",
                "target_keywords": [entry_keyword],
                "risk_level": "low",
                "purpose": f"进入{auxiliary_page_type}辅助页",
                "expected_next_page_type": auxiliary_page_type,
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type=source_page_type,
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(entry_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-auxiliary",
        page_url=f"https://example.com/{auxiliary_page_type}",
        page_title="辅助页面",
        page_seq=2,
        artifact_ids=["artifact-auxiliary"],
        exploration_action_id=entry_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-auxiliary",
        detection_status="recognized",
        page_type=auxiliary_page_type,
        page_state="stable",
        rule_matching_allowed=True,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-auxiliary",
        page_type=auxiliary_page_type,
        candidate_actions=[],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-auxiliary",
        page_type=auxiliary_page_type,
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["action"] == "back"
    assert selected["target"] == "browser_history"
    assert selected["source_page_capture_id"] == "capture-auxiliary"
    assert "auxiliary_page_return" in selected["target_keywords"]
    assert "nested_return" not in selected["target_keywords"]
    assert "source_restore_history" not in selected["target_keywords"]
    assert selected["expected_next_page_type"] == source_page_type
    assert selected["navigation_depth"] == 1
    assert selected["mandatory"] is True
    defaults = memory.frontier_action_capture_defaults(selected["exploration_action_id"])
    assert defaults is not None
    assert "supersedes_page_capture_id" not in defaults


@pytest.mark.parametrize(
    ("source_page_type", "auxiliary_page_type"),
    [("profile", "settings")],
)
def test_auxiliary_return_is_completed_only_after_stable_source_validation(
    source_page_type: str,
    auxiliary_page_type: str,
) -> None:
    memory, return_action_id = _memory_with_selected_auxiliary_return(
        source_page_type=source_page_type,
        auxiliary_page_type=auxiliary_page_type,
    )
    memory.remember_page_capture(
        page_capture_id="capture-returned",
        page_url="https://example.com/capture-source",
        page_title="来源页复访",
        page_seq=3,
        artifact_ids=["artifact-returned"],
        exploration_action_id=return_action_id,
        supersedes_page_capture_id="capture-auxiliary",
    )

    before_detection = memory.audit_snapshot()
    pending = next(
        item
        for item in before_detection["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == return_action_id
    )
    assert pending["status"] == "selected"
    auxiliary_before = next(
        page
        for page in [
            *before_detection["terminal_pages"],
            *before_detection["inactive_pages"],
            before_detection["current_page"],
        ]
        if page is not None and page["page_capture_id"] == "capture-auxiliary"
    )
    assert auxiliary_before["lifecycle_status"] == "active"

    memory.remember_page_detection(
        page_capture_id="capture-returned",
        detection_status="recognized",
        page_type=source_page_type,
        page_state="normal",
        rule_matching_allowed=True,
    )
    assert (
        memory.resolve_auxiliary_page_return_if_ready(
            page_capture_id="capture-returned",
            detection_status="recognized",
            page_type=source_page_type,
            page_state="normal",
        )
        == "ready_reused"
    )

    after_detection = memory.audit_snapshot()
    explored = next(
        item
        for item in after_detection["exploration_frontier"]["explored_actions"]
        if item["exploration_action_id"] == return_action_id
    )
    assert explored["result_page_capture_id"] == "capture-returned"
    auxiliary_after = next(
        page
        for page in after_detection["inactive_pages"]
        if page["page_capture_id"] == "capture-auxiliary"
    )
    assert auxiliary_after["lifecycle_status"] == "superseded"
    assert auxiliary_after["superseded_by_page_capture_id"] == "capture-returned"


@pytest.mark.parametrize(
    ("returned_url", "returned_page_type"),
    [
        ("https://example.com/wrong", "profile"),
        ("https://example.com/capture-source", "home"),
    ],
)
def test_auxiliary_return_wrong_stable_landing_does_not_supersede_auxiliary_page(
    returned_url: str,
    returned_page_type: str,
) -> None:
    memory, return_action_id = _memory_with_selected_auxiliary_return()
    memory.remember_page_capture(
        page_capture_id="capture-wrong",
        page_url=returned_url,
        page_title="错误落点",
        page_seq=3,
        artifact_ids=["artifact-wrong"],
        exploration_action_id=return_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-wrong",
        detection_status="recognized",
        page_type=returned_page_type,
        page_state="stable",
        rule_matching_allowed=True,
    )

    assert (
        memory.resolve_auxiliary_page_return_if_ready(
            page_capture_id="capture-wrong",
            detection_status="recognized",
            page_type=returned_page_type,
            page_state="stable",
        )
        == "target_not_reached"
    )

    snapshot = memory.audit_snapshot()
    failed = next(
        item
        for item in snapshot["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == return_action_id
    )
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "AUXILIARY_RETURN_TARGET_NOT_REACHED"
    auxiliary = next(
        page
        for page in [
            *snapshot["terminal_pages"],
            *snapshot["inactive_pages"],
            snapshot["current_page"],
        ]
        if page is not None and page["page_capture_id"] == "capture-auxiliary"
    )
    assert auxiliary["lifecycle_status"] == "active"


def test_auxiliary_return_popup_landing_waits_for_stable_source_validation() -> None:
    memory, return_action_id = _memory_with_selected_auxiliary_return()
    memory.remember_page_capture(
        page_capture_id="capture-popup",
        page_url="https://example.com/capture-source",
        page_title="来源页弹窗",
        page_seq=3,
        artifact_ids=["artifact-popup"],
        exploration_action_id=return_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-popup",
        detection_status="recognized",
        page_type="profile",
        page_state="popup",
        rule_matching_allowed=True,
    )

    assert (
        memory.resolve_auxiliary_page_return_if_ready(
            page_capture_id="capture-popup",
            detection_status="recognized",
            page_type="profile",
            page_state="popup",
        )
        is None
    )
    pending = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == return_action_id
    )
    assert pending["status"] == "selected"


def test_auxiliary_page_with_bottom_navigation_does_not_add_history_return() -> None:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="profile",
        page_state="stable",
        rule_matching_allowed=True,
    )
    entry_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "设置",
                "target_keywords": ["settings_entry"],
                "risk_level": "low",
                "purpose": "进入设置辅助页",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(entry_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-settings",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-settings"],
        exploration_action_id=entry_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-settings",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )
    bottom_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-settings",
        page_type="settings",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabbar-item:nth-of-type(1)",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "home",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-settings",
        page_type="settings",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == bottom_action_id
    frontier = memory.audit_snapshot()["exploration_frontier"]
    all_actions = [
        *frontier["pending_actions"],
        *frontier["explored_actions"],
        *frontier["inactive_actions"],
    ]
    assert all("auxiliary_page_return" not in item["target_keywords"] for item in all_actions)


def test_direct_entry_auxiliary_page_does_not_guess_browser_history() -> None:
    memory = _memory_with_capture("capture-settings")
    memory.remember_page_detection(
        page_capture_id="capture-settings",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-settings",
        page_type="settings",
        candidate_actions=[],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-settings",
        page_type="settings",
        candidate_rules=[],
    )

    assert memory.prepare_next_exploration_action() is None


def test_source_restore_is_bounded_and_does_not_restart_forever() -> None:
    memory = _memory_with_capture("capture-source")
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_rules=[],
    )
    original_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "更多",
                "target_keywords": ["更多", "van-icon-arrow"],
                "risk_level": "low",
                "purpose": "查看更多同分类视频",
                "expected_next_page_type": "video_list",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-latest",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-latest"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-latest",
        page_type="settings",
        candidate_rules=[],
    )

    for attempt in range(1, 3):
        restore = memory.prepare_next_exploration_action()
        assert restore is not None and restore["action"] == "goto"
        landing_id = f"capture-wrong-{attempt}"
        memory.remember_page_capture(
            page_capture_id=landing_id,
            page_url="https://example.com/unexpected",
            page_title="错误落地页",
            page_seq=2 + attempt,
            artifact_ids=[f"artifact-wrong-{attempt}"],
            exploration_action_id=restore["exploration_action_id"],
        )
        memory.remember_page_detection(
            page_capture_id=landing_id,
            detection_status="recognized",
            page_type="settings",
            page_state="stable",
            rule_matching_allowed=True,
        )
        assert (
            memory.rebind_restored_source_action_if_ready(
                page_capture_id=landing_id,
                detection_status="recognized",
                page_type="settings",
                page_state="stable",
            )
            is None
        )
        assert memory.abandon_unmatchable_page(
            page_capture_id=landing_id,
            reason="source_restore_unexpected_landing",
        )

    assert memory.prepare_next_exploration_action() is None
    failed_action = next(
        action
        for action in memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
        if action["exploration_action_id"] == original_action_id
    )
    assert failed_action["status"] == "failed"
    assert failed_action["failure_code"] == "SOURCE_ACTION_NOT_RESTORABLE"


def test_page_detection_error_is_terminal_instead_of_remaining_pending() -> None:
    memory = _memory_with_capture("capture-1")

    assert memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="error",
        page_type=None,
        rule_matching_allowed=False,
    )

    snapshot = memory.audit_snapshot()
    assert memory.has_incomplete_page_inspection() is False
    assert snapshot["pending_page_count"] == 0
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["stage"] == "page_detection"


def test_line_loading_marks_evidence_insufficient_without_supplemental_capture() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="line_loading",
        rule_matching_allowed=False,
    )

    pending_action_id = memory.ensure_transient_page_recovery(
        page_capture_id="capture-1",
        page_type="line_loading",
    )

    snapshot = memory.audit_snapshot()
    assert pending_action_id is None
    assert memory.has_runnable_work() is False
    assert memory.requires_review() is True
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


def test_loading_state_marks_evidence_insufficient_without_duplicate_observe() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="线路加载",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:same-content",
    )
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        page_state="loading",
        rule_matching_allowed=False,
    )
    action_id = memory.ensure_transient_page_recovery(
        page_capture_id="capture-1",
        page_type="loading",
    )

    snapshot = memory.audit_snapshot()
    assert action_id is None
    assert memory.requires_review() is True
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


def test_entry_exploration_action_can_fail_without_a_page_capture() -> None:
    memory = WorkingMemory()
    action_id = memory.ensure_entry_exploration_action(
        action="goto",
        target="https://example.com",
        navigation_depth=0,
    )

    assert memory.fail_exploration_action(
        exploration_action_id=action_id,
        failure_code="PAGE_CAPTURE_FAILED",
        failure_message="入口页面采集失败",
        retryable=False,
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert memory.is_page_exploration_converged() is True
    assert frontier["pending_action_count"] == 0
    assert frontier["inactive_actions"][0]["origin"] == "entry"
    assert frontier["inactive_actions"][0]["attempt_count"] == 1
    assert frontier["inactive_actions"][0]["failure_code"] == "PAGE_CAPTURE_FAILED"


def test_failed_obstruction_reobservation_is_bounded_across_superseded_pages() -> None:
    memory = _memory_with_capture("capture-1")
    current_capture_id = "capture-1"

    for attempt in range(1, 4):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="home",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="home",
            candidate_actions=[
                {
                    "action": "click",
                    "target": '[role="dialog"] .btn-close:visible',
                    "risk_level": "low",
                    "purpose": "关闭当前弹窗",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        assert memory.mark_exploration_action_selected(action_id)
        assert memory.fail_exploration_action(
            exploration_action_id=action_id,
            failure_code="PAGE_CAPTURE_FAILED",
            failure_message="关闭控件不可点击",
            retryable=False,
        )
        reobserve_action_id = memory.ensure_failed_obstruction_reobservation(
            exploration_action_id=action_id
        )

        if attempt < 3:
            assert reobserve_action_id is not None
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/product",
                page_title="首页弹窗",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                exploration_action_id=reobserve_action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert reobserve_action_id is None

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert memory.requires_review() is True
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


def test_failed_popup_obstruction_reobservation_uses_page_budget() -> None:
    memory = _memory_with_capture("capture-1")
    current_capture_id = "capture-1"

    for attempt in range(1, 21):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": f"popup-close-{attempt % 5}",
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        assert memory.mark_exploration_action_selected(action_id)
        assert memory.fail_exploration_action(
            exploration_action_id=action_id,
            failure_code="PAGE_CAPTURE_FAILED",
            failure_message="关闭控件不可点击",
            retryable=False,
        )
        reobserve_action_id = memory.ensure_failed_obstruction_reobservation(
            exploration_action_id=action_id
        )

        if attempt < 20:
            assert reobserve_action_id is not None
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/movie",
                page_title="视频列表弹窗",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                exploration_action_id=reobserve_action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert reobserve_action_id is None

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_page_obstruction_exhaustion_closes_same_url_active_popup_pages() -> None:
    """连关耗尽后，同一业务地址的活跃遮挡页必须一起收口，避免 pending 空转。"""
    memory = WorkingMemory()
    for page_capture_id in ("capture-1", "capture-2"):
        memory.remember_page_capture(
            page_capture_id=page_capture_id,
            page_url="https://example.com/movie",
            page_title="视频列表弹窗",
            page_seq=1 if page_capture_id == "capture-1" else 2,
            artifact_ids=[f"artifact-{page_capture_id}"],
        )
        memory.remember_page_detection(
            page_capture_id=page_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )

    for attempt in range(1, 22):
        action_id = memory.remember_exploration_candidates(
            page_capture_id="capture-2",
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": f".popup-close-{attempt}",
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        accepted = memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        if attempt <= 20:
            assert accepted is True
            assert memory.fail_exploration_action(
                exploration_action_id=action_id,
                failure_code="PAGE_CAPTURE_FAILED",
                failure_message="关闭控件不可点击",
                retryable=False,
            )
            continue
        assert accepted is False

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert memory.requires_review() is True
    assert snapshot["pending_page_count"] == 0
    assert snapshot["failed_page_count"] == 2
    assert {
        page["page_capture_id"]: page["failure"]["code"]
        for page in snapshot["terminal_pages"]
    } == {
        "capture-1": "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
        "capture-2": "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
    }


def test_popup_same_visible_text_caps_dynamic_dom_noise_at_short_budget() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/moviePlayer?id=1",
        page_title="Video detail popup",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:dynamic-1",
        visible_text_fingerprint="sha256:same-visible-popup",
    )
    current_capture_id = "capture-1"

    for attempt in range(1, 5):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="video_detail",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_detail",
            candidate_actions=[
                {
                    "action": "click",
                    "target": '[role="dialog"] .btn-box.end:visible',
                    "risk_level": "low",
                    "purpose": "close unchanged popup",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        required = memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )

        if attempt <= 3:
            assert required is True
            assert memory.mark_exploration_action_selected(action_id)
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/moviePlayer?id=1",
                page_title="Video detail popup",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                content_fingerprint=f"sha256:dynamic-{attempt + 1}",
                visible_text_fingerprint="sha256:same-visible-popup",
                exploration_action_id=action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert required is False

    snapshot = memory.audit_snapshot()
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_same_visible_text_productive_obstruction_dismissal_uses_page_budget() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/movie",
        page_title="Layered popup",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:layer-1",
        visible_text_fingerprint="sha256:same-visible-popup",
    )
    current_capture_id = "capture-1"

    for attempt in range(1, 13):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": '[role="dialog"] .btn-close:visible',
                    "target_keywords": ["close_control"],
                    "risk_level": "low",
                    "purpose": "close popup layer",
                }
            ],
            next_page_hypotheses=[],
        )[0]

        assert (
            memory.require_exploration_action(
                exploration_action_id=action_id,
                blocks_page_inspection=attempt > 1,
                supersedes_source_page=True,
                requires_source_inspection_complete=attempt == 1,
            )
            is True
        )
        assert memory.mark_exploration_action_selected(action_id)
        next_capture_id = f"capture-{attempt + 1}"
        memory.remember_page_capture(
            page_capture_id=next_capture_id,
            page_url="https://example.com/movie",
            page_title="Layered popup",
            page_seq=attempt + 1,
            artifact_ids=[f"artifact-{attempt + 1}"],
            content_fingerprint=f"sha256:layer-{attempt + 1}",
            visible_text_fingerprint="sha256:same-visible-popup",
            exploration_action_id=action_id,
            supersedes_page_capture_id=current_capture_id,
            obstruction_dismissal_count=1,
        )
        current_capture_id = next_capture_id

    snapshot = memory.audit_snapshot()
    assert snapshot["failed_page_count"] == 0
    assert snapshot["exploration_frontier"]["explored_action_count"] == 12

    memory.remember_page_detection(
        page_capture_id=current_capture_id,
        detection_status="recognized",
        page_type="video_list",
        page_state="popup",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id=current_capture_id,
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": '[role="dialog"] .btn-close:visible',
                "target_keywords": ["close_control"],
                "risk_level": "low",
                "purpose": "close popup layer",
            }
        ],
        next_page_hypotheses=[],
    )[0]

    assert (
        memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        is False
    )

    snapshot = memory.audit_snapshot()
    failed_action = next(
        action
        for action in snapshot["exploration_frontier"]["inactive_actions"]
        if action["exploration_action_id"] == action_id
    )
    assert failed_action["failure_code"] == "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"


def test_failed_popup_reobservation_caps_same_target_at_business_page_budget() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/movie",
        page_title="Video list popup",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:same-popup",
    )
    current_capture_id = "capture-1"

    for attempt in range(1, 21):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": '[role="dialog"] .btn-close:visible',
                    "risk_level": "low",
                    "purpose": "close popup",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=attempt > 1,
            supersedes_source_page=True,
            requires_source_inspection_complete=attempt == 1,
        )
        assert memory.mark_exploration_action_selected(action_id)
        assert memory.fail_exploration_action(
            exploration_action_id=action_id,
            failure_code="POPUP_DISMISSAL_NOT_OBSERVED",
            failure_message="Popup remained visible after click",
            retryable=False,
        )
        reobserve_action_id = memory.ensure_failed_obstruction_reobservation(
            exploration_action_id=action_id
        )

        if attempt < 20:
            assert reobserve_action_id is not None
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/movie",
                page_title="Video list popup",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                content_fingerprint="sha256:same-popup",
                exploration_action_id=reobserve_action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert reobserve_action_id is None

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_startup_ad_popup_reobservation_keeps_short_recovery_budget() -> None:
    memory = _memory_with_capture("capture-1")
    current_capture_id = "capture-1"

    for attempt in range(1, 4):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="startup_ad",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="startup_ad",
            candidate_actions=[
                {
                    "action": "click",
                    "target": f"startup-close-{attempt}",
                    "risk_level": "low",
                    "purpose": "close startup popup",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        assert memory.mark_exploration_action_selected(action_id)
        assert memory.fail_exploration_action(
            exploration_action_id=action_id,
            failure_code="POPUP_DISMISSAL_NOT_OBSERVED",
            failure_message="Startup popup remained visible after click",
            retryable=False,
        )
        reobserve_action_id = memory.ensure_failed_obstruction_reobservation(
            exploration_action_id=action_id
        )

        if attempt < 3:
            assert reobserve_action_id is not None
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/movie",
                page_title="Startup popup",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                exploration_action_id=reobserve_action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert reobserve_action_id is None

    snapshot = memory.audit_snapshot()
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_failed_inspectable_transition_reobserves_current_session() -> None:
    memory = _memory_with_capture("capture-1")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-overlay:visible",
                "risk_level": "low",
                "purpose": "关闭开屏广告",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=True,
    )
    assert memory.mark_exploration_action_selected(action_id)
    assert memory.fail_exploration_action(
        exploration_action_id=action_id,
        failure_code="PAGE_CAPTURE_FAILED",
        failure_message="开屏遮罩已消失",
        retryable=False,
    )

    reobserve_action_id = memory.ensure_failed_obstruction_reobservation(
        exploration_action_id=action_id
    )

    assert reobserve_action_id == "exploration-2"
    pending_action = memory.model_snapshot()["exploration_frontier"]["pending_actions"][0]
    assert pending_action["action"] == "observe"
    assert pending_action["target"] == "same_session"
    assert pending_action["mandatory"] is True


def test_unstarted_mandatory_transition_is_terminalized_for_continuation() -> None:
    memory = _memory_with_capture("capture-1")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-overlay:visible",
                "risk_level": "low",
                "purpose": "关闭开屏广告",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=True,
    )

    assert memory.blocked_mandatory_transition_action_ids() == [action_id]
    assert memory.degrade_mandatory_action_source_pages(
        exploration_action_ids=[action_id],
        failure_message="候选规则查询连续无进展",
    ) == ["capture-1"]

    snapshot = memory.audit_snapshot()
    assert memory.blocked_mandatory_transition_action_ids() == []
    assert snapshot["terminal_pages"][0]["inspection_status"] == "failed"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "RULE_MATCHING_ABORTED_FOR_PAGE_CONTINUATION"
    )
    assert (
        snapshot["exploration_frontier"]["pending_actions"][0]["exploration_action_id"] == action_id
    )


def test_unstarted_normal_page_is_terminalized_without_closing_dynamic_frontier() -> None:
    memory = _memory_with_capture("capture-1")
    assert memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "settings_entry",
                "risk_level": "low",
                "purpose": "探索设置页",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]

    assert memory.fail_unstarted_rule_matching_pages(
        failure_message="候选规则查询连续无进展",
    ) == ["capture-1"]

    snapshot = memory.audit_snapshot()
    assert snapshot["terminal_pages"][0]["inspection_status"] == "failed"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "RULE_MATCHING_ABORTED_FOR_PAGE_CONTINUATION"
    )
    assert (
        snapshot["exploration_frontier"]["pending_actions"][0]["exploration_action_id"] == action_id
    )


def test_successful_but_unchanged_obstruction_recovery_is_bounded_for_any_selector() -> None:
    memory = _memory_with_capture("capture-1")
    current_capture_id = "capture-1"

    for attempt in range(1, 5):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="home",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="home",
            candidate_actions=[
                {
                    "action": "click",
                    "target": '[role="dialog"] .product-close:visible',
                    "risk_level": "low",
                    "purpose": "关闭当前二维码提示层",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        required = memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=False,
            supersedes_source_page=True,
            requires_source_inspection_complete=True,
        )

        if attempt <= 3:
            assert required is True
            assert memory.mark_exploration_action_selected(action_id)
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/product",
                page_title="二维码提示",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                exploration_action_id=action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert required is False

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_popup_obstruction_recovery_is_bounded_when_targets_and_visible_text_change() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/movie",
        page_title="视频列表弹窗",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:dynamic-1",
        visible_text_fingerprint="sha256:visible-1",
    )
    current_capture_id = "capture-1"
    targets = [
        ".van-overlay:visible",
        '[role="dialog"] .btn-close:visible',
        ".van-popup .van-icon-close:visible",
        (
            ':is([role="dialog"], .van-dialog) .btn-close:visible, '
            ':is([role="dialog"], .van-dialog) .van-icon-close:visible'
        ),
    ]

    for attempt in range(1, 22):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": targets[(attempt - 1) % len(targets)],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        required = memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=False,
            supersedes_source_page=True,
            requires_source_inspection_complete=True,
        )

        if attempt <= 20:
            assert required is True
            assert memory.mark_exploration_action_selected(action_id)
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/movie",
                page_title="视频列表弹窗",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                content_fingerprint=f"sha256:dynamic-{attempt + 1}",
                visible_text_fingerprint=f"sha256:visible-{attempt + 1}",
                exploration_action_id=action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert required is False

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_obstruction_recovery_is_bounded_without_explicit_popup_state() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="首页广告",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:dynamic-1",
    )
    current_capture_id = "capture-1"
    targets = [
        ".van-overlay:visible",
        '[role="dialog"] .btn-close:visible',
        ':is([role="dialog"], .van-dialog):visible .app-btn:visible',
        ".home-suspend:visible .btn-close:visible",
    ]

    for attempt in range(1, 22):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="home",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="home",
            candidate_actions=[
                {
                    "action": "click",
                    "target": targets[(attempt - 1) % len(targets)],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡广告并在同一浏览器会话重新采集",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        required = memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )

        if attempt <= 20:
            assert required is True
            assert memory.mark_exploration_action_selected(action_id)
            next_capture_id = f"capture-{attempt + 1}"
            memory.remember_page_capture(
                page_capture_id=next_capture_id,
                page_url="https://example.com/product",
                page_title="首页广告",
                page_seq=attempt + 1,
                artifact_ids=[f"artifact-{attempt + 1}"],
                content_fingerprint=f"sha256:dynamic-{attempt + 1}",
                exploration_action_id=action_id,
                supersedes_page_capture_id=current_capture_id,
            )
            current_capture_id = next_capture_id
        else:
            assert required is False

    snapshot = memory.audit_snapshot()
    assert memory.has_runnable_work() is False
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
    )


def test_distinct_overlay_layers_with_same_selector_have_independent_budgets() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="多层弹窗",
        page_seq=1,
        artifact_ids=["artifact-1"],
        content_fingerprint="sha256:layer-1",
    )
    current_capture_id = "capture-1"

    for layer in range(1, 5):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="home",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="home",
            candidate_actions=[
                {
                    "action": "click",
                    "target": ".van-overlay:visible",
                    "risk_level": "low",
                    "purpose": "关闭当前最上层弹窗",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        assert memory.mark_exploration_action_selected(action_id)
        next_capture_id = f"capture-{layer + 1}"
        memory.remember_page_capture(
            page_capture_id=next_capture_id,
            page_url="https://example.com/product",
            page_title="多层弹窗",
            page_seq=layer + 1,
            artifact_ids=[f"artifact-{layer + 1}"],
            content_fingerprint=f"sha256:layer-{layer + 1}",
            exploration_action_id=action_id,
            supersedes_page_capture_id=current_capture_id,
        )
        current_capture_id = next_capture_id

    snapshot = memory.audit_snapshot()
    assert snapshot["failed_page_count"] == 0
    assert snapshot["exploration_frontier"]["explored_action_count"] == 4


def test_distinct_popup_layers_continue_until_page_state_is_stable() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="多层广告弹窗",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    current_capture_id = "capture-1"

    for layer in range(1, 13):
        memory.remember_page_detection(
            page_capture_id=current_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_id = memory.remember_exploration_candidates(
            page_capture_id=current_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": '[role="dialog"] .btn-close:visible',
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        required = memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=False,
            supersedes_source_page=True,
            requires_source_inspection_complete=True,
        )
        assert required is True
        assert memory.mark_exploration_action_selected(action_id)
        next_capture_id = f"capture-{layer + 1}"
        memory.remember_page_capture(
            page_capture_id=next_capture_id,
            page_url="https://example.com/product",
            page_title="多层广告弹窗",
            page_seq=layer + 1,
            artifact_ids=[f"artifact-{layer + 1}"],
            content_fingerprint=f"sha256:popup-layer-{layer + 1}",
            exploration_action_id=action_id,
            supersedes_page_capture_id=current_capture_id,
        )
        current_capture_id = next_capture_id

    memory.remember_page_detection(
        page_capture_id=current_capture_id,
        detection_status="recognized",
        page_type="video_list",
        page_state="stable",
        rule_matching_allowed=True,
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert frontier["explored_action_count"] == 12
    assert all(item["status"] != "blocked" for item in frontier["inactive_actions"])


def test_pending_nested_action_rebinds_to_latest_same_branch_capture() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )
    nested_payload = {
        "action": "click",
        "target": ".masonry-grid .data-item:visible",
        "target_keywords": ["nested_content", "bottom_tab:3"],
        "risk_level": "low",
        "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
        "expected_next_page_type": "content_detail",
        "navigation_depth": 2,
    }
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_actions=[nested_payload],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/2d",
        page_title="内容列表",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )

    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-2",
            page_type="content_list",
            candidate_actions=[nested_payload],
            next_page_hypotheses=[],
        )
        == []
    )
    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert action["source_page_capture_id"] == "capture-2"
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    memory.record_page_rule_result(
        page_capture_id="capture-2",
        result={
            "execution_id": "execution-1",
            "rule_id": "rule-1",
            "execution_status": "pass",
        },
        arguments={"rule_id": "rule-1", "execution_status": "pass"},
    )
    assert memory.prepare_next_exploration_action()["exploration_action_id"] == action_id


def test_business_navigation_candidate_derives_depth_from_source_capture() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )

    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "a.ai-channel",
                "target_keywords": ["AI频道"],
                "risk_level": "low",
                "purpose": "进入 AI 内容频道继续质检",
                "expected_next_page_type": "content_list",
            }
        ],
        next_page_hypotheses=[],
    )[0]

    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert action["navigation_depth"] == 1


def test_structured_nested_action_precedes_model_text_action_on_same_list() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "首个内容标题",
                "target_keywords": ["内容标题"],
                "risk_level": "low",
                "purpose": "进入首个低风险内容卡片",
                "expected_next_page_type": "content_detail",
            },
            {
                "action": "click",
                "target": ".masonry-grid .data-item:visible",
                "target_keywords": ["nested_content", "bottom_tab:3"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "content_detail",
                "navigation_depth": 2,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    assert selected["target_keywords"] == ["nested_content", "bottom_tab:3"]


def test_page_local_more_action_precedes_global_bottom_navigation() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "更多",
                "target_keywords": ["更多", "van-icon-arrow"],
                "risk_level": "low",
                "purpose": "查看更多同分类视频",
                "expected_next_page_type": "video_list",
            },
            {
                "action": "click",
                "target": ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"),
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["target"] == "更多"


def test_profile_local_action_precedes_global_bottom_navigation() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "积分任务",
                "target_keywords": ["积分任务", "活动"],
                "risk_level": "low",
                "purpose": "探索当前个人中心的低风险业务入口",
                "expected_next_page_type": "activity",
            },
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(1):visible",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "home",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["target"] == "积分任务"


def test_same_page_type_tab_precedes_global_bottom_navigation() -> None:
    """同类分类 Tab 必须在离开当前底部导航页前完成。"""
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(2):visible",
                "target_keywords": ["page_tab_navigation", "小说", "content_list"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "post_list",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["target_keywords"] == ["page_tab_navigation", "小说", "content_list"]


def test_stale_same_page_type_tab_fails_without_hard_reload() -> None:
    """防御性约束：已离开来源页的同类 Tab 不得通过 goto 重启应用。"""
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )
    tab_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(2):visible",
                "target_keywords": ["page_tab_navigation", "小说", "content_list"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="content_list",
        candidate_rules=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-latest",
        page_url="https://example.com/task",
        page_title="积分任务",
        page_seq=2,
        artifact_ids=["artifact-latest"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-latest",
        page_type="activity",
        candidate_rules=[],
    )

    assert memory.prepare_next_exploration_action() is None
    failed_tab = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == tab_action_id
    )
    assert failed_tab["status"] == "failed"
    assert failed_tab["result_page_capture_id"] == "capture-latest"
    assert failed_tab["failure_code"] == "LOCAL_PAGE_ACTION_SOURCE_LEFT"
    assert memory.requires_review() is True


def test_page_tabs_do_not_alternate_across_same_route_captures() -> None:
    """同一路由切换到另一个分类 Tab 后，不得把初始 Tab 再次加入队列。"""
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(2):visible",
                "target_keywords": ["page_tab_navigation", "小说", "content_list"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "post_list",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    tab_action_id, global_action_id = action_ids
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_rules=[],
    )
    assert memory.prepare_next_exploration_action()["exploration_action_id"] == tab_action_id
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/capture-1",
        page_title="二次元",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=tab_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="content_list",
        rule_matching_allowed=True,
    )

    repeated_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(1):visible",
                "target_keywords": ["page_tab_navigation", "推荐", "content_list"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="content_list",
        candidate_rules=[],
    )

    assert repeated_ids == []
    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert selected["exploration_action_id"] == global_action_id


def test_nested_content_precedes_missing_home_navigation_on_current_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    nested_action = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="masonry-grid"><div class="movie-item">'
            '<div class="video-title">content</div></div></div>'
        ],
    )[0].model_dump()
    home_action = {
        "action": "click",
        "target": ".van-tabs:visible .van-tab:nth-of-type(1):visible",
        "target_keywords": ["page_tab_navigation", "home"],
        "risk_level": "low",
        "purpose": "explore missing home coverage",
        "expected_next_page_type": "home",
        "navigation_depth": 1,
    }
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[nested_action, home_action],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["expected_next_page_type"] == "video_detail"


def test_global_navigation_precedes_repeated_home_tab_after_home_attempt() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    first_home_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(4):visible",
                "target_keywords": ["page_tab_navigation", "home"],
                "risk_level": "low",
                "purpose": "explore missing home coverage",
                "expected_next_page_type": "home",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )
    first = memory.prepare_next_exploration_action()
    assert first is not None
    assert first["exploration_action_id"] == first_home_ids[0]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/movie",
        page_title="视频列表",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=first_home_ids[0],
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-2",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(4):visible",
                "target_keywords": ["page_tab_navigation", "home"],
                "risk_level": "low",
                "purpose": "explore missing home coverage",
                "expected_next_page_type": "home",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="video_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    assert selected["expected_next_page_type"] == "profile"


def test_missing_settings_local_action_precedes_bottom_navigation_on_profile_page() -> None:
    memory = _memory_with_capture("capture-profile")
    memory.remember_page_detection(
        page_capture_id="capture-profile",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "post_list",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "设置",
                "target_keywords": ["设置"],
                "risk_level": "low",
                "purpose": "探索设置页",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    assert selected["expected_next_page_type"] == "settings"


def test_missing_payment_tab_precedes_redundant_video_detail_after_detail_covered() -> None:
    memory = _memory_with_capture("capture-covered")
    memory.remember_page_detection(
        page_capture_id="capture-covered",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    detail_id = memory.remember_exploration_candidates(
        page_capture_id="capture-covered",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "covered-detail-card",
                "target_keywords": ["covered_detail"],
                "risk_level": "low",
                "purpose": "covered detail",
                "expected_next_page_type": "video_detail",
                "navigation_depth": 2,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-covered",
        page_type="home",
        candidate_rules=[],
    )
    assert memory.prepare_next_exploration_action()["exploration_action_id"] == detail_id
    memory.remember_page_capture(
        page_capture_id="capture-detail",
        page_url="https://example.com/play/1",
        page_title="视频详情页",
        page_seq=2,
        artifact_ids=["artifact-detail"],
        exploration_action_id=detail_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-detail",
        detection_status="recognized",
        page_type="video_detail",
        rule_matching_allowed=True,
    )
    memory.remember_page_capture(
        page_capture_id="capture-video-2",
        page_url="https://example.com/movie",
        page_title="视频列表页",
        page_seq=3,
        artifact_ids=["artifact-video-2"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-video-2",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-video-2",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".movie-list .media-item:visible",
                "target_keywords": ["nested_content", "bottom_tab:2"],
                "risk_level": "low",
                "purpose": "进入首个低风险视频详情",
                "expected_next_page_type": "video_detail",
                "navigation_depth": 2,
            },
            {
                "action": "click",
                "target": "payment-center",
                "target_keywords": ["payment"],
                "risk_level": "low",
                "purpose": "explore payment center",
                "expected_next_page_type": "payment",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-video-2",
        page_type="video_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    assert selected["expected_next_page_type"] == "payment"


def test_nested_content_precedes_profile_bottom_navigation_on_current_list_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".movie-list .media-item:visible",
                "target_keywords": ["nested_content", "bottom_tab:2"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "video_detail",
                "navigation_depth": 2,
            },
            {
                "action": "click",
                "target": ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible"),
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["expected_next_page_type"] == "video_detail"


def test_structured_detail_return_precedes_model_chapter_action() -> None:
    memory = _memory_with_nested_detail_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "click",
                "target": "第1集",
                "target_keywords": ["chapter", "free"],
                "risk_level": "low",
                "purpose": "进入首个低风险免费章节阅读",
                "expected_next_page_type": "content_detail",
            },
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "详情质检完成后返回来源列表继续探索",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    assert selected["action"] == "back"


def test_detail_return_expires_stale_local_action_from_superseded_detail() -> None:
    memory = _memory_with_nested_detail_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_detail",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "click",
                "target": "第1集",
                "target_keywords": ["chapter", "free"],
                "risk_level": "low",
                "purpose": "进入首个低风险免费章节阅读",
                "expected_next_page_type": "content_detail",
            },
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "详情质检完成后返回来源列表继续探索",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    capture_defaults = memory.frontier_action_capture_defaults(action_ids[1])
    assert capture_defaults is not None
    assert capture_defaults["supersedes_page_capture_id"] == "capture-1"
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/list",
        page_title="内容列表",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_ids[1],
        supersedes_page_capture_id=str(capture_defaults["supersedes_page_capture_id"]),
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    stale_action = next(
        item
        for item in frontier["inactive_actions"]
        if item["exploration_action_id"] == action_ids[0]
    )
    assert stale_action["status"] == "skipped"
    assert stale_action["failure_reason"] == "source_page_superseded"


def test_superseded_popup_expires_other_pending_obstruction_actions() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {"action": "click", "target": ".popup-a:visible", "risk_level": "low"},
            {"action": "click", "target": ".popup-b:visible", "risk_level": "low"},
        ],
        next_page_hypotheses=[],
    )
    for action_id in action_ids:
        assert memory.require_exploration_action(
            exploration_action_id=action_id,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
    assert memory.mark_exploration_action_selected(action_ids[0])

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/product",
        page_title="稳定首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_ids[0],
        supersedes_page_capture_id="capture-1",
    )

    frontier = memory.audit_snapshot()["exploration_frontier"]
    statuses = {
        item["exploration_action_id"]: item["status"]
        for key in ("explored_actions", "inactive_actions")
        for item in frontier[key]
    }
    assert statuses == {
        action_ids[0]: "explored",
        action_ids[1]: "skipped",
    }
    assert memory.prepare_next_exploration_action() is None


def test_stable_page_expires_unlinked_popup_obstruction_source() -> None:
    memory = _memory_with_capture("capture-1")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[{"action": "click", "target": ".popup:visible", "risk_level": "low"}],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
    )
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/product",
        page_title="稳定首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )

    assert memory.expire_stale_obstruction_actions(current_page_capture_id="capture-2") == [
        action_id
    ]

    snapshot = memory.audit_snapshot()
    assert snapshot["inactive_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["inactive_pages"][0]["lifecycle_status"] == "superseded"
    assert snapshot["exploration_frontier"]["inactive_actions"][0]["status"] == "skipped"
    assert memory.prepare_next_exploration_action() is None


def test_quality_failure_is_completed_execution_not_degraded_execution() -> None:
    memory = _memory_with_capture("capture-1")
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    memory.record_page_rule_result(
        page_capture_id="capture-1",
        result={"rule_id": "rule-1", "execution_status": "fail"},
        arguments={},
    )

    page = memory.audit_snapshot()["terminal_pages"][0]
    assert page["inspection_status"] == "completed"
    assert page["quality_status"] == "fail"
    assert memory.is_page_exploration_converged() is True


def test_stale_page_tab_navigation_is_skipped_after_browser_moves_to_new_page() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(13):visible",
                "target_keywords": ["page_tab_navigation", "XVideos", "video_list"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "video_list",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/product",
        page_title="首页",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )

    assert memory.prepare_next_exploration_action() is None

    inactive_actions = memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
    stale_action = next(
        item for item in inactive_actions if item["exploration_action_id"] == action_id
    )
    assert stale_action["status"] == "skipped"
    assert stale_action["failure_reason"] == "stale_local_page_action"
    assert memory.has_pending_exploration() is False


def test_unmatchable_landing_marks_stale_page_tab_as_coverage_gap() -> None:
    memory = _memory_with_capture("capture-list")
    memory.remember_page_detection(
        page_capture_id="capture-list",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    stale_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-list",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tabs:visible .van-tab:nth-of-type(4):visible",
                "target_keywords": ["page_tab_navigation", "推荐", "home"],
                "risk_level": "low",
                "purpose": "探索稳定页面中的页内 Tab 业务分支",
                "expected_next_page_type": "home",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-list",
        page_type="video_list",
        candidate_rules=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-detail",
        page_url="https://example.com/moviePlayer?id=1",
        page_title="视频详情",
        page_seq=2,
        artifact_ids=["artifact-detail"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-detail",
        detection_status="recognized",
        page_type="video_detail",
        rule_matching_allowed=False,
    )
    assert memory.abandon_unmatchable_page(
        page_capture_id="capture-detail",
        reason="page_type_not_rule_matchable",
    )

    assert memory.prepare_next_exploration_action() is None

    inactive_actions = memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
    stale_action = next(
        item for item in inactive_actions if item["exploration_action_id"] == stale_action_id
    )
    assert stale_action["status"] == "failed"
    assert stale_action["failure_code"] == "LOCAL_PAGE_ACTION_SOURCE_LEFT"
    assert stale_action["failure_reason"] == "local_page_action_source_left"
    assert memory.has_pending_exploration() is False


def test_auxiliary_return_unstable_landing_is_not_dispatched_twice() -> None:
    memory, return_action_id = _memory_with_selected_auxiliary_return()
    memory.remember_page_capture(
        page_capture_id="capture-popup",
        page_url="https://example.com/capture-source",
        page_title="来源页弹窗",
        page_seq=3,
        artifact_ids=["artifact-popup"],
        exploration_action_id=return_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-popup",
        detection_status="recognized",
        page_type="profile",
        page_state="popup",
        rule_matching_allowed=True,
    )
    assert (
        memory.resolve_auxiliary_page_return_if_ready(
            page_capture_id="capture-popup",
            detection_status="recognized",
            page_type="profile",
            page_state="popup",
        )
        is None
    )
    memory.start_page_inspection(
        page_capture_id="capture-popup",
        page_type="profile",
        candidate_rules=[],
    )

    next_action = memory.prepare_next_exploration_action()

    assert next_action is None or next_action["exploration_action_id"] != return_action_id


@pytest.mark.parametrize("mark_selected", [False, True])
def test_high_risk_duplicate_cannot_be_hidden_by_low_risk_candidate(
    mark_selected: bool,
) -> None:
    memory = _memory_with_capture("capture-1")
    low_risk = {
        "action": "click",
        "target": ".video-card:first",
        "target_keywords": ["nested_content"],
        "risk_level": "low",
        "purpose": "进入视频详情",
        "expected_next_page_type": "video_detail",
        "navigation_depth": 1,
    }
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[low_risk],
        next_page_hypotheses=[],
    )[0]
    if mark_selected:
        assert memory.mark_exploration_action_selected(action_id)
    high_risk = {
        **low_risk,
        "target_keywords": ["nested_content", "支付"],
        "risk_level": "high",
    }

    assert (
        memory.remember_exploration_candidates(
            page_capture_id="capture-1",
            page_type="video_list",
            candidate_actions=[high_risk],
            next_page_hypotheses=[],
        )
        == []
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )

    action = next(
        item
        for item in memory.audit_snapshot()["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert action["risk_level"] == "high"
    assert action["status"] == "blocked"
    assert memory.prepare_next_exploration_action() is None


def test_query_only_auxiliary_route_still_adds_history_return() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-source",
        page_url="https://example.com/app?view=profile",
        page_title="个人中心",
        page_seq=1,
        artifact_ids=["artifact-source"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type="profile",
        page_state="stable",
        rule_matching_allowed=True,
    )
    entry_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "进入设置",
                "target_keywords": ["settings_entry"],
                "risk_level": "low",
                "purpose": "进入设置辅助页",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type="profile",
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(entry_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-settings",
        page_url="https://example.com/app?view=settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-settings"],
        exploration_action_id=entry_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-settings",
        detection_status="recognized",
        page_type="settings",
        page_state="stable",
        rule_matching_allowed=True,
    )

    added_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-settings",
        page_type="settings",
        candidate_actions=[],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-settings",
        page_type="settings",
        candidate_rules=[],
    )
    selected = memory.prepare_next_exploration_action()

    assert len(added_ids) == 1
    assert selected is not None
    assert selected["exploration_action_id"] == added_ids[0]
    assert selected["action"] == "back"
    assert "auxiliary_page_return" in selected["target_keywords"]


def test_auxiliary_return_requires_same_query_identity() -> None:
    memory, return_action_id = _memory_with_selected_auxiliary_return()
    memory.remember_page_capture(
        page_capture_id="capture-query-mismatch",
        page_url="https://example.com/capture-source?tab=security",
        page_title="错误查询参数落点",
        page_seq=3,
        artifact_ids=["artifact-query-mismatch"],
        exploration_action_id=return_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-query-mismatch",
        detection_status="recognized",
        page_type="profile",
        page_state="stable",
        rule_matching_allowed=True,
    )

    assert (
        memory.resolve_auxiliary_page_return_if_ready(
            page_capture_id="capture-query-mismatch",
            detection_status="recognized",
            page_type="profile",
            page_state="stable",
        )
        == "target_not_reached"
    )
    snapshot = memory.audit_snapshot()
    failed = next(
        item
        for item in snapshot["exploration_frontier"]["inactive_actions"]
        if item["exploration_action_id"] == return_action_id
    )
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "AUXILIARY_RETURN_TARGET_NOT_REACHED"
    auxiliary = next(
        page
        for page in [
            *snapshot["terminal_pages"],
            *snapshot["inactive_pages"],
            snapshot["current_page"],
        ]
        if page is not None and page["page_capture_id"] == "capture-auxiliary"
    )
    assert auxiliary["lifecycle_status"] == "active"


def _memory_with_selected_auxiliary_return(
    *,
    source_page_type: str = "profile",
    auxiliary_page_type: str = "settings",
) -> tuple[WorkingMemory, str]:
    memory = _memory_with_capture("capture-source")
    memory.remember_page_detection(
        page_capture_id="capture-source",
        detection_status="recognized",
        page_type=source_page_type,
        page_state="stable",
        rule_matching_allowed=True,
    )
    entry_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-source",
        page_type=source_page_type,
        candidate_actions=[
            {
                "action": "click",
                "target": f"进入{auxiliary_page_type}",
                "target_keywords": [f"{auxiliary_page_type}_entry"],
                "risk_level": "low",
                "purpose": f"进入{auxiliary_page_type}辅助页",
                "expected_next_page_type": auxiliary_page_type,
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-source",
        page_type=source_page_type,
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(entry_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-auxiliary",
        page_url=f"https://example.com/{auxiliary_page_type}",
        page_title="辅助页",
        page_seq=2,
        artifact_ids=["artifact-auxiliary"],
        exploration_action_id=entry_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-auxiliary",
        detection_status="recognized",
        page_type=auxiliary_page_type,
        page_state="stable",
        rule_matching_allowed=True,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-auxiliary",
        page_type=auxiliary_page_type,
        candidate_actions=[],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-auxiliary",
        page_type=auxiliary_page_type,
        candidate_rules=[],
    )
    selected = memory.prepare_next_exploration_action()
    assert selected is not None
    assert "auxiliary_page_return" in selected["target_keywords"]
    return memory, selected["exploration_action_id"]


def _memory_with_capture(page_capture_id: str) -> WorkingMemory:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id=page_capture_id,
        page_url=f"https://example.com/{page_capture_id}",
        page_title="测试页面",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    return memory


def _memory_with_nested_detail_capture(page_capture_id: str) -> WorkingMemory:
    """构造由列表真实进入详情的采集，供浏览器历史返回测试使用。"""
    memory = _memory_with_capture("capture-list")
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-list",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "a[href='/detail']",
                "target_keywords": ["nested_content", "path:/detail"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "content_detail",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.mark_exploration_action_selected(action_id)
    memory.remember_page_capture(
        page_capture_id=page_capture_id,
        page_url="https://example.com/detail",
        page_title="详情",
        page_seq=2,
        artifact_ids=["artifact-detail"],
        exploration_action_id=action_id,
        supersedes_page_capture_id="capture-list",
    )
    return memory


def test_coverage_gap_uses_recognized_result_and_selects_alternative_route() -> None:
    memory = _memory_with_capture("capture-profile")
    memory.remember_page_detection(
        page_capture_id="capture-profile",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    first_settings_id = memory.remember_exploration_candidates(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "settings-row",
                "target_keywords": ["settings"],
                "risk_level": "low",
                "purpose": "open settings",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-profile",
        page_type="profile",
        candidate_rules=[],
    )
    assert memory.prepare_next_exploration_action()["exploration_action_id"] == first_settings_id
    memory.remember_page_capture(
        page_capture_id="capture-login",
        page_url="https://example.com/login",
        page_title="Login",
        page_seq=2,
        artifact_ids=["artifact-login"],
        exploration_action_id=first_settings_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-login",
        detection_status="recognized",
        page_type="profile",
        page_state="login_required",
        rule_matching_allowed=False,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-login",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "settings-icon",
                "target_keywords": ["settings", "alternative-route"],
                "risk_level": "low",
                "purpose": "open settings through alternative route",
                "expected_next_page_type": "settings",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "bottom-community",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "open community",
                "expected_next_page_type": "post_list",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-login",
        page_type="profile",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[0]
    assert selected["expected_next_page_type"] == "settings"
