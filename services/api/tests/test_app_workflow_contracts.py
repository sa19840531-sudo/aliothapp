"""可恢复 App Workflow 的版本化领域合同测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain import interaction as interaction_domain


def _now() -> datetime:
    return datetime(2026, 8, 4, 8, 0, tzinfo=UTC)


def _ready_snapshot() -> interaction_domain.AppBootstrapSnapshot:
    return interaction_domain.AppBootstrapSnapshot(
        task_id="task-1",
        session_id="session-1",
        platform_build_ref="app-build:build-1",
        stage=interaction_domain.AppBootstrapStage.APP_READY,
        install_request_id="install-1",
        install_attempt=1,
        installed_package="com.example.target",
        open_request_id="open-1",
        open_attempt=1,
        bootstrap_capture_id="capture-1",
        row_version=6,
        started_at=_now(),
        ready_at=_now(),
        updated_at=_now(),
    )


def test_bootstrap_stage_values_are_stable_and_ordered() -> None:
    assert [stage.value for stage in interaction_domain.AppBootstrapStage] == [
        "validating",
        "acquiring_session",
        "resolving_build",
        "installing",
        "installed",
        "opening",
        "opened",
        "bootstrap_capturing",
        "app_ready",
        "failed",
        "canceled",
    ]
    assert interaction_domain.AppBootstrapStage.APP_READY.is_terminal is True
    assert interaction_domain.AppBootstrapStage.INSTALLING.is_terminal is False


def test_ready_bootstrap_snapshot_round_trip_ignores_unknown_fields() -> None:
    snapshot = _ready_snapshot()
    payload = snapshot.to_dict()
    payload["future_field"] = {"ignored": True}

    restored = interaction_domain.AppBootstrapSnapshot.from_dict(payload)

    assert restored == snapshot
    assert payload["protocol_version"] == "app-workflow.v1"


def test_ready_bootstrap_snapshot_requires_capture_and_ready_time() -> None:
    with pytest.raises(
        interaction_domain.InteractionValidationError,
        match="APP_READY",
    ):
        interaction_domain.AppBootstrapSnapshot(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="app-build:build-1",
            stage=interaction_domain.AppBootstrapStage.APP_READY,
            install_attempt=1,
            open_attempt=1,
            row_version=1,
            started_at=_now(),
            updated_at=_now(),
        )


def test_failed_bootstrap_snapshot_requires_error_and_failed_time() -> None:
    with pytest.raises(
        interaction_domain.InteractionValidationError,
        match="FAILED",
    ):
        interaction_domain.AppBootstrapSnapshot(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="app-build:build-1",
            stage=interaction_domain.AppBootstrapStage.FAILED,
            install_attempt=1,
            open_attempt=0,
            row_version=2,
            started_at=_now(),
            updated_at=_now(),
        )


def test_non_failed_bootstrap_snapshot_rejects_error_fields() -> None:
    with pytest.raises(
        interaction_domain.InteractionValidationError,
        match="失败信息",
    ):
        interaction_domain.AppBootstrapSnapshot(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="app-build:build-1",
            stage=interaction_domain.AppBootstrapStage.INSTALLING,
            install_attempt=1,
            open_attempt=0,
            error_code="INSTALL_FAILED",
            row_version=2,
            started_at=_now(),
            updated_at=_now(),
        )


def test_app_ready_handoff_only_serializes_public_references() -> None:
    capture = interaction_domain.CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=interaction_domain.SurfaceKind.ANDROID,
        capture_kind=interaction_domain.CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-1",),
    )
    manifest = interaction_domain.build_android_screenshot_only_manifest(
        adapter_id="fake-app-runtime",
        adapter_version="1.0.0",
        backend="fake",
    )
    handoff = interaction_domain.AppReadyHandoff(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="session-1",
        installed_package="com.example.target",
        capture=capture,
        capability_manifest=manifest,
        bootstrap_row_version=6,
    )

    payload = handoff.to_dict()
    restored = interaction_domain.AppReadyHandoff.from_dict(
        {**payload, "future_field": "ignored"}
    )

    assert restored == handoff
    assert payload["protocol_version"] == "app-ready-handoff.v2"
    assert payload["installed_package"] == "com.example.target"
    assert payload["capture"]["capture_id"] == "capture-1"
    assert "image_bytes" not in payload
    assert "lease_token" not in payload
    assert "driver_handle" not in payload


def test_app_ready_handoff_reads_legacy_payload_but_new_payload_requires_package() -> None:
    capture = interaction_domain.CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=interaction_domain.SurfaceKind.ANDROID,
        capture_kind=interaction_domain.CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-1",),
    )
    manifest = interaction_domain.build_android_screenshot_only_manifest(
        adapter_id="fake-app-runtime",
        adapter_version="1.0.0",
        backend="fake",
    )
    legacy = interaction_domain.AppReadyHandoff.from_dict(
        {
            "protocol_version": "app-workflow.v1",
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "session_id": "session-1",
            "capture": capture.to_dict(),
            "capability_manifest": manifest.to_dict(),
            "bootstrap_row_version": 6,
        }
    )

    assert legacy.installed_package is None
    with pytest.raises(
        interaction_domain.InteractionValidationError,
        match="installed_package",
    ):
        interaction_domain.AppReadyHandoff(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="session-1",
            capture=capture,
            capability_manifest=manifest,
            bootstrap_row_version=6,
        )


def test_app_workflow_contract_rejects_old_protocol_version() -> None:
    payload = _ready_snapshot().to_dict()
    payload["protocol_version"] = "app-workflow.v0"

    with pytest.raises(
        interaction_domain.InteractionValidationError,
        match="protocol_version",
    ):
        interaction_domain.AppBootstrapSnapshot.from_dict(payload)


def test_app_workflow_failure_has_stable_error_metadata() -> None:
    failure = interaction_domain.AppWorkflowFailure(
        "设备正在忙",
        stage=interaction_domain.AppBootstrapStage.ACQUIRING_SESSION,
        error_code="DEVICE_BUSY",
        retryable=True,
    )

    assert str(failure) == "设备正在忙"
    assert failure.stage is interaction_domain.AppBootstrapStage.ACQUIRING_SESSION
    assert failure.error_code == "DEVICE_BUSY"
    assert failure.retryable is True
