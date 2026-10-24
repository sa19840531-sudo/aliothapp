"""附件审查整改的基础协议与质量门禁。"""

import tomllib
from pathlib import Path

import pytest

from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.core.config import Settings
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_mobile_demo_is_disabled_by_default() -> None:
    """默认配置不能把 Demo 误当成正式 API 能力。"""
    settings = Settings.model_validate({})

    assert settings.workflow.mobile_demo_enabled is False


def test_public_api_router_does_not_mount_mobile_demo_by_default() -> None:
    """默认 API 路由表不能包含未授权的移动 Demo。"""
    source = (REPOSITORY_ROOT / "services/api/app/api/v1/router.py").read_text(
        encoding="utf-8"
    )

    assert "api_router.include_router(mobile_demo_router" not in source


def test_current_android_capture_consumer_uses_common_capture_id() -> None:
    """CaptureRef 重命名必须覆盖 Agent 运行入口。"""
    path = REPOSITORY_ROOT / "services/api/app/api/v1/mobile_demo_agent_ad_capture.py"
    if not path.exists():
        pytest.skip("Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接")
    source = path.read_text(encoding="utf-8")

    assert "after_capture.page_capture_id" not in source


def test_app_worker_uses_shared_finalization_gate_before_persisting_terminal_run() -> None:
    """App Worker 不能绕过观察、规则、复核和最终门禁。"""
    source = (REPOSITORY_ROOT / "services/api/app/worker.py").read_text(encoding="utf-8")
    start = source.index("async def _process_app_one")
    end = source.index("async def _finish_progress", start)
    app_worker = source[start:end]

    assert "_drain_snapshot_inspection_work" in app_worker
    assert "_finalize_rule_executions" in app_worker
    assert "_finalize_persisted_inspection_work" in app_worker
    assert "_finish_terminal_run" in app_worker
    assert "task_finalization_service" in app_worker


def test_app_ready_recovery_uses_explicit_session_reattach_contract() -> None:
    """Worker 重启后必须重新获取设备会话，不得依赖进程内集合。"""
    driver = (REPOSITORY_ROOT / "services/api/app/domain/interaction/surface_driver.py").read_text(
        encoding="utf-8"
    )
    workflow = (REPOSITORY_ROOT / "services/api/app/application/app_workflow.py").read_text(
        encoding="utf-8"
    )
    factory = (
        REPOSITORY_ROOT / "services/api/app/infrastructure/app_runtime/factory.py"
    ).read_text(encoding="utf-8")

    assert "reattach_session" in driver
    assert "reattach_session" in workflow
    assert '"reattach_session"' in factory


def test_android_coordinate_resolver_rejects_out_of_bounds_points() -> None:
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-1",
        semantic_target=None,
        action_type=InteractionOperation.TAP,
        parameters={
            "point": {"x": 1080, "y": 100, "space": "device_px"},
        },
    )

    with pytest.raises(Exception, match="范围"):
        resolver.resolve_android(request)


def test_adb_screencap_fallback_checks_command_exit_status() -> None:
    source = (REPOSITORY_ROOT / "services/api/app/infrastructure/device/adb_ops.py").read_text(
        encoding="utf-8"
    )

    assert "returncode != 0" in source
    assert 'b"\\x89PNG\\r\\n\\x1a\\n"' in source


def test_mobile_demo_observation_carries_stable_command_identity() -> None:
    source = (
        REPOSITORY_ROOT / "services/api/app/infrastructure/device/android_capture_runtime.py"
    ).read_text(encoding="utf-8")
    start = source.index("async def observe_to_file")
    end = source.index("async def coordinate_intent", start)
    observe = source[start:end]

    assert "TimedObservationIntent(" in observe
    assert 'role="observation"' in observe
    assert "action_execution_id=" not in observe
    assert "self._observation_sequence += 1" in observe
    assert 'f"observation:{self.session_id}:{command_sequence}"' in observe
    assert "self.coordinator.capture_observation(" in observe
    assert "self.capture_writer.capture(" not in observe


