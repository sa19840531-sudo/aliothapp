"""流程基建 runtime 接线：定义注册、TimedObservation v3、内存路径 claim＋事件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.api.app.application.app_screenshot_exploration import (
    _scheduler_monotonic_observed_at,
)
from services.api.app.application.capture_coordinator import (
    CaptureCoordinator,
    _try_build_device_execution_permit,
)
from services.api.app.application.flow_foundation_observation_sequence import (
    late_fill_observation_sequence,
    seal_observation_sequence,
)
from services.api.app.application.flow_foundation_p1 import FlowFoundationP1Service
from services.api.app.application.flow_foundation_parent_hold import (
    ParentHoldGate,
    ParentHoldGateError,
)
from services.api.app.application.flow_foundation_runtime import (
    AppFlowFoundationRuntime,
    build_app_flow_foundation_runtime,
)
from services.api.app.application.page_capture_persistence import (
    _should_retry_without_surface_binding,
)
from services.api.app.domain.exploration.flow_foundation import (
    DeviceExecutionPermit,
    FlowFoundationContractError,
)
from services.api.app.domain.exploration.flow_foundation_definitions import (
    APP_DETAIL_PURE_OBSERVATION,
    APP_DETAIL_PURE_OBSERVATION_CODE,
    APP_LOW_RISK_EXPAND_CLOSE_RETURN,
    APP_LOW_RISK_EXPAND_CLOSE_RETURN_CODE,
    get_flow_definition,
    list_app_flow_definitions,
)
from services.api.app.domain.exploration.flow_foundation_evidence import (
    ObservationClockDomain,
    ObservationSequenceDraft,
    SampleFrameRef,
    TimedObservationProtocolVersion,
    assert_timed_observation_v3_enabled,
)
from services.api.app.domain.interaction import CaptureDirective, SurfaceKind
from services.api.app.domain.interaction.timed_observation import (
    TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3,
    build_timed_observation_intent_v3,
)


def test_flow_definitions_registry_frozen_hashes() -> None:
    definitions = list_app_flow_definitions()
    assert {item.code for item in definitions} == {
        APP_DETAIL_PURE_OBSERVATION_CODE,
        APP_LOW_RISK_EXPAND_CLOSE_RETURN_CODE,
    }
    detail = get_flow_definition(APP_DETAIL_PURE_OBSERVATION_CODE)
    assert detail.definition_hash == APP_DETAIL_PURE_OBSERVATION.definition_hash
    assert detail.forbid_playback_proof is True
    assert detail.forbid_clear_data is True
    assert detail.forbid_payment is True
    low = get_flow_definition(APP_LOW_RISK_EXPAND_CLOSE_RETURN_CODE)
    assert low.definition_hash == APP_LOW_RISK_EXPAND_CLOSE_RETURN.definition_hash
    assert "warm_restore" in low.capability_requirements


def test_timed_observation_v3_gate_and_factory() -> None:
    with pytest.raises(FlowFoundationContractError, match="均声明支持"):
        assert_timed_observation_v3_enabled(
            writer_supports_v3=True,
            reader_supports_v3=False,
            scheduler_supports_v3=True,
            adapter_supports_v3=True,
            requested_version=TimedObservationProtocolVersion.V3,
        )
    deadline = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    intent = build_timed_observation_intent_v3(
        task_id="task-1",
        session_id="session-1",
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        delay_ms=500,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot",),
        ),
        deadline=deadline,
        writer_supports_v3=True,
        reader_supports_v3=True,
        scheduler_supports_v3=True,
        adapter_supports_v3=True,
        phase_key="detail_observe",
        context_ref="ctx:detail",
        policy_ref="policy:app-observe",
        capability_ref="cap:device_screenshot",
    )
    assert intent.protocol_version == TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3
    assert intent.context_ref == "ctx:detail"
    payload = intent.to_dict()
    assert payload["context_ref"] == "ctx:detail"
    assert payload["policy_ref"] == "policy:app-observe"
    restored = type(intent).from_dict(payload)
    assert restored.capability_ref == "cap:device_screenshot"
    with pytest.raises(Exception, match="均声明支持|TimedObservation"):
        build_timed_observation_intent_v3(
            task_id="task-1",
            session_id="session-1",
            phase_run_id="phase-1",
            decision_id="decision-1",
            observation_id="observation-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.ANDROID,
            delay_ms=0,
            directive=CaptureDirective(
                role="observation",
                artifact_types=("screenshot",),
            ),
            deadline=deadline + timedelta(seconds=1),
            writer_supports_v3=True,
            reader_supports_v3=True,
            scheduler_supports_v3=False,
            adapter_supports_v3=True,
        )


@pytest.mark.asyncio
async def test_runtime_memory_claim_and_capture_event_without_pg() -> None:
    runtime = build_app_flow_foundation_runtime(repo=None, enabled=True)
    assert isinstance(runtime, AppFlowFoundationRuntime)
    assert runtime.repo is None

    decision = runtime.evaluate_and_maybe_claim_after_observation(
        task_id="task-ff-1",
        exploration_scope_id="scope-1",
        phase_run_id="phase-ff-1",
        parent_work_kind="app_exploration_work",
        parent_work_id="work-1",
        parent_version=0,
        owner_execution_segment_id="seg-1",
        page_type="content_detail",
        subject_ref="subject:video-1",
    )
    assert decision["kind"] == "eligible"
    assert decision["claimed"] is True
    assert decision["flow_code"] == APP_DETAIL_PURE_OBSERVATION_CODE
    flows = await runtime.list_flows_for_task(task_id="task-ff-1")
    assert len(flows) == 1
    assert flows[0]["phase_run_id"] == "phase-ff-1"

    reservation, commitment = await runtime.record_capture_reservation_and_commitment(
        task_id="task-ff-1",
        phase_run_id="phase-ff-1",
        checkpoint_key="detail_observe",
        sample_index=0,
        producer_epoch="epoch-1",
        capture_id="capture-ff-1",
        content_digest="sha256:abc",
        content_length=128,
        fencing_token=3,
        session_id="session-1",
        pure_observation=True,
    )
    assert reservation.capture_id == "capture-ff-1"
    assert commitment.content_digest == "sha256:abc"

    allocation = await runtime.allocate_observation_event(
        task_id="task-ff-1",
        phase_run_id="phase-ff-1",
        resource_id="device:phone-1",
        producer_epoch="epoch-1",
        sample_index=0,
        capture_id="capture-ff-1",
        content_digest="sha256:abc",
    )
    assert allocation.resource_event_seq == 1
    observations = await runtime.list_observations_for_phase(
        task_id="task-ff-1",
        phase_run_id="phase-ff-1",
    )
    assert len(observations) == 1
    links = await runtime.list_flow_links_for_capture(
        task_id="task-ff-1",
        capture_id="capture-ff-1",
    )
    assert len(links) == 1
    assert links[0]["resource_event_seq"] == 1

    # 同触发键再次评估应为 already_claimed／skipped
    again = runtime.evaluate_and_maybe_claim_after_observation(
        task_id="task-ff-1",
        exploration_scope_id="scope-1",
        phase_run_id="phase-ff-2",
        parent_work_kind="app_exploration_work",
        parent_work_id="work-2",
        parent_version=1,
        owner_execution_segment_id="seg-1",
        page_type="content_detail",
        subject_ref="subject:video-1",
    )
    assert again["kind"] == "skipped"
    assert again["claimed"] is False

    # 内存路径 persist_claim_decision 在无 repo 时为 no-op，不抛错
    await runtime.persist_claim_decision(task_id="task-ff-1", decision=decision)
    assert decision["parent_work_id"] == "work-1"


def test_home_page_does_not_claim_short_flow() -> None:
    """首页不得 claim 详情短流程，否则后继 Work 会「短流程禁止递归」。"""

    runtime = build_app_flow_foundation_runtime(repo=None, enabled=True)
    home_decision = runtime.evaluate_and_maybe_claim_after_observation(
        task_id="task-home-1",
        exploration_scope_id="scope-home",
        phase_run_id="phase-home-1",
        parent_work_kind="app_exploration_work",
        parent_work_id="work-home-1",
        parent_version=0,
        owner_execution_segment_id="seg-home",
        page_type="home",
        subject_ref="subject:home",
    )
    assert home_decision["claimed"] is False
    assert home_decision["kind"] == "skipped"
    assert home_decision["reason_code"] == "no_short_flow_for_page"


@pytest.mark.asyncio
async def test_serial_claim_across_successor_works_does_not_raise_nested() -> None:
    """跨后继 Work 串行 claim：须释放旧 hold，不得全局 depth 误杀。"""

    runtime = build_app_flow_foundation_runtime(repo=None, enabled=True)
    first = runtime.evaluate_and_maybe_claim_after_observation(
        task_id="task-serial",
        exploration_scope_id="scope-serial",
        phase_run_id="phase-serial-1",
        parent_work_kind="app_exploration_work",
        parent_work_id="work-serial-1",
        parent_version=0,
        owner_execution_segment_id="seg-1",
        page_type="content_detail",
        subject_ref="subject:a",
    )
    assert first["claimed"] is True
    second = runtime.evaluate_and_maybe_claim_after_observation(
        task_id="task-serial",
        exploration_scope_id="scope-serial",
        phase_run_id="phase-serial-2",
        parent_work_kind="app_exploration_work",
        parent_work_id="work-serial-2",
        parent_version=1,
        owner_execution_segment_id="seg-2",
        page_type="content_detail",
        subject_ref="subject:b",
    )
    assert second["claimed"] is True
    assert second.get("claim_error") is None
    await runtime.assert_parent_claimable("work-serial-1")


@pytest.mark.asyncio
async def test_parent_hold_gate_blocks_second_claimable_check() -> None:
    runtime = build_app_flow_foundation_runtime(repo=None, enabled=True)
    decision = runtime.evaluate_and_maybe_claim_after_observation(
        task_id="task-hold-1",
        exploration_scope_id="scope-hold",
        phase_run_id="phase-hold-1",
        parent_work_kind="app_exploration_work",
        parent_work_id="work-held",
        parent_version=0,
        owner_execution_segment_id="seg-hold",
        page_type="content_detail",
        subject_ref="subject:held",
    )
    assert decision["claimed"] is True
    await runtime.assert_parent_claimable("work-other")
    with pytest.raises(ParentHoldGateError, match="PARENT_WORK_HELD_BY_FLOW|父待办"):
        await runtime.assert_parent_claimable("work-held")
    gate = ParentHoldGate(runtime)
    with pytest.raises(ParentHoldGateError) as exc_info:
        await gate.ensure_claimable("work-held")
    assert exc_info.value.error_code == "PARENT_WORK_HELD_BY_FLOW"
    # runtime None／未启用 = no-op
    await ParentHoldGate(None).ensure_claimable("work-held")
    disabled = build_app_flow_foundation_runtime(repo=None, enabled=False)
    await disabled.assert_parent_claimable("work-held")


def test_strip_binding_forbidden_for_app_surface_session() -> None:
    error = RuntimeError("Surface Session 绑定失配")
    assert (
        _should_retry_without_surface_binding(
            error=error,
            surface_session_id="app:session-1",
            action_execution_id="action-1",
            session_command_sequence=1,
            finalization_epoch=1,
        )
        is False
    )
    assert (
        _should_retry_without_surface_binding(
            error=error,
            surface_session_id="h5:session-1",
            action_execution_id="action-1",
            session_command_sequence=1,
            finalization_epoch=1,
            forbid_strip_binding=True,
        )
        is False
    )
    assert (
        _should_retry_without_surface_binding(
            error=error,
            surface_session_id="h5:session-1",
            action_execution_id="action-1",
            session_command_sequence=1,
            finalization_epoch=1,
        )
        is True
    )


def test_scheduler_monotonic_observed_at_clock_metadata() -> None:
    prior = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
    sample = datetime(2026, 10, 22, 11, 59, 59, tzinfo=UTC)
    bumped, meta = _scheduler_monotonic_observed_at(
        sample_observed_at=sample,
        prior_observed_at=prior,
    )
    assert bumped == prior + timedelta(microseconds=1)
    assert meta["scheduler_synthetic_observed_at"] is True
    assert meta["observed_at_clock_domain"] == (
        ObservationClockDomain.PERSISTED_AT_LEGACY.value
    )
    fresh = prior + timedelta(seconds=2)
    kept, meta_ok = _scheduler_monotonic_observed_at(
        sample_observed_at=fresh,
        prior_observed_at=prior,
    )
    assert kept == fresh
    assert meta_ok["scheduler_synthetic_observed_at"] is False
    assert meta_ok["observed_at_clock_domain"] == (
        ObservationClockDomain.PRODUCER_MONOTONIC.value
    )


def test_observation_sequence_seal_and_runtime_memory_registry() -> None:
    draft = ObservationSequenceDraft(series_id="series-seal-1")
    draft.append_frame(
        SampleFrameRef(
            capture_id="c-seal-1",
            sample_index=0,
            content_digest="digest-a",
        )
    )
    sealed = seal_observation_sequence(draft)
    assert sealed.revision == 1
    assert draft.sealed_revision == 1
    newer = late_fill_observation_sequence(sealed=sealed, late_fill=True)
    assert newer.revision == 2

    runtime = build_app_flow_foundation_runtime(repo=None, enabled=True)
    draft2 = ObservationSequenceDraft(series_id="series-seal-2")
    draft2.append_frame(
        SampleFrameRef(
            capture_id="c-seal-2",
            sample_index=0,
            content_digest="digest-b",
        )
    )
    sealed2 = runtime.seal_series(draft2)
    assert runtime._memory_seals["series-seal-2"].revision == 1
    filled = runtime.late_fill_series(series_id="series-seal-2")
    assert filled.revision == 2
    assert sealed2.revision == 1


def test_fence_gate_builds_permit_from_params_and_require_permit() -> None:
    permit = _try_build_device_execution_permit(
        {
            "fencing_token": "3",
            "device_id": "device-1",
            "resource_id": "device-1",
            "session_id": "session-1",
            "execution_segment_id": "seg-1",
            "task_id": "task-1",
        }
    )
    assert isinstance(permit, DeviceExecutionPermit)
    assert permit.fencing_token == "3"
    assert _try_build_device_execution_permit({"fencing_token": "3"}) is None

    nested = _try_build_device_execution_permit(
        {
            "device_execution_permit": {
                "device_id": "d2",
                "resource_id": "d2",
                "fencing_token": "7",
                "session_id": "s2",
                "execution_segment_id": "seg-2",
                "task_id": "t2",
            },
            "fencing_token": "7",
        }
    )
    assert nested is not None and nested.fencing_token == "7"

    gateway = FlowFoundationP1Service()
    good = DeviceExecutionPermit(
        device_id="device-1",
        resource_id="device-1",
        fencing_token="3",
        session_id="session-1",
        execution_segment_id="seg-1",
        task_id="task-1",
    )
    gateway.require_permit(good, presented_fencing_token=3)
    with pytest.raises(Exception, match="fencing|许可"):
        gateway.require_permit(good, presented_fencing_token=99)

    # CaptureCoordinator 可选门禁：参数齐全时调用 require_permit
    mock_gateway = MagicMock()
    mock_gateway.gate_android_coordination = MagicMock()
    mock_gateway.gate_interaction_operation = MagicMock()
    mock_gateway.require_permit = MagicMock()
    coordinator = CaptureCoordinator(
        interaction_executor=MagicMock(),
        capture_writer=MagicMock(),
        evidence_exit_gate=MagicMock(),
        flow_foundation_p1=mock_gateway,
    )
    intent = SimpleNamespace(
        surface_kind=SurfaceKind.ANDROID,
        action_type=SimpleNamespace(value="tap"),
        parameters={
            "fencing_token": "3",
            "device_id": "device-1",
            "resource_id": "device-1",
            "session_id": "session-1",
            "execution_segment_id": "seg-1",
            "task_id": "task-1",
        },
    )
    command = SimpleNamespace(intent=intent)
    coordinator._apply_flow_foundation_p1_gates(command)
    mock_gateway.require_permit.assert_called_once()


@pytest.mark.asyncio
async def test_recover_then_handoff_uses_build_lease_handoff() -> None:
    """无 DB：校验 P1 build_lease_handoff 仍被 recover_then_handoff 路径依赖。"""

    from services.api.app.application.device_pool import DeviceLease, PostgresDevicePool

    predecessor = DeviceLease(
        lease_id="11111111-1111-1111-1111-111111111111",
        device_ref="device-handoff",
        task_id="22222222-2222-2222-2222-222222222222",
        session_id="session-pred",
        lease_token="33333333-3333-3333-3333-333333333333",
        fencing_token=1,
        state="active",
        claimed_at=datetime(2026, 10, 22, tzinfo=UTC),
        expires_at=datetime(2026, 10, 23, tzinfo=UTC),
        backend="adb",
        profile_id="profile-1",
    )
    p1 = FlowFoundationP1Service()
    handoff = p1.build_lease_handoff(
        predecessor=predecessor,
        successor_session_id="session-succ",
        successor_fencing_token=2,
        predecessor_channel_silenced=True,
    )
    assert handoff.successor_session_id == "session-succ"
    assert handoff.successor_fencing_token == "2"

    recover = AsyncMock(return_value=predecessor)
    handoff = AsyncMock(
        return_value=SimpleNamespace(session_id="session-succ", fencing_token=2)
    )
    # 无 recover_then_handoff 属性 → pool 回退到 recover + handoff
    repo = SimpleNamespace(
        recover=recover,
        handoff_to_successor_session=handoff,
    )
    pool = PostgresDevicePool(repository=repo)
    result = await pool.recover_then_handoff(
        lease=predecessor,
        successor_session_id="session-succ",
        predecessor_channel_silenced=True,
    )
    recover.assert_awaited_once()
    handoff.assert_awaited_once()
    assert result.session_id == "session-succ"
