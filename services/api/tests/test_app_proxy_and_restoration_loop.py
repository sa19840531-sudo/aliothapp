"""真代理 network_trace + App Probe 恢复/等价挂回闭环。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_network_proxy import (
    AppNetworkProxySettings,
    get_app_network_proxy_hub,
    reset_app_network_proxy_hub_for_tests,
)
from services.api.app.application.app_probe_hangback import (
    _resolve_session_ref,
    finalize_probe_equivalence_hangback,
)
from services.api.app.application.app_restoration_executor import (
    AppRestorationExecutor,
    probe_result_to_snapshot,
    snapshot_from_target_invariants,
)
from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import (
    CommandPolicy,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_probe_hangback_reuses_canonical_app_surface_identity() -> None:
    reference = _resolve_session_ref(
        runtime=object(),
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
    )

    assert reference is not None
    assert reference.primary_surface_ref == app_primary_surface_ref("session-1")


def _evidence_request(
    *,
    replay=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
    required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
    session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
):
    return build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采 App 证据",
        required_artifact_types=("screenshot", "ui_snapshot"),
        session_dependency=session_dependency,
        replay_policy=replay,
        required_equivalence=required_equivalence,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {
                "surface_kind": "android",
                "deep_link": "app://home",
                "lifecycle_phase": "foreground",
            },
            "business": {
                "package_name": "com.example.app",
                "activity": ".Main",
                "route": "app://com.example.app/.Main",
            },
            "semantic": {"page_state": "stable", "overlay": "none"},
            "structure": {
                "screen_id": "screen:abc123",
                "state_version": "v1",
                "structure_signature": "sha256:struct",
            },
        },
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_build_app_temporal_trace_requires_monotonic_offsets() -> None:
    from services.api.app.application.app_surface_evidence import (
        build_app_temporal_trace,
        resolve_app_timed_capture_params,
    )

    assert resolve_app_timed_capture_params(
        profile_id="app-click-v1",
        requested_artifact_types={"screenshot"},
    ) is None
    assert resolve_app_timed_capture_params(
        profile_id="app-observation-timed_capture-v1",
        requested_artifact_types={"temporal_trace"},
    ) == (3, 1000)
    assert resolve_app_timed_capture_params(
        profile_id="app-observation-startup-timed_capture-v1",
        requested_artifact_types={"temporal_trace"},
    ) == (12, 1000)
    payload = build_app_temporal_trace(
        samples=[
            {"offset_ms": 0, "visible_text": "3秒后跳过", "image_sha256": "sha256:a"},
            {"offset_ms": 1000.5, "visible_text": "2秒后跳过", "image_sha256": "sha256:b"},
            {"offset_ms": 2001, "visible_text": "1秒后跳过", "image_sha256": "sha256:c"},
        ],
        requested_interval_ms=1000,
        package_name="com.example",
    )
    assert payload["clock"] == "monotonic"
    assert len(payload["samples"]) == 3
    try:
        build_app_temporal_trace(
            samples=[
                {"offset_ms": 0, "visible_text": "a"},
                {"offset_ms": 0, "visible_text": "b"},
            ],
            requested_interval_ms=1000,
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "严格递增" in str(exc)


def test_project_har_from_session_proxy_network_trace_includes_statuses() -> None:
    from services.api.app.application.app_surface_evidence import (
        bound_network_trace_payload,
        project_har_from_network_trace,
    )
    from services.api.app.application.rule_policy_evaluators import evaluate_rate_limit

    trace = bound_network_trace_payload(
        entries=[
            {
                "method": "POST",
                "url_host": "api.example",
                "path": "/comment",
                "status_code": 429,
                "bytes": 8,
            }
        ],
        package_name="com.example.app",
        proxy_attached=True,
        status="captured",
        reason="session_proxy",
    )
    har = project_har_from_network_trace(trace)
    assert har["format"] == "network_events"
    assert har["proxy_attached"] is True
    assert any(
        item.get("event") == "response" and item.get("status") == 429
        for item in har["entries"]
    )
    dumpsys = bound_network_trace_payload(
        package_name="com.example.app",
        proxy_attached=False,
        raw_summary="NetworkInfo connectivity",
        status="captured",
        reason="dumpsys_network_summary",
    )
    empty_har = project_har_from_network_trace(dumpsys)
    assert empty_har["format"] == "network_events"
    assert empty_har["entries"] == []
    assert empty_har["proxy_attached"] is False

    action = [{"action": "submit_probe", "text_sha256": "abc"}]
    roles = {
        "attempt_1": {
            "visible_text": "评论1",
            "action_trace": action,
            "network_trace": trace,
        },
        "attempt_2": {
            "visible_text": "评论2",
            "action_trace": action,
            "har": har,
        },
        "attempt_3": {
            "visible_text": "评论3 操作过于频繁",
            "action_trace": action,
            "har": {"format": "network_events", "entries": []},
        },
        "primary": {
            "visible_text": "评论4 操作过于频繁",
            "action_trace": action,
            "har": har,
        },
    }
    # 判定侧缺 har 时由投影补齐（attempt_1）
    from services.api.app.application.rule_judge import _fill_har_from_app_network_trace

    filled = _fill_har_from_app_network_trace(roles)
    assert filled["attempt_1"]["har"]["format"] == "network_events"
    result = evaluate_rate_limit(
        evidence_by_role=filled,
        config={
            "min_attempts": 4,
            "primary_attempt_index": 4,
            "rate_limit_text_pattern": "操作过于频繁",
            "rate_limit_statuses": [429],
            "require_explicit_challenge": False,
        },
    )
    assert result.passed is True


def test_network_proxy_hub_drains_session_proxy_entries() -> None:
    reset_app_network_proxy_hub_for_tests()
    hub = get_app_network_proxy_hub()
    hub.configure(
        AppNetworkProxySettings(
            enabled=True,
            listen_host="127.0.0.1",
            listen_port=18901,
            advertise_host="10.0.2.2",
        )
    )
    hub.bind_session("session-proxy-1", endpoint_ref="10.0.2.2:18901")
    hub.record_entry(
        "session-proxy-1",
        {
            "method": "GET",
            "url_host": "api.example",
            "path": "/v1/ping",
            "status_code": 200,
            "bytes": 12,
        },
    )
    payload = hub.build_network_trace(
        session_id="session-proxy-1",
        package_name="com.example.app",
    )
    assert payload["proxy_attached"] is True
    assert payload["source"] == "session_proxy"
    assert payload["entry_count"] == 1
    assert payload["status"] == "captured"
    assert payload["proxy_endpoint_ref"] == "10.0.2.2:18901"
    # 再次 drain 应为空
    empty = hub.build_network_trace(session_id="session-proxy-1")
    assert empty["status"] == "empty"
    reset_app_network_proxy_hub_for_tests()


@pytest.mark.asyncio
async def test_app_restoration_pin_passes_equivalence_gate() -> None:
    request = _evidence_request()
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=10),
    )
    original = snapshot_from_target_invariants(
        request=request,
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        captured_at=NOW,
    )
    # 恢复快照必须独立观察出与目标一致的字段，禁止 merge target_invariants。
    restored = probe_result_to_snapshot(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-probe",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example.app",
        activity=".Main",
        screen_id="screen:abc123",
        structure_signature="sha256:struct",
        image_signature="v1",
        deep_link="app://home",
        page_state="stable",
        lifecycle_phase="foreground",
        captured_at=NOW + timedelta(seconds=2),
    )
    executor = AppRestorationExecutor(
        driver=None,
        session=None,
        package_name="com.example.app",
        probe_result_snapshot=restored,
    )
    result = await executor.restore(
        __import__(
            "services.api.app.application.restoration",
            fromlist=["RestorationExecutionRequest"],
        ).RestorationExecutionRequest(
            request=request,
            plan=plan,
            restoration_attempt_id="attempt-1",
        )
    )
    assert result.restored_snapshot.capture_id == "capture-probe"
    gate = StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=3))
    assessment = gate.assess(
        request=request,
        plan=plan,
        restoration_attempt_id="attempt-1",
        original=original,
        restored=result.restored_snapshot,
    )
    assert assessment.passed is True


@pytest.mark.asyncio
async def test_app_restoration_pin_rejects_target_only_self_proof() -> None:
    """目标值未出现在 restored 时，不得仅因 target 通过等价门禁。"""
    request = _evidence_request()
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=10),
    )
    original = snapshot_from_target_invariants(
        request=request,
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        captured_at=NOW,
    )
    # 仅有包名／Activity，缺少 structure 独立观察字段。
    restored = probe_result_to_snapshot(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-probe-incomplete",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example.app",
        activity=".Main",
        deep_link="app://home",
        page_state="stable",
        lifecycle_phase="foreground",
        captured_at=NOW + timedelta(seconds=2),
    )
    gate = StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=3))
    assessment = gate.assess(
        request=request,
        plan=plan,
        restoration_attempt_id="attempt-incomplete",
        original=original,
        restored=restored,
    )
    assert assessment.passed is False
    missing = {
        item.key
        for item in assessment.comparisons
        if not item.matched and item.dimension.value == "structure"
    }
    assert "screen_id" in missing or "structure_signature" in missing


@pytest.mark.asyncio
async def test_probe_hangback_runs_coordinator_then_satisfies() -> None:
    request = _evidence_request()
    stored = SimpleNamespace(
        request=request,
        status=EvidenceRequestStatus.RUNNING,
        row_version=3,
    )
    transitions: list = []

    class FakeRequestStore:
        async def get(self, *, request_id: str):
            del request_id
            return stored

        async def transition(self, *, command):
            transitions.append(command)
            stored.row_version = command.row_version + 1
            stored.status = command.target_status
            return stored.row_version

    class FakeManifests:
        async def get_latest_for_capture(self, **kwargs):
            del kwargs
            return SimpleNamespace(manifest_id="manifest-pass-1")

    await finalize_probe_equivalence_hangback(
        task_id="task-1",
        session_id="session-1",
        outcome=SimpleNamespace(
            claimed=True,
            work_type="probe",
            evidence_request_id=request.request_id,
            result_capture_id="capture-probe",
            # 独立观察字段；不得依赖 hangback 从 target 填空。
            screen_id="screen:abc123",
            structure_signature="sha256:struct",
            state_version="v1",
            page_state="stable",
            lifecycle_phase="foreground",
        ),
        request_store=FakeRequestStore(),
        manifest_store=FakeManifests(),
        restoration_repository=__import__(
            "services.api.app.application.app_probe_hangback",
            fromlist=["_MemoryRestorationAttemptRepository"],
        )._MemoryRestorationAttemptRepository(),
        package_name="com.example.app",
        activity=".Main",
        deep_link="app://home",
    )
    assert transitions
    assert transitions[-1].target_status is EvidenceRequestStatus.SATISFIED
    assert transitions[-1].resolution_manifest_id == "manifest-pass-1"
    assert "restoration_equivalent:" in (transitions[-1].resolution_reason or "")


@pytest.mark.asyncio
async def test_probe_hangback_does_not_satisfy_without_independent_structure() -> None:
    request = _evidence_request()
    stored = SimpleNamespace(
        request=request,
        status=EvidenceRequestStatus.RUNNING,
        row_version=3,
    )
    transitions: list = []

    class FakeRequestStore:
        async def get(self, *, request_id: str):
            del request_id
            return stored

        async def transition(self, *, command):
            transitions.append(command)
            stored.row_version = command.row_version + 1
            stored.status = command.target_status
            return stored.row_version

    class FakeManifests:
        async def get_latest_for_capture(self, **kwargs):
            del kwargs
            return SimpleNamespace(manifest_id="manifest-pass-1")

    await finalize_probe_equivalence_hangback(
        task_id="task-1",
        session_id="session-1",
        outcome=SimpleNamespace(
            claimed=True,
            work_type="probe",
            evidence_request_id=request.request_id,
            result_capture_id="capture-probe-bare",
        ),
        request_store=FakeRequestStore(),
        manifest_store=FakeManifests(),
        restoration_repository=__import__(
            "services.api.app.application.app_probe_hangback",
            fromlist=["_MemoryRestorationAttemptRepository"],
        )._MemoryRestorationAttemptRepository(),
        package_name="com.example.app",
        activity=".Main",
        deep_link="app://home",
    )
    # DIVERGENT 时保留 RUNNING、不写终态；不得因缺独立观察却 SATISFIED。
    assert stored.status is EvidenceRequestStatus.RUNNING
    assert not any(
        command.target_status is EvidenceRequestStatus.SATISFIED for command in transitions
    )


@pytest.mark.asyncio
async def test_same_app_session_executor_rewrites_capture_identity() -> None:
    request = _evidence_request(
        replay=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
    )
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        replay_execution_plan_id="replay-plan:1",
    )
    base = probe_result_to_snapshot(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-probe",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example.app",
        captured_at=datetime.now(UTC),
    )
    executor = AppRestorationExecutor(
        driver=None,
        session=None,
        package_name="com.example.app",
        deep_link="app://home",
        probe_result_snapshot=base,
    )
    from services.api.app.application.restoration import RestorationExecutionRequest

    result = await executor.restore(
        RestorationExecutionRequest(
            request=request,
            plan=plan,
            restoration_attempt_id="attempt-same",
        )
    )
    assert result.restored_snapshot.capture_id != "capture-probe"
    assert result.restored_snapshot.session_id == "session-1"


def test_session_event_detection_permission_and_process_death() -> None:
    from services.api.app.application.app_session_events import detect_app_session_events

    permission = detect_app_session_events(
        expected_package="com.example.app",
        focus_evidence_lines=(
            "mResumedActivity: ActivityRecord{x u0 "
            "com.android.permissioncontroller/.permission.ui.GrantPermissionsActivity t1}",
        ),
    )
    assert permission.session_event == "permission_dialog"
    assert permission.page_state_hint == "system_dialog"

    death = detect_app_session_events(
        expected_package="com.example.app",
        focus_evidence_lines=(),
        logcat_text=(
            "FATAL EXCEPTION: main\n"
            "Process: com.example.app, PID: 123\n"
            "Process com.example.app (123) has died"
        ),
    )
    assert death.session_event == "process_death"
    assert death.process_alive is False


@pytest.mark.asyncio
async def test_new_session_replay_calls_start_session() -> None:
    request = _evidence_request(
        replay=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
    )
    target = f"session-replay:{request.request_id}"
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id=target,
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.NEW_SESSION_REPLAY,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        replay_execution_plan_id="replay-plan:new",
    )
    started: list[str] = []

    class _Driver(FakeAppRuntime):
        def __init__(self) -> None:
            super().__init__()

        async def start_session(self, spec):
            started.append(spec.session_id)
            return await super().start_session(spec)

        async def reset_app(self, _request):
            return SimpleNamespace(succeeded=True, error_code=None)

        async def execute(self, **kwargs):
            return SimpleNamespace(succeeded=True, error_code=None)

    from services.api.app.domain.interaction import SurfaceSessionRef

    session = SurfaceSessionRef(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="app-surface:session-1",
    )
    base = probe_result_to_snapshot(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-probe",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example.app",
        activity=".Main",
        screen_id="screen:abc123",
        structure_signature="sha256:struct",
        image_signature="v1",
        deep_link="app://home",
        captured_at=datetime.now(UTC),
    )
    executor = AppRestorationExecutor(
        driver=_Driver(),
        session=session,
        package_name="com.example.app",
        activity=".Main",
        deep_link="app://home",
        probe_result_snapshot=base,
        platform_build_ref="build:1",
        session_options={"platform_build_ref": "build:1"},
        command_validator=InteractionCommandValidator(
            policy=CommandPolicy(
                allowed_risk_levels=(
                    InteractionRiskLevel.LOW,
                    InteractionRiskLevel.MEDIUM,
                    InteractionRiskLevel.HIGH,
                ),
                approved_high_risk_intent_ids=("*",),
            )
        ),
        # 该单测保留旧的 Fake Driver 直接 start/execute 合同；正式入口不传此开关。
        legacy_driver_fallback=True,
    )
    from services.api.app.application.restoration import RestorationExecutionRequest

    result = await executor.restore(
        RestorationExecutionRequest(
            request=request,
            plan=plan,
            restoration_attempt_id="attempt-new",
        )
    )
    assert started == [target]
    assert result.restored_snapshot.session_id == target
    assert any("start_session" in item for item in result.action_execution_ids)


@pytest.mark.asyncio
async def test_direct_app_restore_primitive_requires_command_scope_before_driver() -> None:
    request = _evidence_request(
        replay=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
    )
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        replay_execution_plan_id="replay-plan:primitive-scope",
    )

    class _Driver(FakeAppRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        async def warm_restore(self, _request):
            self.calls.append("warm_restore")
            return SimpleNamespace(succeeded=True, error_code=None)

    from services.api.app.application.restoration import (
        RestorationExecutionError,
        RestorationExecutionRequest,
    )
    from services.api.app.domain.interaction import SurfaceSessionRef

    driver = _Driver()
    session = SurfaceSessionRef(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="app-surface:session-1",
    )
    snapshot = probe_result_to_snapshot(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-probe",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example.app",
        activity=".Main",
    )
    executor = AppRestorationExecutor(
        driver=driver,
        session=session,
        package_name="com.example.app",
        activity=".Main",
        deep_link="app://home",
        probe_result_snapshot=snapshot,
        # 该单测验证旧直驱路径上的命令范围门禁；正式入口不传此开关。
        legacy_driver_fallback=True,
    )

    with pytest.raises(RestorationExecutionError) as error:
        await executor.restore(
            RestorationExecutionRequest(
                request=request,
                plan=plan,
                restoration_attempt_id="attempt-primitive-scope",
            )
        )

    assert error.value.error_code == "COMMAND_SCOPE_REQUIRED"
    assert driver.calls == []


def test_mitm_authority_issues_leaf_cert(tmp_path) -> None:
    from services.api.app.application.app_network_mitm_certs import (
        AppNetworkMitmAuthority,
        cryptography_available,
    )

    if not cryptography_available():
        pytest.skip("cryptography 不可用")
    authority = AppNetworkMitmAuthority(storage_dir=tmp_path)
    assert b"BEGIN CERTIFICATE" in authority.ca_cert_pem
    ctx = authority.ssl_context_for_host("api.example.test")
    assert ctx is not None
    settings = AppNetworkProxySettings(
        enabled=True,
        listen_host="127.0.0.1",
        listen_port=18999,
        mitm_enabled=True,
        mitm_ca_dir=str(tmp_path),
    )
    assert settings.mitm_enabled is True
