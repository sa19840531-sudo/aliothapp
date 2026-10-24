"""封面原对象绑定与展示补证预算、门禁的反例。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeResult,
    _attach_evidence_gap,
    _h5_gap_interaction,
)
from services.api.app.domain.exploration import ExplorationTerminal
from services.api.app.domain.interaction import ActionIntent, InteractionOperation
from services.api.tests.test_h5_pair_probe_targets import _snapshot
from services.api.tests.test_h5_probe_goal_planning import (
    NOW,
    _frontier,
    _request_like,
    _runtime_work,
)
from services.api.tests.test_h5_typed_probe_contract import _with_request
from services.api.tests.test_rule_judge import _request


@pytest.mark.parametrize("drift", [False, True])
@pytest.mark.parametrize("depth", [0, 2])
async def test_cover_goal_keeps_original_subject_instead_of_clicking_new_first_card(drift, depth):
    original = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#title",
                "tag": "div",
                "text": "原封面对象",
                "class_tokens": ["videoTitle"],
                "ancestor_class_tokens": [],
                "visible": True,
                "hit_test": {"center_hits_target": True},
                "rect": {"x": 20, "y": 100, "width": 120, "height": 50},
            }
        ],
    }
    packet = SimpleNamespace(
        packet_payload={
            "identity": {"target_platform": "h5"},
            "page": {"surface_kind": "web", "page_type": "home"},
            "facts": {"ui_snapshot": original},
        }
    )
    requirement = _h5_gap_interaction(
        RuleJudgeDefinition(
            rule_id="rule",
            rule_code="alioth_15",
            executor_type="hybrid",
            judge_config={"capability_config": {"batch_group": "cover-content"}},
        ),
        packet,
    )
    assert requirement.parameters["probe_subject_key"]
    work = _runtime_work()
    work = _with_request(
        work,
        _request_like(work.evidence_request, required_interaction=requirement),
        budget=replace(work.budget, max_depth=depth),
    )
    frontier = _frontier(work, operation=InteractionOperation.CLICK)
    candidate = replace(
        frontier.candidates[0],
        semantic_target="当前列表首个内容卡片",
        navigation_depth=2,
        metadata={
            "action_parameters": {
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".videoTitle:visible",
                "source_page_type": "home",
                "expected_page_type": "video_detail",
            },
            "expected_next_page_type": "video_detail",
            "target_keywords": ["nested_content", "list_detail_navigation"],
        },
    )
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation, page_type="home", page_state="stable", overlay_signature=None
        ),
        candidates=(candidate,),
    )
    current = deepcopy(original)
    if drift:
        current["elements"][0]["text"] = "另一内容对象"
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    result = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=current)),
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    delegate.decide_next.assert_not_awaited()
    if drift or depth == 0:
        assert isinstance(result, ExplorationTerminal)
    else:
        assert isinstance(result, ActionIntent)
        assert result.parameters["probe_subject_key"] == requirement.parameters["probe_subject_key"]
        assert result.deadline == baseline.deadline


@pytest.mark.parametrize(
    "bad",
    [
        "missing_original",
        "object_drift",
        "loading",
        "overlay",
        "auth",
        "deadline",
        "unknown_snapshot_reader",
    ],
)
async def test_display_probe_does_not_skip_identity_or_existing_gate(bad):
    goal = "media_fullscreen_pair"
    snapshot = _snapshot(goal)
    key = probe_subject_key(select_probe_target(snapshot, goal=goal, page_type="content_detail"))
    from services.api.app.domain.inspection import EvidenceInteractionRequirement

    work = _runtime_work()
    work = _with_request(
        work,
        _request_like(
            work.evidence_request,
            required_interaction=EvidenceInteractionRequirement(
                operation=InteractionOperation.CLICK,
                semantic_target="放大当前帖子媒体",
                purpose="补充前后证据",
                parameters={
                    "goal_capability": goal,
                    **({} if bad == "missing_original" else {"probe_subject_key": key}),
                },
            ),
        ),
    )
    frontier = _frontier(work)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            page_type="content_detail",
            page_state="loading" if bad == "loading" else "stable",
            overlay_signature="a" * 64 if bad == "overlay" else None,
            metadata={"requires_auth": bad == "auth"},
        ),
    )
    if bad == "object_drift":
        snapshot["elements"][0]["src"] = "另一个媒体"
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    if bad == "deadline":
        baseline = replace(baseline, deadline=NOW)
    result = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        snapshot_reader=None
        if bad == "unknown_snapshot_reader"
        else SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    assert isinstance(result, ExplorationTerminal)
    delegate.decide_next.assert_not_awaited()


@pytest.mark.parametrize(
    "group,code,page,goal",
    [
        ("cover-content", "alioth_15", "content_list", "cover_content_pair"),
        ("interaction-state", "alioth_46", "post_list", "media_fullscreen_pair"),
        ("payment-entry", "alioth_ops_4", "profile", "payment_entry_pair"),
        ("payment-entry", "alioth_ops_4", "payment", "payment_plan_selection"),
    ],
)
def test_pair_gap_keeps_fresh_roles_and_serialized_contract(group, code, page, goal):
    from services.api.app.domain.inspection.evidence_gap import materialize_evidence_request

    definition = RuleJudgeDefinition(
        rule_id="rule-1",
        rule_code=code,
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": group}},
        input_contract={"recapture_policy": "equivalent_state"},
    )
    packet = SimpleNamespace(
        packet_payload={
            "identity": {"target_platform": "h5"},
            "page": {
                "surface_kind": "web",
                "page_type": page,
                "page_state": "stable",
                "url": "https://fixture.invalid/page",
                "page_capture_id": "capture-1",
            },
        },
        observation_packet_id="packet-1",
    )
    result = RuleJudgeResult(
        rule_id="rule-1",
        rule_code=code,
        executor_type="hybrid",
        judge_strategy="hybrid",
        execution_status="evidence_insufficient",
        confidence=0,
        result_summary="缺配对",
        problem_desc="缺动作前后配对",
        page_capture_id="capture-1",
        evidence_artifact_ids=(),
        judge_input={},
        judge_output={},
    )
    attached = _attach_evidence_gap(
        result,
        definition=definition,
        request=_request(rule_code=code, judge_input={}),
        packet=packet,
    )
    gap = attached.judge_output["evidence_gap"]
    assert gap["missing"]["capture_roles"] == ["before", "primary"]
    kwargs = dict(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code=code,
        page_capture_id="capture-1",
    )
    request = materialize_evidence_request(gap=gap, **kwargs)
    assert request.required_interaction.parameters["goal_capability"] == goal
    assert request.required_interaction.operation is InteractionOperation.CLICK
    tampered = deepcopy(gap)
    tampered["target"]["required_interaction"]["parameters"]["probe_subject_key"] = "f" * 64
    with pytest.raises(ValueError):
        materialize_evidence_request(gap=tampered, **kwargs)
