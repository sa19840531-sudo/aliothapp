"""WEB_V2 Handler 必须独立于 H5/App 的分派合同。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.app_workflow_dispatch import AppWorkflowDispatcher
from services.api.app.application.app_workflow_router import AppWorkflowRouter, WorkflowRoute
from services.api.app.application.web_workflow_dispatch import (
    RuntimeBackedWebWorkflowHandler,
    WebWorkflowNotConfiguredError,
)
from services.api.app.core.config import Settings
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


def _web_record(*, execution_mode: str = "exploration_only") -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-web",
        task_id="task-web",
        message="web task",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "execution_mode": execution_mode,
            "web_inspection_notification_mode": (
                "enforced" if execution_mode == "full_inspection" else "off"
            ),
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test/dashboard",
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


@pytest.mark.asyncio
async def test_web_dispatch_requires_explicit_handler_and_never_falls_back() -> None:
    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(
            app_qc_enabled=False,
            web_v2_mode="exploration_only",
        ),
        handler=None,
        h5_handler=None,
        web_handler=None,
    )

    assert dispatcher.route(_web_record(), Settings()) is WorkflowRoute.WEB_V2
    with pytest.raises(WebWorkflowNotConfiguredError, match="WEB_RUNTIME_NOT_CONFIGURED"):
        await dispatcher.dispatch_web_v2(
            record=_web_record(),
            settings=Settings(),
        )


@pytest.mark.asyncio
async def test_runtime_backed_web_handler_uses_only_web_provider_and_executor() -> None:
    calls: list[object] = []

    class Provider:
        async def build(self, *, record, settings):
            del record, settings
            return "web-v2-runtime"

    class Executor:
        async def run(self, runtime, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            calls.append(runtime)
            return "web-result"

    handler = RuntimeBackedWebWorkflowHandler(
        provider=Provider(),
        executor=Executor(),
    )

    result = await handler.run(record=_web_record(), settings=Settings())

    assert result == "web-result"
    assert calls == ["web-v2-runtime"]


@pytest.mark.asyncio
async def test_incremental_publication_dispatch_returns_without_waiting_for_judge() -> None:
    judge_completed = False

    class Provider:
        async def build(self, *, record, settings):
            del record, settings
            return "runtime-with-persisted-publication"

    class Executor:
        async def run(self, runtime, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            assert runtime == "runtime-with-persisted-publication"
            # Runtime 只完成 Publication/Outbox 与唤醒；Judge 结论不在执行器合同内。
            assert judge_completed is False
            return "exploration-continued"

    result = await RuntimeBackedWebWorkflowHandler(
        provider=Provider(),
        executor=Executor(),
    ).run(record=_web_record(), settings=Settings())

    assert result == "exploration-continued"
    assert judge_completed is False


@pytest.mark.asyncio
async def test_web_full_inspection_runtime_keeps_the_locked_execution_mode() -> None:
    observed: list[tuple[str, str]] = []

    class Provider:
        async def build(self, *, record, settings):
            del settings
            observed.append(
                (
                    record.task_config["execution_mode"],
                    record.task_config["web_inspection_notification_mode"],
                )
            )
            return "web-full-runtime"

    class Executor:
        async def run(self, runtime, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            assert runtime == "web-full-runtime"
            return "web-full-result"

    result = await RuntimeBackedWebWorkflowHandler(
        provider=Provider(),
        executor=Executor(),
    ).run(record=_web_record(execution_mode="full_inspection"), settings=Settings())

    assert result == "web-full-result"
    assert observed == [("full_inspection", "enforced")]