def test_mobile_demo_matrix_agent_identity_includes_profile_scope() -> None:
    path = REPOSITORY_ROOT / "services/api/app/api/v1/mobile_demo_agent_ad_capture.py"
    if not path.exists():
        pytest.skip("Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接")
    source = path.read_text(encoding="utf-8")
    assert "profile_id: str | None = None" in source
    assert 'runtime_scope = f"{run_id}:{profile_id}"' in source
    assert 'session_id=f"mobile-demo-session:{runtime_scope}"' in source

    matrix_path = REPOSITORY_ROOT / "services/api/app/api/v1/mobile_demo.py"
    if not matrix_path.exists():
        pytest.skip("Mobile Demo API 已下线，正式链路改由 App Workflow 承接")
    matrix_source = matrix_path.read_text(encoding="utf-8")
    assert "profile_id=profile.id" in matrix_source


def test_app_capture_profile_binds_action_execution_to_idempotency_key() -> None:
    source = (
        REPOSITORY_ROOT / "services/api/app/application/app_interaction_runtime.py"
    ).read_text(encoding="utf-8")

    assert 'profile_token = request.phase.value' in source
    assert 'app-{profile_token}-v1:' in source
    assert "request.action_execution_id or 'observation'" in source


def test_app_action_capture_identity_is_business_idempotent() -> None:
    source = (
        REPOSITORY_ROOT / "services/api/app/application/app_capture_persistence.py"
    ).read_text(encoding="utf-8")
    start = source.index("async def persist_action_capture")
    end = source.index("def _read_png_dimensions", start)
    action_capture = source[start:end]

    assert "uuid5(" in action_capture
    assert "alioth:app-capture:{action_execution_id}:{role}" in action_capture
    assert "alioth:app-artifact:{action_execution_id}:{role}:{raw.artifact_type}" in action_capture


def test_mobile_demo_requires_explicit_done_and_complete_evidence() -> None:
    path = REPOSITORY_ROOT / "services/api/app/api/v1/mobile_demo.py"
    if not path.exists():
        pytest.skip("Mobile Demo API 已下线，正式链路改由 App Workflow 承接")
    source = path.read_text(encoding="utf-8")
    assert (
        'MOBILE_DEMO_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "incomplete"})'
        in source
    )
    assert "run.status in MOBILE_DEMO_TERMINAL_STATUSES" in source
    assert "v.status === 'incomplete'" in source
    start = source.index("def _run_agent_workflow_ad_capture_stub")
    end = source.index("def _maybe_run_agent_fallback_instead_of_airtest", start)
    workflow = source[start:end]

    assert 'any(s.action == "done" and s.ok for s in step_results)' in workflow
    assert "has_failed_step = any(not s.ok for s in step_results)" in workflow
    assert "completed = explicit_done" in workflow
    assert "completed and rel_shots and not has_failed_step" in workflow
    assert 'run.status = "succeeded"' in workflow
    assert (
        'run.status = "incomplete" if (rel_shots or ok_steps > 0) else "failed"'
        in workflow
    )

    agent_source = (
        REPOSITORY_ROOT / "services/api/app/api/v1/mobile_demo_agent_ad_capture.py"
    ).read_text(encoding="utf-8")
    loop_start = agent_source.index("async def _run_agent_ad_capture_loop_async")
    agent_loop = agent_source[loop_start:]
    assert 'phase_now == _PHASE_DONE or no_more_tabs' in agent_loop
    assert '"missing_explicit_done"' in agent_loop
    assert 'if action == "done":' in agent_loop
    assert 'if action == "done" or phase_now == _PHASE_DONE or no_more_tabs:' not in agent_loop


def test_pillow_is_declared_as_production_dependency() -> None:
    """生产 Worker 导入图像策略时必须能够使用 Pillow。"""
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]

    dependencies = [str(item).casefold() for item in project["dependencies"]]
    assert any(item.startswith("pillow") for item in dependencies)
