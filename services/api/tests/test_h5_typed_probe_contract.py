"""目录切换和到底补证不能用观察或无关点击冒充；缺候选不调用模型。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
from services.api.app.domain.exploration import ExplorationTerminal, build_probe_work
from services.api.app.domain.inspection import EvidenceInteractionRequirement
from services.api.app.domain.inspection.evidence_gap import materialize_evidence_request
from services.api.app.domain.interaction import ActionIntent, InteractionOperation
from services.api.tests.test_evidence_gap import _gap
from services.api.tests.test_h5_probe_goal_planning import (
    NOW,
    _frontier,
    _request_like,
    _runtime_work,
)


def _with_request(work, request, *, budget=None):
    values = {
        key: getattr(work, key)
        for key in (
            "session_id",
            "branch_id",
            "surface_kind",
            "source_capture_id",
            "restoration_plan_hash",
            "restoration_attempt_id",
            "equivalence_assessment_id",
            "source_grade",
            "evidence_request",
            "budget",
            "deadline",
        )
    }
    values["evidence_request"] = request
    if budget is not None:
        values["budget"] = budget
    return build_probe_work(**values)


@pytest.mark.parametrize(
    "candidate_operation",
    [InteractionOperation.OBSERVE, InteractionOperation.CLICK, InteractionOperation.SCROLL],
)
async def test_scroll_contract_rejects_unrelated_action_before_model(candidate_operation):
    work = _runtime_work()
    request = _request_like(
        work.evidence_request,
        required_interaction=EvidenceInteractionRequirement(
            operation=InteractionOperation.SCROLL,
            semantic_target="当前页面纵向滚动区域",
            purpose="补充滚动到底后的证据",
            parameters={"goal_capability": "vertical_scroll"},
        ),
    )
    work = _with_request(work, request)
    frontier = _frontier(work, operation=candidate_operation)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            page_state="stable",
            overlay_signature=None,
        ),
    )
    delegate = SimpleNamespace(
        decide_next=AsyncMock(side_effect=AssertionError("单一确定性候选不应调用模型"))
    )
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    result = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    if candidate_operation is InteractionOperation.SCROLL:
        assert isinstance(result, ActionIntent)
        assert result.parameters["candidate_id"] == frontier.candidates[0].candidate_id
        assert result.parameters["direction"] == "down"
        assert result.capture_profile.before.role == "probe_before"
        assert result.capture_profile.after.role == "probe_result_replay"
    else:
        assert isinstance(result, ExplorationTerminal)
        assert result.work_id == work.work_id
    assert delegate.decide_next.await_count == 0
    if isinstance(result, ActionIntent):
        assert result.deadline == baseline.deadline


async def test_top_tab_goal_does_not_authorize_payment_or_generic_click():
    work = _runtime_work()
    work = _with_request(
        work,
        _request_like(
            work.evidence_request,
            required_interaction=EvidenceInteractionRequirement(
                operation=InteractionOperation.CLICK,
                semantic_target="当前页面顶部目录切换控件",
                purpose="补充顶部目录切换前后证据",
                parameters={"goal_capability": "page_tab_navigation"},
            ),
        ),
    )
    frontier = _frontier(work, operation=InteractionOperation.CLICK)
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    result = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    assert isinstance(result, ExplorationTerminal)
    assert delegate.decide_next.await_count == 0


@pytest.mark.parametrize("state", ["loading", "overlay", "auth"])
async def test_typed_scroll_does_not_skip_unstable_or_obstructed_surface(state):
    work = _runtime_work()
    work = _with_request(
        work,
        _request_like(
            work.evidence_request,
            required_interaction=EvidenceInteractionRequirement(
                operation=InteractionOperation.SCROLL,
                semantic_target="当前页面纵向滚动区域",
                purpose="补充滚动前后证据",
                parameters={"goal_capability": "vertical_scroll"},
            ),
        ),
    )
    frontier = _frontier(work, operation=InteractionOperation.SCROLL)
    observation = replace(
        frontier.observation,
        page_state="loading" if state == "loading" else "stable",
        overlay_signature="a" * 64 if state == "overlay" else None,
        metadata={"requires_auth": state == "auth"},
    )
    frontier = replace(frontier, observation=observation)
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    result = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    assert isinstance(result, ExplorationTerminal)
    assert delegate.decide_next.await_count == 0


@pytest.mark.parametrize(
    "operation,goal",
    [
        (InteractionOperation.SCROLL, "vertical_scroll"),
        (InteractionOperation.CLICK, "page_tab_navigation"),
    ],
)
def test_gap_materialization_preserves_typed_goal_and_rejects_goal_drift(operation, goal):
    requirement = EvidenceInteractionRequirement(
        operation=operation,
        semantic_target="当前页面低风险补证目标",
        purpose="补充前后证据",
        parameters={"goal_capability": goal},
    )
    gap = _gap(
        input_contract={"recapture_policy": "equivalent_state"}, required_interaction=requirement
    )
    kwargs = dict(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
    )
    request = materialize_evidence_request(gap=gap.to_dict(), **kwargs)
    assert request.required_interaction.to_dict() == requirement.to_dict()
    assert request.request_id == gap.requested_request["request_id"]
    tampered = deepcopy(gap.to_dict())
    tampered["target"]["required_interaction"]["parameters"]["goal_capability"] = "other"
    with pytest.raises(ValueError):
        materialize_evidence_request(gap=tampered, **kwargs)


@pytest.mark.parametrize("valid_signature", [False, True])
@pytest.mark.parametrize("depth_allowed", [False, True])
async def test_typed_top_tab_preserves_existing_signature_gate(valid_signature, depth_allowed):
    work = _runtime_work()
    work = _with_request(
        work,
        _request_like(
            work.evidence_request,
            required_interaction=EvidenceInteractionRequirement(
                operation=InteractionOperation.CLICK,
                semantic_target="当前页面顶部目录切换控件",
                purpose="补充顶部目录切换前后证据",
                parameters={"goal_capability": "page_tab_navigation"},
            ),
        ),
        budget=replace(work.budget, max_depth=int(depth_allowed)),
    )
    frontier = _frontier(work, operation=InteractionOperation.CLICK)
    parameters = {
        "target_mode": "page_tab_navigation",
        "navigation_index": 2,
        "navigation_label": "栏目二",
        "interaction_pattern_signature": "a" * 64 if valid_signature else "broken",
    }
    candidate = replace(
        frontier.candidates[0],
        semantic_target="internal:page_tab:nth-of-type(2)",
        navigation_depth=1,
        metadata={
            "action_parameters": parameters,
            "target_keywords": ["page_tab_navigation", "栏目二"],
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 1.0,
        },
    )
    frontier = replace(
        frontier,
        candidates=(candidate,),
        observation=replace(
            frontier.observation,
            page_type="home",
            page_state="stable",
            overlay_signature=None,
        ),
    )
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    planner = H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        now=lambda: NOW,
    )
    if not depth_allowed:
        assert isinstance(await planner.plan(work=work, baseline=baseline), ExplorationTerminal)
    elif valid_signature:
        result = await planner.plan(work=work, baseline=baseline)
        assert result.parameters["interaction_pattern_signature"] == "a" * 64
        assert result.capture_profile.before == baseline.capture_profile.before
        assert result.capture_profile.after == baseline.capture_profile.after
        assert result.deadline == baseline.deadline
    else:
        with pytest.raises(EvidenceRecoveryReplayError):
            await planner.plan(work=work, baseline=baseline)
    assert delegate.decide_next.await_count == 0
