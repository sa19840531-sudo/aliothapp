"""H5 缺省 Probe 不再把交互补证退化为静态重拍。"""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.evidence_exit_gate import (
    is_trusted_h5_targeted_recapture_action,
    requires_evidence_exit_gate,
)
from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
from services.api.app.application.h5_probe_work import H5ProbeDecisionAdapter, H5ProbeWorkError
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    build_probe_work,
)
from services.api.app.domain.inspection import (
    EvidenceSideEffectPolicy,
    build_evidence_request,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.command_policy import CommandPolicy, CommandPolicyError
from services.api.app.domain.protocol import thaw_json_mapping
from services.api.app.infrastructure.browser.h5_evidence_recovery_runtime import (
    H5EvidenceRecoveryRuntime,
)
from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
from services.api.tests.test_h5_probe_work import NOW, _work
from services.api.tests.test_maf_explorer_agent import _work as _exploration


def _probe():
    original = _work()
    return _work(_request_like(original.evidence_request, required_interaction=None))


def _request_like(request, **changes):
    fields = (
        "work_item_id",
        "attempt_id",
        "task_id",
        "rule_id",
        "rule_code",
        "target_capture_id",
        "reason_code",
        "evidence_goal",
        "required_artifact_types",
        "required_interaction",
        "session_dependency",
        "replay_policy",
        "required_equivalence",
        "side_effect_policy",
        "target_invariants",
        "minimum_source_grade",
        "expires_at",
        "retryable",
    )
    return build_evidence_request(**{**{key: getattr(request, key) for key in fields}, **changes})


def _frontier(probe, *, operation=InteractionOperation.SCROLL):
    base = _exploration().frontier
    return replace(
        base,
        observation=replace(base.observation, capture_id=probe.source_capture_id),
        candidates=(
            replace(
                base.candidates[0],
                action_type=operation,
                navigation_depth=0,
                semantic_target="当前列表向下滚动",
                metadata={"action_parameters": {"direction": "down"}},
            ),
        ),
    )


async def test_typed_missing_artifact_goal_observes_without_model_and_keeps_evidence_pair():
    from services.api.tests.test_h5_recapture_coalescing import probe, request

    work = probe(request())
    frontier = _frontier(work)
    frontier = replace(frontier, observation=replace(
        frontier.observation, page_state="stable", overlay_signature=None,
    ))
    reader = SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier))
    selector = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    planner = H5ProbeGoalPlanner(frontiers=reader, delegate=selector, now=lambda: NOW)
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    result = await planner.plan(work=work, baseline=baseline)
    assert result is baseline
    assert result.action_type is InteractionOperation.OBSERVE
    assert result.capture_profile.before.role == "probe_before"
    assert result.capture_profile.after.role == "probe_result_replay"


@pytest.mark.parametrize("operation", [InteractionOperation.CLICK, InteractionOperation.SCROLL])
async def test_implicit_probe_selects_real_candidate_and_preserves_probe_lineage(operation):
    work = _runtime_work()
    frontier = _frontier(work, operation=operation)
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "kind": "action",
                        "candidate_id": "candidate-1",
                        "purpose": "补采交互后的证据",
                    }
                )
            )
        ]
    )
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    reader = SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier))
    adapter = H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(delegate=delegate, frontiers=reader, now=lambda: NOW),
    )
    result = await adapter.decide_next(work)
    assert isinstance(result, ActionIntent)
    assert result.action_type is operation
    assert result.purpose == f"定向补采：{result.semantic_target}"
    assert result.source_capture_id == work.source_capture_id
    assert result.parameters["direction"] == "down"
    assert result.parameters["exploration_work_id"] == work.work_id
    assert result.parameters["restoration_attempt_id"] == work.restoration_attempt_id
    assert result.capture_profile.before.role == "probe_before"
    assert result.capture_profile.after.role == "probe_result_replay"
    assert {"action_trace", *work.evidence_request.required_artifact_types} <= set(
        result.capture_profile.after.artifact_types
    )
    assert result.deadline == work.deadline
    if operation is InteractionOperation.CLICK:
        assert is_trusted_h5_targeted_recapture_action(result)
        assert not requires_evidence_exit_gate(result)
        for field in (
            "evidence_request_id", "exploration_work_id", "restoration_plan_hash",
            "restoration_attempt_id", "equivalence_assessment_id",
        ):
            incomplete = replace(
                result,
                parameters={key: value for key, value in result.parameters.items() if key != field},
            )
            assert requires_evidence_exit_gate(incomplete)


