"""Surface Session、Action Execution 与统一 Driver 合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.api.app.domain.interaction import (
    ActionExecutionSnapshot,
    ActionExecutionStatus,
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionValidationError,
    RawArtifact,
    SurfaceKind,
    SurfaceSessionSnapshot,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
    build_action_execution_id,
    build_action_intent_hash,
)


def _intent(*, task_id: str = "task-1", session_id: str = "session-1") -> ActionIntent:
    return ActionIntent(
        task_id=task_id,
        session_id=session_id,
        intent_id="intent-1",
        idempotency_key="branch-1:intent-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        purpose="进入登录页",
        capture_profile=CaptureProfile(
            profile_id="baseline",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(
                role="after_action",
                artifact_types=("screenshot", "action_trace"),
            ),
        ),
        deadline=datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
    )


def test_surface_session_snapshot_round_trip_excludes_lease_token() -> None:
    snapshot = SurfaceSessionSnapshot(
        session_id="session-1",
        task_id="task-1",
        surface_kind=SurfaceKind.WEB,
        session_owner="interaction-runtime",
        status=SurfaceSessionStatus.ACTIVE,
        command_sequence=3,
        finalization_epoch=1,
        row_version=5,
        account_environment_ref="account-env-1",
        platform_build_ref="web-build-1",
        device_ref=None,
        current_surface_ref="surface-1",
        current_capture_id="capture-1",
        started_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, 10, 1, tzinfo=UTC),
    )

    payload = snapshot.to_dict()

    assert "lease_token" not in payload
    assert "row_version" not in payload
    assert SurfaceSessionSnapshot.from_dict(payload) == snapshot
    with pytest.raises(ValueError, match="运行时句柄"):
        SurfaceSessionSnapshot.from_dict({**payload, "lease_token": "secret"})


def test_surface_session_spec_rejects_driver_and_raw_credentials() -> None:
    with pytest.raises(InteractionValidationError, match="禁止字段"):
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            options={"driver": "appium"},
        )
    with pytest.raises(InteractionValidationError, match="禁止字段"):
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.IOS,
            options={"password": "raw-secret"},
        )


def test_raw_artifact_accepts_immutable_binary_without_exposing_handles() -> None:
    source = bytearray(b"png")

    artifact = RawArtifact(artifact_type="screenshot", content=source)
    source[0] = ord("x")

    assert artifact.content == b"png"
    with pytest.raises(ValueError, match="运行时句柄"):
        RawArtifact(artifact_type="ui_snapshot", content={"driver": "appium"})


def test_action_execution_identity_is_scoped_to_task_and_session() -> None:
    base = _intent()
    other_task = _intent(task_id="task-2")
    other_session = _intent(session_id="session-2")

    assert build_action_execution_id(base) == build_action_execution_id(base)
    assert build_action_execution_id(base) != build_action_execution_id(other_task)
    assert build_action_execution_id(base) != build_action_execution_id(other_session)
    assert build_action_intent_hash(base) != build_action_intent_hash(other_task)


def test_action_execution_distinguishes_action_and_evidence_completion() -> None:
    intent = _intent()
    pending = ActionExecutionSnapshot(
        action_execution_id=build_action_execution_id(intent),
        task_id=intent.task_id,
        session_id=intent.session_id,
        intent_id=intent.intent_id,
        intent_hash=build_action_intent_hash(intent),
        command_sequence=1,
        finalization_epoch=0,
        requested_source_capture_id=intent.source_capture_id,
        source_capture_id="capture-before",
        status=ActionExecutionStatus.EVIDENCE_PENDING,
        row_version=2,
        started_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        action_finished_at=datetime(2026, 8, 3, 10, 0, 1, tzinfo=UTC),
    )

    assert not pending.status.is_terminal
    assert ActionExecutionSnapshot.from_dict(pending.to_dict()) == pending
    with pytest.raises(InteractionValidationError, match="结果 Capture"):
        ActionExecutionSnapshot(
            **{
                **pending.__dict__,
                "status": ActionExecutionStatus.COMPLETED,
                "evidence_finished_at": datetime(2026, 8, 3, 10, 0, 2, tzinfo=UTC),
            }
        )
