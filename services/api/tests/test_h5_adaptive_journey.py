"""检查点执行与精确证据绑定的受控复现、串属和时序反例。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.domain.journey import parse_journey_definition
from services.api.app.domain.journey.definitions import build_run_journey_execution_plan
from services.api.tests.test_h5_exploration_turn import _work
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition


def evidence_case():
    now = datetime.now(UTC)
    return {
        "scope": dict(
            task_id="task",
            agent_run_id="run",
            session_id="session",
            epoch=0,
            source_capture_id="source",
            intent_id="intent",
        ),
        "action": dict(
            task_id="task",
            session_id="session",
            finalization_epoch=0,
            intent_id="intent",
            action_execution_id="action",
            status="completed",
            requested_source_capture_id="source",
            source_capture_id="before",
            result_capture_id="after",
            command_sequence=2,
            started_at=now,
            action_finished_at=now + timedelta(seconds=1),
        ),
        "captures": [
            dict(
                id="before",
                task_id="task",
                agent_run_id="run",
                session_id="session",
                epoch=0,
                role="before_action",
                action_execution_id="action",
                parent_capture_id="source",
                command_sequence=2,
                status="captured",
                captured_at=now - timedelta(milliseconds=1),
                artifact_ids=["before-png"],
            ),
            dict(
                id="after",
                task_id="task",
                agent_run_id="run",
                session_id="session",
                epoch=0,
                role="after_action",
                action_execution_id="action",
                parent_capture_id="before",
                command_sequence=2,
                status="captured",
                captured_at=now + timedelta(seconds=2),
                artifact_ids=["after-png"],
            ),
        ],
        "manifests": [
            dict(
                manifest_id="manifest-before",
                manifest_version=1,
                task_id="task",
                session_id="session",
                capture_id="before",
                quality_status="pass",
                artifacts=[
                    dict(
                        artifact_id="before-png",
                        artifact_type="screenshot",
                        role="before_action",
                        checksum="a" * 64,
                    )
                ],
            ),
            dict(
                manifest_id="manifest-after",
                manifest_version=2,
                task_id="task",
                session_id="session",
                capture_id="after",
                quality_status="pending",
                artifacts=[
                    dict(
                        artifact_id="after-png",
                        artifact_type="screenshot",
                        role="after_action",
                        checksum="b" * 64,
                    )
                ],
            ),
        ],
    }


def test_binding_keeps_latest_pending_version_without_promoting_it_to_pass():
    from services.api.app.domain.journey.checkpoint_evidence import bind_checkpoint_evidence

    binding = bind_checkpoint_evidence(**evidence_case())
    assert binding[1]["manifest_version"] == 2
    assert binding[1]["quality_status"] == "pending"
    assert binding[1]["action_execution_id"] == "action"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["captures"][1].update(task_id="other"),
        lambda c: c["captures"][1].update(agent_run_id="other"),
        lambda c: c["captures"][1].update(session_id="other"),
        lambda c: c["captures"][1].update(epoch=1),
        lambda c: c["captures"][1].update(role="probe_before"),
        lambda c: c["captures"][1].update(parent_capture_id="source"),
        lambda c: c["captures"][1].update(action_execution_id="other"),
        lambda c: c["captures"][1].update(command_sequence=3),
        lambda c: c["captures"][1].update(status="failed"),
        lambda c: c["captures"][1].update(captured_at=c["action"]["started_at"]),
        lambda c: c["action"].update(intent_id="other"),
        lambda c: c["action"].update(requested_source_capture_id="old"),
        lambda c: c["action"].update(status="executing"),
        lambda c: c["manifests"][1].update(capture_id="before"),
        lambda c: c["manifests"][1]["artifacts"][0].update(artifact_id="foreign"),
        lambda c: c["manifests"][1]["artifacts"][0].update(role="before_action"),
    ],
)
def test_binding_rejects_cross_scope_stale_before_frame_and_duplicate_artifacts(mutate):
    from services.api.app.domain.journey.checkpoint_evidence import bind_checkpoint_evidence

    case = deepcopy(evidence_case())
    mutate(case)
    with pytest.raises(ValueError):
        bind_checkpoint_evidence(**case)


def test_same_pixels_are_retained_but_cannot_count_as_independent_frames():
    from services.api.app.domain.journey.checkpoint_evidence import bind_checkpoint_evidence

    case = evidence_case()
    case["manifests"][1]["artifacts"][0]["checksum"] = "a" * 64
    bindings = bind_checkpoint_evidence(**case)
    assert bindings[1]["independent"] is False


def test_different_snapshot_metadata_cannot_turn_duplicate_screenshot_into_independent_frame():
    from services.api.app.domain.journey.checkpoint_evidence import bind_checkpoint_evidence

    case = evidence_case()
    case["manifests"][1]["artifacts"][0]["checksum"] = "a" * 64
    for index, role in enumerate(("before_action", "after_action")):
        artifact_id = f"snapshot-{index}"
        case["captures"][index]["artifact_ids"].append(artifact_id)
        case["manifests"][index]["artifacts"].append(
            dict(
                artifact_id=artifact_id,
                artifact_type="ui_snapshot",
                role=role,
                checksum=str(index) * 64,
            )
        )
    assert bind_checkpoint_evidence(**case)[1]["independent"] is False


def run_plan(work):
    payload = checkpoint_definition()
    return build_run_journey_execution_plan(
        parse_journey_definition(payload),
        task_id=work.task_id,
        agent_run_id="run",
        scenario_key="manual",
        parameters={},
    )


async def test_no_plans_delegates_once_without_reserving_or_replaying():
    from services.api.app.application.h5_adaptive_journey import H5AdaptiveJourneyRunner

    repository = SimpleNamespace(list_plans=AsyncMock(return_value=()), reserve=AsyncMock())
    executor = SimpleNamespace(execute=AsyncMock(return_value="ordinary-turn"))
    runner = H5AdaptiveJourneyRunner(repository=repository, planner=None)
    work = _work()
    assert await runner.prepare(work=work, agent_run_id="run") == work
    assert await runner.execute(work=work, agent_run_id="run", executor=executor) == "ordinary-turn"
    repository.reserve.assert_not_awaited()
    executor.execute.assert_awaited_once()


@pytest.mark.parametrize("status", ["bound", "reserved", "gap"])
async def test_redelivery_does_not_redecide_or_replay_checkpoint_action(status):
    from services.api.app.application.h5_adaptive_journey import (
        ADAPTIVE_BINDING_KEY,
        H5AdaptiveJourneyRunner,
    )

    work = _work()
    context = dict(
        task_id=work.task_id,
        agent_run_id="run",
        session_id=work.session_id,
        work_id=work.work_id,
        source_capture_id=work.source_capture_id,
        attempt_id="attempt",
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={ADAPTIVE_BINDING_KEY: context},
            ),
        ),
    )
    repository = SimpleNamespace(
        recover_attempt=AsyncMock(return_value=status),
        read_turn=AsyncMock(return_value="original-turn"),
        reserve=AsyncMock(),
    )
    executor = SimpleNamespace(
        decide=AsyncMock(side_effect=AssertionError("重领不能再次规划")),
        execute_decision=AsyncMock(return_value="blocked-turn"),
    )
    runner = H5AdaptiveJourneyRunner(repository=repository, planner=None)
    result = await runner.execute(work=work, agent_run_id="run", executor=executor)
    assert result == ("original-turn" if status == "bound" else "blocked-turn")
    executor.decide.assert_not_awaited()
    repository.reserve.assert_not_awaited()
    if status == "bound":
        executor.execute_decision.assert_not_awaited()
    else:
        terminal = executor.execute_decision.await_args.kwargs["decision"]
        assert terminal.reason_code == "H5_CHECKPOINT_ATTEMPT_UNCERTAIN"


async def test_budget_expired_never_calls_checkpoint_planner_or_reserves_work():
    from services.api.app.application.h5_adaptive_journey import H5AdaptiveJourneyRunner

    work = replace(_work(), deadline=datetime.now(UTC) - timedelta(seconds=1))
    repository = SimpleNamespace(
        list_plans=AsyncMock(return_value=(run_plan(work),)),
        next_checkpoint=AsyncMock(),
        reserve=AsyncMock(),
    )
    planner = AsyncMock()
    runner = H5AdaptiveJourneyRunner(repository=repository, planner=planner)
    assert await runner.prepare(work=work, agent_run_id="run") == work
    planner.assert_not_awaited()
    repository.next_checkpoint.assert_not_awaited()


async def test_pause_before_after_pair_cannot_prove_time_stopped():
    from services.api.app.application.h5_checkpoint_observation import (
        H5CheckpointObserver,
        checkpoint_facts,
    )

    work = _work()
    plan = run_plan(work)
    snapshots = [
        dict(
            document={"time_origin_ms": 10},
            elements=[
                dict(
                    tag="video",
                    visible=True,
                    h5_media_instance_id="media-1",
                    media=dict(
                        paused=paused,
                        current_time=5,
                        ready_state=4,
                        video_width=640,
                        video_height=360,
                    ),
                )
            ],
        )
        for paused in (False, True)
    ]
    assert "media_time_stopped" not in checkpoint_facts(snapshots[1])
    observer = H5CheckpointObserver(
        snapshots=SimpleNamespace(read_ui_snapshot=AsyncMock(side_effect=snapshots)),
        plans=SimpleNamespace(get_execution_plan=AsyncMock(return_value=plan)),
    )
    case = evidence_case()
    for manifest in case["manifests"]:
        manifest["quality_status"] = "pass"
        manifest["artifacts"].extend(
            [dict(artifact_type="action_receipt"), dict(artifact_type="media_state")]
        )
    # 单帧数量义务，不靠数量不足掩盖原测量器的错误推断。
    payload = checkpoint_definition()
    payload["checkpoints"][1]["evidence_requirements"][0]["minimum_count"] = 1
    payload["checkpoints"][1]["exit_condition"] = {"observation_complete": True}
    plan = build_run_journey_execution_plan(
        parse_journey_definition(payload),
        task_id=work.task_id,
        agent_run_id="run",
        scenario_key="manual",
        parameters={},
    )
    observer._plans.get_execution_plan.return_value = plan
    assert (
        await observer.result_matches(
            plan_id=plan.execution_plan_id,
            checkpoint_id="paused",
            captures=case["captures"],
            manifests=case["manifests"],
        )
        is False
    )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "media",
        "document",
        "pending",
        "degraded",
        "multiple",
        "foreign_fullscreen",
        "truncated",
    ],
)
async def test_native_fullscreen_observation_requires_exact_instance_document_and_quality(fault):
    from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver

    work = _work()
    payload = checkpoint_definition()
    checkpoint = payload["checkpoints"][0]
    checkpoint.update(
        entry_condition={"player_visible": True},
        expected={"fullscreen_active": True},
        exit_condition={"observation_complete": True},
    )
    checkpoint["evidence_requirements"] = [
        dict(
            role="before_fullscreen", capture_phase="before_action", artifact_types=["screenshot"]
        ),
        dict(role="after_fullscreen", artifact_types=["screenshot", "action_receipt"]),
    ]
    payload["checkpoints"] = [checkpoint]
    plan = build_run_journey_execution_plan(
        parse_journey_definition(payload),
        task_id=work.task_id,
        agent_run_id="run",
        scenario_key="manual",
        parameters={},
    )
    before = dict(
        url="https://example.test/video/1",
        document={"time_origin_ms": 10},
        document_state={"fullscreen_active": False},
        elements=[
            dict(
                tag="video",
                visible=True,
                h5_media_instance_id="media-1",
                media=dict(
                    paused=False, current_time=5, ready_state=4, video_width=640, video_height=360
                ),
            )
        ],
    )
    after = deepcopy(before)
    after["document_state"]["fullscreen_active"] = True
    after["document_state"]["fullscreen_tag"] = "video"
    after["document_state"]["fullscreen_media_instance_id"] = "media-1"
    after["document_state"]["fullscreen_surface_kind"] = "native_video"
    case = evidence_case()
    for manifest in case["manifests"]:
        manifest["quality_status"] = "pass"
    if fault == "media":
        after["elements"][0]["h5_media_instance_id"] = "different"
    elif fault == "document":
        after["document"]["time_origin_ms"] = 20
    elif fault == "pending":
        case["manifests"][1]["quality_status"] = "pending"
    elif fault == "degraded":
        after["capture_degraded"] = True
    elif fault == "multiple":
        before["elements"].append(deepcopy(before["elements"][0]))
    elif fault == "foreign_fullscreen":
        after["document_state"]["fullscreen_tag"] = "img"
    elif fault == "truncated":
        after["element_limit_reached"] = True
    observer = H5CheckpointObserver(
        snapshots=SimpleNamespace(read_ui_snapshot=AsyncMock(side_effect=[before, after])),
        plans=SimpleNamespace(get_execution_plan=AsyncMock(return_value=plan)),
    )
    assert await observer.result_matches(
        plan_id=plan.execution_plan_id,
        checkpoint_id="ready",
        captures=case["captures"],
        manifests=case["manifests"],
    ) is (fault is None)


async def test_recharge_without_readonly_contract_remains_blocked_before_snapshot_or_planning():
    from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver

    checkpoint = parse_journey_definition(checkpoint_definition("payment")).checkpoints[0]
    reader = SimpleNamespace(read_ui_snapshot=AsyncMock())
    observer = H5CheckpointObserver(snapshots=reader, plans=None)
    work = selected_work()
    assert await observer.entry_matches(work=work, checkpoint=checkpoint) is False
    reader.read_ui_snapshot.assert_not_awaited()


@pytest.mark.parametrize("fault", ["temporal", "stable_frame", "multiple_frames", "surface"])
async def test_unimplemented_observation_contract_does_not_spend_goal_planning_budget(fault):
    from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver

    payload = checkpoint_definition()
    checkpoint = payload["checkpoints"][0]
    checkpoint.update(
        entry_condition={"player_visible": True},
        expected={"paused": True},
        exit_condition={"observation_complete": True},
    )
    checkpoint["evidence_requirements"] = [dict(role="after", artifact_types=["screenshot"])]
    if fault == "temporal":
        checkpoint["expected"]["media_time_accelerated"] = True
    elif fault == "stable_frame":
        checkpoint["exit_condition"] = {"stable_body_layout": True}
    elif fault == "multiple_frames":
        checkpoint["evidence_requirements"][0]["minimum_count"] = 3
    else:
        checkpoint["entry_condition"] = {"surface": "video_detail"}
    payload["checkpoints"] = [checkpoint]
    checkpoint = parse_journey_definition(payload).checkpoints[0]
    reader = SimpleNamespace(read_ui_snapshot=AsyncMock(return_value={}))
    observer = H5CheckpointObserver(snapshots=reader, plans=None)
    assert await observer.entry_matches(work=selected_work(), checkpoint=checkpoint) is False
    reader.read_ui_snapshot.assert_not_awaited()


def selected_work():
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot

    work = _work()
    observation = replace(work.frontier.observation, page_state="stable")
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=observation.state_version,
        source_location=observation.location,
        navigation_depth=0,
        parent_state_version=None,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=observation), snapshot=snapshot
    )
    return replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)


async def test_only_candidate_is_not_automatically_related_to_checkpoint_goal():
    from services.api.app.application.h5_checkpoint_planning import H5CheckpointPlanner
    from services.api.app.domain.exploration import ExplorationTerminal, ExplorationTerminalStatus

    work = selected_work()
    plan = run_plan(work)
    delegate = SimpleNamespace(
        decide_next=AsyncMock(
            return_value=ExplorationTerminal(
                task_id=work.task_id,
                work_id=work.work_id,
                status=ExplorationTerminalStatus.BLOCKED,
                reason="我的入口不能暂停视频",
            )
        )
    )
    planner = H5CheckpointPlanner(delegate_factory=lambda: delegate)
    assert await planner.prepare(work=work, plan=plan, checkpoint=plan.checkpoints[1]) is None
    delegate.decide_next.assert_awaited_once()


async def test_real_turn_runner_invokes_adaptive_inside_owned_journey_and_existing_executor():
    from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
    from services.api.app.application.h5_exploration_turn import H5ExplorationTurnRunner
    from services.api.app.application.h5_session_journey import H5SessionJourneyGate
    from services.api.tests.test_h5_exploration_turn import (
        _Continuation,
        _Executor,
        _Queue,
        _terminal_turn,
    )

    work = _work()
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    events = []

    class Adaptive:
        async def prepare(self, *, work, agent_run_id):
            assert gate._active
            events.append("prepare")
            return work

        async def execute(self, *, work, agent_run_id, executor):
            assert gate._active
            events.append("execute")
            return await executor.execute(work=work, agent_run_id=agent_run_id)

    runner = H5ExplorationTurnRunner(
        queue=_Queue(work),
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="next")),
        lease_owner="adaptive-test",
        adaptive_journey=Adaptive(),
        session_command_gate=gate,
    )
    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-h5")
    assert events == ["prepare", "execute"]
    assert outcome.turn.is_terminal
    assert not gate._active