@pytest.mark.parametrize("goal", [
    "交互控件与反馈区域：连续滑动采样期间确认 DOM 响应；"
    "抽样点击按钮并采集反馈结果。允许重新观察当前页面。",
    "采集支付页说明文案，不执行支付、授权或提交。",
])
async def test_implicit_probe_risk_uses_actual_operation_not_whole_evidence_goal(goal):
    work = _runtime_work(evidence_goal=goal)
    frontier = _frontier(work)
    client = ScriptedChatClient([make_text_response(json.dumps({
        "kind": "action", "candidate_id": "candidate-1",
        "purpose": "允许授权后采集支付凭证",
    }))])
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            now=lambda: NOW,
        ),
    ).decide_next(work)
    assert isinstance(result, ActionIntent)
    assert work.evidence_request.evidence_goal == goal
    assert CommandPolicy().validate_resolved_target(result) is None
    # 候选自报 low 不能绕过实际目标中的危险语义。
    with pytest.raises(CommandPolicyError) as error:
        CommandPolicy().validate_resolved_target(replace(result, semantic_target="确认支付"))
    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"


@pytest.mark.parametrize("operation", [InteractionOperation.CLICK, InteractionOperation.SCROLL])
@pytest.mark.parametrize("stale", [False, True])
async def test_implicit_logical_probe_binds_fresh_candidate_source_contract(stale, operation):
    work = _runtime_work()
    frontier = _frontier(work, operation=operation)
    identity, state = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    candidate = replace(frontier.candidates[0], metadata={
        "logical_target_required": True,
        "source_identity_fingerprint": identity,
        "source_state_fingerprint": "sha256:" + "c" * 64 if stale else state,
        "source_page_type": "home", "expected_next_page_type": "content_list",
    })
    frontier = replace(frontier, candidates=(candidate,), observation=replace(
        frontier.observation, metadata={
            "logical_targets_effective": True,
            "logical_surface_identity_fingerprint": identity,
            "logical_surface_state_fingerprint": state,
        },
    ))
    client = ScriptedChatClient([make_text_response(json.dumps({
        "kind": "action", "candidate_id": candidate.candidate_id, "purpose": "补证",
    }))])
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    adapter = H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            now=lambda: NOW,
        ),
    )
    if stale:
        with pytest.raises(H5ProbeWorkError) as raised:
            await adapter.decide_next(work)
        assert raised.value.error_code == "LOGICAL_SOURCE_STATE_STALE"
    else:
        result = await adapter.decide_next(work)
        assert result.parameters["logical_target"] == {
            "protocol_version": "logical_target.v1",
            "source_identity_fingerprint": identity,
            "source_state_fingerprint": state,
            "source_page_type": "home", "expected_page_type": "content_list",
            "expects_logical_navigation": operation is InteractionOperation.CLICK,
            **({"expects_scroll_effect": True} if operation is InteractionOperation.SCROLL else {}),
        }
        assert result.parameters["frontier_snapshot_id"] == frontier.snapshot_id
        assert result.parameters["evidence_request_id"] == work.evidence_request.request_id


