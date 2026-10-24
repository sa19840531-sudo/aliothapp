"""App Workflow 生产路由的 fail-closed 测试。"""

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.application.app_run_recovery import AppRunExecutionLease
from services.api.app.application.app_workflow_dispatch import (
    AppWorkflowDispatcher,
    AppWorkflowNotConfiguredError,
    H5WorkflowNotConfiguredError,
    RuntimeBackedAppWorkflowHandler,
    RuntimeBackedH5WorkflowHandler,
    task_target_from_dict,
)
from services.api.app.application.app_workflow_router import (
    AppWorkflowRouter,
    WorkflowRoute,
    WorkflowRoutingError,
)
from services.api.app.application.web_workflow_dispatch import (
    WebWorkflowNotConfiguredError,
)
from services.api.app.core.config import Settings
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord, RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


def _app_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-app",
        task_id="task-app",
        message="app task",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "_task_target": {
                "platform": "app",
                "target_url": None,
                "app": {
                    "platform_build_ref": "app-build:1",
                    "package_name": "com.example.app",
                    "device": {"backend": "cloud_phone", "profile_id": "default"},
                },
            },
        },
    )


def _h5_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-h5",
        task_id="task-h5",
        message="h5 task",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v1",
            "_task_target": {
                "platform": "h5",
                "target_url": "https://example.com/mobile",
                "app": None,
            },
        },
    )


def _h5_v2_record() -> AgentRunRecord:
    record = _h5_record()
    record.task_config["workflow_version"] = "v2"
    return record


def _web_v2_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-web",
        task_id="task-web",
        message="web task",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.com/desktop",
                "app": None,
                "web": {
                    "browser_profile_id": "desktop_fhd_1920x1080_v1",
                    "adult_self_attestation": {
                        "declared": True,
                        "statement_version": "adult-v1",
                        "declared_by": "operator-1",
                        "declared_at": "2026-08-30T12:00:00Z",
                    },
                },
            },
        },
    )


def test_app_route_requires_an_explicit_handler() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True),
        handler=None,
    )

    assert dispatcher.route(_app_record(), Settings()) is WorkflowRoute.APP
    with pytest.raises(AppWorkflowNotConfiguredError, match="App Workflow handler"):
        dispatcher.require_handler()


def test_h5_route_remains_outside_the_app_runtime_handler() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True),
        handler=None,
    )

    assert dispatcher.route(_h5_record(), Settings()) is WorkflowRoute.H5


def test_h5_v2_route_uses_locked_deployment_mode() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True, h5_v2_mode="on"),
        handler=None,
    )

    assert dispatcher.route(_h5_v2_record(), Settings()).value == "h5_v2"


def test_web_target_snapshot_restores_adult_self_attestation() -> None:
    target = task_target_from_dict(
        {
            "platform": "web",
            "target_url": "https://example.test",
            "web": {
                "browser_profile_id": "desktop_fhd_1920x1080_v1",
                "adult_self_attestation": {
                    "declared": True,
                    "statement_version": "adult-v1",
                    "declared_by": "operator-1",
                    "declared_at": "2026-08-30T12:00:00Z",
                },
            },
        }
    )

    assert target.web is not None
    assert target.web.adult_self_attestation is not None
    assert target.web.adult_self_attestation.declared_by == "operator-1"
    assert target.web.adult_self_attestation.declared_at == datetime(
        2026, 8, 30, 12, 0, tzinfo=UTC
    )


def test_web_target_snapshot_allows_missing_attestation() -> None:
    target = task_target_from_dict(
        {
            "platform": "web",
            "target_url": "https://example.test",
            "web": {
                "browser_profile_id": "desktop_fhd_1920x1080_v1",
                "adult_self_attestation": None,
            },
        }
    )

    assert target.web is not None
    assert target.web.adult_self_attestation is None


def test_web_target_snapshot_rejects_false_attestation() -> None:
    with pytest.raises(ValueError, match="成年声明|主动声明成年"):
        task_target_from_dict(
            {
                "platform": "web",
                "target_url": "https://example.test",
                "web": {
                    "browser_profile_id": "desktop_fhd_1920x1080_v1",
                    "adult_self_attestation": {
                        "declared": False,
                        "statement_version": "adult-v1",
                        "declared_by": "operator-1",
                        "declared_at": "2026-08-30T12:00:00Z",
                    },
                },
            }
        )


def test_unknown_app_workflow_version_fails_closed() -> None:
    record = _app_record()
    record.task_config["workflow_version"] = "v3"
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True),
        handler=None,
    )

    with pytest.raises(WorkflowRoutingError, match="workflow_version=v2"):
        dispatcher.route(record, Settings())


def test_missing_locked_target_fails_closed_unless_explicitly_legacy_h5() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True),
        handler=None,
    )
    record = _h5_record()
    record.task_config.pop("_task_target")

    with pytest.raises(WorkflowRoutingError, match="_task_target"):
        dispatcher.route(record, Settings())

    record.task_config["legacy_h5_compatible"] = True
    assert dispatcher.route(record, Settings()) is WorkflowRoute.H5


