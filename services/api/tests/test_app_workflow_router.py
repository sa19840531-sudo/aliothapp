"""持久化 TaskTarget 到 H5/App Workflow 的显式路由测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.app_workflow_router import (
    AppWorkflowRouter,
    WorkflowRoute,
    WorkflowRoutingError,
)
from services.api.app.domain.task import (
    AdultSelfAttestation,
    AppTaskConfig,
    TaskTarget,
    TaskTargetPlatform,
    WebTaskConfig,
)


def _h5_target() -> TaskTarget:
    return TaskTarget(
        platform=TaskTargetPlatform.H5,
        target_url="https://m.example.com",
    )


def _app_target() -> TaskTarget:
    return TaskTarget(
        platform=TaskTargetPlatform.APP,
        app=AppTaskConfig(platform_build_ref="app-build:00000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_locked_h5_v1_always_routes_to_existing_playwright_workflow(mode: str) -> None:
    router = AppWorkflowRouter(app_qc_enabled=False, h5_v2_mode=mode)

    assert router.route(target=_h5_target(), workflow_version="v1") is WorkflowRoute.H5


@pytest.mark.parametrize(
    ("mode", "expected_value"),
    [
        ("off", "h5"),
        ("shadow", "h5_shadow"),
        ("on", "h5_v2"),
    ],
)
def test_locked_h5_v2_respects_deployment_mode(
    mode: str,
    expected_value: str,
) -> None:
    router = AppWorkflowRouter(app_qc_enabled=False, h5_v2_mode=mode)

    assert router.route(target=_h5_target(), workflow_version="v2").value == expected_value


def test_unknown_h5_workflow_version_fails_closed() -> None:
    router = AppWorkflowRouter(app_qc_enabled=False, h5_v2_mode="on")

    with pytest.raises(WorkflowRoutingError, match="H5.*workflow_version"):
        router.route(target=_h5_target(), workflow_version="v3")


def test_enabled_app_v2_routes_to_app_qc_workflow() -> None:
    router = AppWorkflowRouter(app_qc_enabled=True)

    assert router.route(target=_app_target(), workflow_version="v2") is WorkflowRoute.APP


def test_app_v1_and_disabled_app_are_rejected() -> None:
    with pytest.raises(WorkflowRoutingError, match="workflow_version=v2"):
        AppWorkflowRouter(app_qc_enabled=True).route(
            target=_app_target(),
            workflow_version="v1",
        )
    with pytest.raises(WorkflowRoutingError, match="未启用"):
        AppWorkflowRouter(app_qc_enabled=False).route(
            target=_app_target(),
            workflow_version="v2",
        )


def _web_target() -> TaskTarget:
    return TaskTarget(
        platform=TaskTargetPlatform.WEB,
        target_url="https://www.example.com",
        web=WebTaskConfig(
            browser_profile_id="desktop_fhd_1920x1080_v1",
            adult_self_attestation=AdultSelfAttestation(
                declared=True,
                statement_version="adult-v1",
                declared_by="operator-1",
                declared_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
            ),
        ),
    )


@pytest.mark.parametrize("h5_mode", ["off", "shadow", "on"])
def test_web_v2_route_is_independent_from_h5_mode(h5_mode: str) -> None:
    router = AppWorkflowRouter(
        app_qc_enabled=False,
        h5_v2_mode=h5_mode,
        web_v2_mode="exploration_only",
    )

    assert router.route(target=_web_target(), workflow_version="v2") is WorkflowRoute.WEB_V2


def test_web_rejects_disabled_web_mode_even_when_h5_is_enabled() -> None:
    router = AppWorkflowRouter(
        app_qc_enabled=False,
        h5_v2_mode="on",
        web_v2_mode="off",
    )

    with pytest.raises(WorkflowRoutingError, match="Web V2 Workflow 未开启"):
        router.route(target=_web_target(), workflow_version="v2")


def test_web_rejects_legacy_workflow_version() -> None:
    router = AppWorkflowRouter(
        app_qc_enabled=False,
        web_v2_mode="exploration_only",
    )

    with pytest.raises(WorkflowRoutingError, match="仅支持 workflow_version=v2"):
        router.route(target=_web_target(), workflow_version="v1")


def test_web_mode_is_independent_from_h5_mode() -> None:
    router = AppWorkflowRouter(
        app_qc_enabled=False,
        h5_v2_mode="off",
        web_v2_mode="exploration_only",
    )

    assert router.route(target=_web_target(), workflow_version="v2") is WorkflowRoute.WEB_V2


def test_router_requires_persisted_target_and_locked_version() -> None:
    router = AppWorkflowRouter(app_qc_enabled=True)

    with pytest.raises(TypeError, match="TaskTarget"):
        router.route(target={"platform": "app"}, workflow_version="v2")
    with pytest.raises(WorkflowRoutingError, match="锁定"):
        router.route(target=_app_target(), workflow_version="")