@pytest.mark.parametrize("mode", ["off", "shadow", "enforced"])
@pytest.mark.parametrize("parameters,metadata", [
    ({"target_mode": "visual_navigation_coordinate"}, {}),
    ({"visual_navigation_capability": {}}, {}),
    ({"visual_navigation_provenance": "vision"}, {}),
    ({"visual_target_kind": "navigation"}, {}),
    ({}, {"visual_navigation_target": True}),
    ({}, {"visual_target_kind": "navigation"}),
])
async def test_implicit_probe_never_downgrades_invalid_visual_provenance(
    mode, parameters, metadata,
):
    work = _runtime_work()
    frontier = _frontier(work, operation=InteractionOperation.CLICK)
    candidate = replace(frontier.candidates[0], metadata={
        **metadata, "action_parameters": parameters,
    })
    frontier = replace(frontier, candidates=(candidate,))
    client = ScriptedChatClient([make_text_response(json.dumps({
        "kind": "action", "candidate_id": candidate.candidate_id, "purpose": "补证",
    }))])
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    adapter = H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            h5_visual_coordinate_mode=mode, now=lambda: NOW,
        ),
    )
    with pytest.raises(H5ProbeWorkError, match="视觉导航能力"):
        await adapter.decide_next(work)


@pytest.mark.parametrize("mode", ["off", "shadow", "enforced"])
async def test_implicit_probe_preserves_valid_visual_capability_only_when_enforced(mode):
    from services.api.tests.test_h5_runtime_composition import (
        _visual_navigation_enforced_work_and_candidate,
    )

    base = _runtime_work()
    _, exploration, original, capability, template = (
        _visual_navigation_enforced_work_and_candidate()
    )
    work = build_probe_work(
        session_id=base.session_id, branch_id=base.branch_id, surface_kind=base.surface_kind,
        source_capture_id=exploration.source_capture_id,
        restoration_plan_hash=base.restoration_plan_hash,
        restoration_attempt_id=base.restoration_attempt_id,
        equivalence_assessment_id=base.equivalence_assessment_id, source_grade=base.source_grade,
        evidence_request=_request_like(base.evidence_request, task_id=exploration.task_id),
        budget=base.budget, deadline=base.deadline,
    )
    candidate = replace(original, navigation_depth=0, metadata={
        **original.metadata, "action_parameters": {
            "target_mode": "visual_navigation_coordinate",
            "visual_navigation_capability": capability,
        },
    })
    frontier = replace(template, candidates=(candidate,), observation=replace(
        template.observation, capture_id=work.source_capture_id,
    ))
    client = ScriptedChatClient([make_text_response(json.dumps({
        "kind": "action", "candidate_id": candidate.candidate_id, "purpose": "补证",
    }))])
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    adapter = H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            h5_visual_coordinate_mode=mode, now=lambda: NOW,
        ),
    )
    if mode != "enforced":
        with pytest.raises(H5ProbeWorkError, match="视觉导航能力"):
            await adapter.decide_next(work)
    else:
        result = await adapter.decide_next(work)
        assert thaw_json_mapping(result.parameters["visual_navigation_capability"]) == capability
        assert result.parameters["target_mode"] == "visual_navigation_coordinate"
        assert result.parameters["evidence_request_id"] == work.evidence_request.request_id


@pytest.mark.parametrize(
    "explicit,policy",
    [
        (True, EvidenceSideEffectPolicy.READ_ONLY),
        (False, EvidenceSideEffectPolicy.PROHIBITED),
    ],
)
async def test_explicit_or_prohibited_probe_never_calls_goal_model(explicit, policy):
    work = _work() if explicit else _probe()
    work = _work(_request_like(work.evidence_request, side_effect_policy=policy))
    delegate = MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW)
    reader = SimpleNamespace(read_probe_frontier=AsyncMock(side_effect=AssertionError))
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(delegate=delegate, frontiers=reader, now=lambda: NOW),
    ).decide_next(work)
    assert result.action_type is (
        InteractionOperation.CLICK if explicit else InteractionOperation.OBSERVE
    )
    reader.read_probe_frontier.assert_not_awaited()