@pytest.mark.asyncio
async def test_app_dispatch_calls_handler_without_h5_agent() -> None:
    calls: list[str] = []

    class Handler:
        async def run(self, *, record, settings, progress_recorder=None):
            del progress_recorder
            calls.append(record.run_id)
            return "app-result"

    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True),
        handler=Handler(),
    )

    result = await dispatcher.dispatch(record=_app_record(), settings=Settings())

    assert result == "app-result"
    assert calls == ["run-app"]


@pytest.mark.asyncio
async def test_runtime_backed_handler_passes_composed_runtime_to_executor() -> None:
    calls: list[object] = []

    class Provider:
        async def build(self, *, record, settings):
            del record, settings
            return "app-v2-runtime"

    class Executor:
        async def run(self, runtime, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            calls.append(runtime)
            return "run-result"

    handler = RuntimeBackedAppWorkflowHandler(
        provider=Provider(),
        executor=Executor(),
    )

    result = await handler.run(record=_app_record(), settings=Settings())

    assert result == "run-result"
    assert calls == ["app-v2-runtime"]


@pytest.mark.asyncio
async def test_h5_v2_dispatch_requires_explicit_handler_and_never_falls_back() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=True, h5_v2_mode="on"),
        handler=None,
        h5_handler=None,
    )

    with pytest.raises(H5WorkflowNotConfiguredError, match="H5 V2 Workflow handler"):
        await dispatcher.dispatch_h5_v2(
            record=_h5_v2_record(),
            settings=Settings(),
        )


@pytest.mark.asyncio
async def test_runtime_backed_h5_handler_passes_runtime_to_executor() -> None:
    calls: list[object] = []

    class Provider:
        async def build(self, *, record, settings):
            del record, settings
            return "h5-v2-runtime"

    class Executor:
        async def run(self, runtime, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            calls.append(runtime)
            return "h5-result"

    handler = RuntimeBackedH5WorkflowHandler(
        provider=Provider(),
        executor=Executor(),
    )

    result = await handler.run(record=_h5_v2_record(), settings=Settings())

    assert result == "h5-result"
    assert calls == ["h5-v2-runtime"]


@pytest.mark.asyncio
async def test_web_v2_dispatch_uses_only_explicit_web_handler() -> None:
    calls: list[str] = []

    class Handler:
        async def run(self, *, record, settings, progress_recorder=None):
            del settings, progress_recorder
            calls.append(record.run_id)
            return "web-result"

    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=False, web_v2_mode="exploration_only"),
        handler=None,
        web_handler=Handler(),
    )

    result = await dispatcher.dispatch_web_v2(
        record=_web_v2_record(),
        settings=Settings(),
    )

    assert result == "web-result"
    assert calls == ["run-web"]


@pytest.mark.asyncio
async def test_web_v2_dispatch_fails_closed_without_web_handler() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=False, web_v2_mode="exploration_only"),
        handler=None,
    )

    with pytest.raises(WebWorkflowNotConfiguredError, match="Web V2 Workflow handler"):
        await dispatcher.dispatch_web_v2(
            record=_web_v2_record(),
            settings=Settings(),
        )


@pytest.mark.asyncio
async def test_worker_routes_app_before_building_main_agent(monkeypatch) -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    record = await queue.enqueue(
        "app task",
        task_id="task-app",
        task_config=_app_record().task_config,
    )
    state_store = InMemoryAgentRunStateStore()
    routed: list[WorkflowRoute] = []

    async def _unexpected_main_agent(*_args, **_kwargs):
        raise AssertionError("App Run 不得构造通用 Main Agent")

    async def _capture_app(*_args, dispatcher, **_kwargs):
        routed.append(dispatcher.route(record, Settings()))

    class Recovery:
        async def acquire(self, command):
            return AppRunExecutionLease(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                lease_owner=command.lease_owner,
                lease_token=command.lease_token,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                row_version=1,
                attempt_count=1,
            )

        async def renew(self, command):
            return AppRunExecutionLease(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                lease_owner=command.lease_owner,
                lease_token=command.lease_token,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                row_version=2,
                attempt_count=1,
            )

        async def release(self, command):
            del command
            return True

    monkeypatch.setattr(worker_module, "build_default_main_agent", _unexpected_main_agent)
    monkeypatch.setattr(worker_module, "_process_app_one", _capture_app)

    settings = Settings.model_validate(
        {"workflow": {"app_qc_enabled": True, "app_runtime_provider": "official"}}
    )
    await worker_module._process_one(
        queue,
        record.run_id,
        settings,
        state_store=state_store,
        worker_instance_id="test-worker",
        app_run_recovery=Recovery(),
    )

    assert routed == [WorkflowRoute.APP]