async def test_unrelated_candidates_can_be_blocked_without_fabricated_success():
    work = _probe()
    frontier = _frontier(work)
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "kind": "terminal",
                        "terminal_status": "blocked",
                        "reason": "当前没有目标所需的安全候选",
                    }
                )
            )
        ]
    )
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    reader = SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier))
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(delegate=delegate, frontiers=reader, now=lambda: NOW),
    ).decide_next(work)
    assert isinstance(result, ExplorationTerminal)
    assert result.work_id == work.work_id
    assert result.status is ExplorationTerminalStatus.BLOCKED


@pytest.mark.parametrize("has_back", [True, False])
async def test_stable_implicit_probe_can_offer_current_observation_without_enabling_back(has_back):
    work = _runtime_work(evidence_goal="同一商品的价格区域缺少当前截图角色。")
    frontier = _frontier(work, operation=InteractionOperation.BACK)
    frontier = replace(
        frontier,
        observation=replace(frontier.observation, page_type="payment", page_state="stable"),
        candidates=frontier.candidates if has_back else (),
    )
    digest = hashlib.sha256(
        f"{work.work_id}:{frontier.snapshot_id}:{work.source_capture_id}".encode()
    ).hexdigest()
    candidate_id = f"h5-probe-observe:{digest}"
    client = ScriptedChatClient([make_text_response(json.dumps({
        "kind": "action", "candidate_id": candidate_id, "purpose": "观察当前价格区域",
    }))])
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            now=lambda: NOW,
        ),
    ).decide_next(work)
    assert isinstance(result, ActionIntent)
    assert result.action_type is InteractionOperation.OBSERVE
    assert result.source_capture_id == work.source_capture_id
    assert result.parameters["candidate_id"] == candidate_id
    assert "direction" not in result.parameters
    assert result.capture_profile.before.role == "probe_before"
    assert result.capture_profile.after.role == "probe_result_replay"
    assert result.deadline == work.deadline


async def test_stable_observation_option_does_not_satisfy_missing_share_landing():
    work = _runtime_work(evidence_goal="需要分享链接在第三方浏览器中的真实落地证据。")
    frontier = _frontier(work, operation=InteractionOperation.BACK)
    frontier = replace(frontier, observation=replace(
        frontier.observation, page_type="activity", page_state="stable",
    ))
    client = ScriptedChatClient([make_text_response(json.dumps({
        "kind": "terminal", "terminal_status": "blocked",
        "reason": "当前页面观察不能证明第三方浏览器分享落地",
    }))])
    delegate = MafExplorerAgent(client=client, now=lambda: NOW)
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            now=lambda: NOW,
        ),
    ).decide_next(work)
    assert isinstance(result, ExplorationTerminal)
    assert result.status is ExplorationTerminalStatus.BLOCKED
    assert result.work_id == work.work_id
    assert work.evidence_request.evidence_goal == "需要分享链接在第三方浏览器中的真实落地证据。"


@pytest.mark.parametrize("mutation", ["unknown", "loading", "overlay", "auth", "blocked_observe"])
async def test_probe_observation_option_does_not_bypass_source_or_candidate_blockers(mutation):
    work = _runtime_work()
    frontier = _frontier(work, operation=InteractionOperation.BACK)
    observation = replace(frontier.observation, page_type="payment", page_state="stable")
    if mutation == "unknown":
        observation = replace(observation, page_type=None)
    elif mutation == "loading":
        observation = replace(observation, page_state="loading")
    elif mutation == "overlay":
        observation = replace(observation, overlay_signature="overlay:fixture")
    elif mutation == "auth":
        observation = replace(observation, metadata={"requires_auth": True})
    else:
        frontier = replace(
            frontier,
            candidates=(replace(frontier.candidates[0], action_type=InteractionOperation.OBSERVE),),
            blocked_candidate_ids=(frontier.candidates[0].candidate_id,),
        )
    frontier = replace(frontier, observation=observation)
    delegate = MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW)
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            now=lambda: NOW,
        ),
    ).decide_next(work)
    assert isinstance(result, ExplorationTerminal)
    assert result.status in {ExplorationTerminalStatus.BLOCKED, ExplorationTerminalStatus.BOUNDED}


@pytest.mark.parametrize("mutation", ["capture", "risk", "depth", "blocked"])
async def test_probe_frontier_scope_and_safety_are_not_relaxed(mutation):
    work = _probe()
    frontier = _frontier(work)
    if mutation == "capture":
        frontier = replace(frontier, observation=replace(frontier.observation, capture_id="other"))
    elif mutation == "risk":
        frontier = replace(
            frontier,
            candidates=(
                replace(
                    frontier.candidates[0],
                    risk_level=InteractionRiskLevel.HIGH,
                ),
            ),
        )
    elif mutation == "depth":
        frontier = replace(
            frontier, candidates=(replace(frontier.candidates[0], navigation_depth=5),)
        )
    else:
        frontier = replace(frontier, blocked_candidate_ids=("candidate-1",))
    delegate = MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW)
    reader = SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier))
    adapter = H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(delegate=delegate, frontiers=reader, now=lambda: NOW),
    )
    if mutation == "capture":
        with pytest.raises(H5ProbeWorkError, match="来源 Capture"):
            await adapter.decide_next(work)
    else:
        result = await adapter.decide_next(work)
        assert isinstance(result, ExplorationTerminal)


async def test_probe_goal_planning_timeout_is_bounded_and_cancels_reader():
    work = _probe()
    work = replace(work, budget=replace(work.budget, remaining_duration_ms=5))
    cancelled = asyncio.Event()

    async def read_probe_frontier(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    delegate = MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW)
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=read_probe_frontier),
            now=lambda: NOW,
        ),
    ).decide_next(work)
    assert isinstance(result, ExplorationTerminal)
    assert result.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert cancelled.is_set()


def _runtime_work(*, evidence_goal=None):
    work = _probe()
    return build_probe_work(
        session_id="h5:01234567-89ab-cdef-0123-456789abcdef",
        branch_id=work.branch_id,
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        restoration_plan_hash=work.restoration_plan_hash,
        restoration_attempt_id=work.restoration_attempt_id,
        equivalence_assessment_id=work.equivalence_assessment_id,
        source_grade=work.source_grade,
        evidence_request=(
            _request_like(work.evidence_request, evidence_goal=evidence_goal)
            if evidence_goal is not None else work.evidence_request
        ),
        budget=work.budget,
        deadline=work.deadline,
    )


def _runtime(work, *, captures=None):
    session = SimpleNamespace(
        task_id=work.task_id,
        current_capture_id=work.source_capture_id,
        session_owner="h5-workflow",
        status="active",
        surface_kind=SurfaceKind.WEB,
    )
    detector = SimpleNamespace(
        detect=AsyncMock(
            return_value={
                "page_capture_id": work.source_capture_id,
                "page_url": "https://fixture.test/",
                "page_type": "home",
                "page_state": "stable",
                "confidence": 0.9,
                "matched_catalog_signals": [],
                "candidate_actions": [],
            }
        )
    )
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=SimpleNamespace(get=AsyncMock(return_value=session)),
        capture_coordinator=object(),
        detector=detector,
        page_graph=object(),
        captures=captures,
        now=lambda: NOW,
    )
    return runtime, session, detector


async def test_probe_reuses_restoration_frontier_without_another_model_call():
    work = _runtime_work()
    runtime, _, detector = _runtime(work)
    frontier = _frontier(work)
    runtime._frontiers[work.source_capture_id] = frontier
    assert await runtime.read_probe_frontier(work=work) is frontier
    detector.detect.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "other-task"),
        ("current_capture_id", "new-capture"),
        ("session_owner", "web-workflow"),
        ("status", "closed"),
    ],
)
async def test_probe_rejects_live_scope_drift_even_when_frontier_is_cached(field, value):
    work = _runtime_work()
    runtime, session, _ = _runtime(work)
    runtime._frontiers[work.source_capture_id] = _frontier(work)
    setattr(session, field, value)
    with pytest.raises(EvidenceRecoveryReplayError, match="当前活会话"):
        await runtime.read_probe_frontier(work=work)


async def test_restart_probe_rebuilds_only_verified_same_capture_candidates():
    work = _runtime_work()
    capture = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=work.source_capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
    )
    reader = SimpleNamespace(
        get_verified_capture=AsyncMock(
            return_value=SimpleNamespace(
                capture=capture,
            )
        )
    )
    runtime, _, detector = _runtime(work, captures=reader)
    frontier = await runtime.read_probe_frontier(work=work)
    assert frontier.observation.capture_id == work.source_capture_id
    detector.detect.assert_awaited_once()
    assert detector.detect.await_args.kwargs["capture"] is capture


async def test_restart_probe_without_verified_source_does_not_guess_candidates():
    work = _runtime_work()
    runtime, _, detector = _runtime(work)
    with pytest.raises(EvidenceRecoveryReplayError, match="已校验"):
        await runtime.read_probe_frontier(work=work)
    detector.detect.assert_not_awaited()


@pytest.mark.parametrize("recovery", [False, True])
async def test_probe_rejects_canonical_and_recovery_owner_cross_binding(recovery):
    work = _runtime_work()
    run_id = work.session_id.split(":")[1]
    if recovery:
        work = replace(work, session_id=f"{work.session_id}:recovery:{'a' * 20}")
    runtime, session, _ = _runtime(work)
    session.session_owner = "h5-workflow" if recovery else f"h5-browser-recovery:{run_id}"
    runtime._frontiers[work.source_capture_id] = _frontier(work)
    with pytest.raises(EvidenceRecoveryReplayError, match="当前活会话"):
        await runtime.read_probe_frontier(work=work)


async def test_probe_provider_timeout_is_not_mislabeled_as_work_budget_exhaustion():
    work = _probe()
    planner = H5ProbeGoalPlanner(
        delegate=SimpleNamespace(decide_next=AsyncMock(side_effect=TimeoutError("provider"))),
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=_frontier(work))),
        now=lambda: NOW,
    )
    with pytest.raises(TimeoutError, match="provider"):
        await H5ProbeDecisionAdapter(
            delegate=MafExplorerAgent(agent=object(), now=lambda: NOW), goal_planner=planner
        ).decide_next(work)


async def test_main_exploration_completion_never_short_circuits_probe_goal_selection():
    from services.api.app.domain.exploration.h5_frontier import (
        H5_TASK_FRONTIER_METADATA_KEY,
        H5TaskFrontierSnapshot,
    )

    work = _probe()
    frontier = _frontier(work)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={
                **frontier.observation.metadata,
                H5_TASK_FRONTIER_METADATA_KEY: H5TaskFrontierSnapshot.create(
                    max_pages=None
                ).to_dict(),
                "objective_satisfied": True,
            },
        ),
    )
    delegate = MafExplorerAgent(
        client=ScriptedChatClient(
            [
                make_text_response(
                    json.dumps(
                        {"kind": "action", "candidate_id": "candidate-1", "purpose": "补充滚动证据"}
                    )
                ),
            ]
        ),
        now=lambda: NOW,
    )
    result = await H5ProbeDecisionAdapter(
        delegate=delegate,
        goal_planner=H5ProbeGoalPlanner(
            delegate=delegate,
            frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
            now=lambda: NOW,
        ),
    ).decide_next(work)
    assert isinstance(result, ActionIntent)
    assert result.action_type is InteractionOperation.SCROLL
