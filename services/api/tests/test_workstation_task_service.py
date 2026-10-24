"""工作台任务服务业务操作测试。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis
import pytest

from services.api.app.application import workstation_tasks as workstation_tasks_module
from services.api.app.application.task_submission import (
    RunCancellationOutcome,
    TaskSubmissionResult,
)
from services.api.app.application.web_runtime_composition import _locked_web_runtime_config
from services.api.app.application.workstation_tasks import (
    InvalidTaskOperationError,
    ProductSnapshot,
    TaskNotFoundError,
    WorkstationTaskCreateCommand,
    WorkstationTaskDetail,
    WorkstationTaskOperationResult,
    WorkstationTaskService,
    web_execution_phase,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import (
    AdultSelfAttestation,
    H5InspectionNotificationMode,
    TaskExecutionMode,
    TaskTargetPlatform,
)
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any", "taskObjects:write:any"}),
)


@pytest.mark.parametrize(
    ("task_status", "run_status", "reason", "unknown", "expected"),
    [
        ("queued", None, None, (), "queued"),
        ("queued", "pending", None, (), "pending"),
        ("running", "running", None, (), "running"),
        ("failed", "failed", "WEB_POLICY_CONFIGURATION_INVALID", (), "configuration_error"),
        ("failed", "failed", "WEB_HOME_UNREACHABLE", (), "navigation_failed"),
        ("failed", "failed", "WEB_MANIFEST_PERSISTENCE_FAILED", (), "evidence_failed"),
        ("review", "done", None, ("unknown",), "classification_review"),
    ],
)
def test_web_execution_phase_distinguishes_queue_and_failure_surfaces(
    task_status, run_status, reason, unknown, expected
) -> None:
    assert web_execution_phase(
        task_status=task_status,
        run_status=run_status,
        failure_reason=reason,
        coverage_unrecognized_page_types=unknown,
    ) == expected


class FakeTaskRepository:
    retry_calls: list[str] = []

    async def get_task_detail(
        self,
        task_id: str,
        *,
        access_scope: AccessScope,
        for_write: bool = False,
    ) -> WorkstationTaskDetail | None:
        if task_id != "task-old" or not access_scope.can_read_resource(
            owner_id="owner-1",
            tenant_id="tenant-1",
        ):
            return None
        if for_write and not access_scope.can_write_resource(
            owner_id="owner-1",
            tenant_id="tenant-1",
        ):
            return None
        return WorkstationTaskDetail(
            task_id=task_id,
            task_name="首页质检",
            product=ProductSnapshot("质量部", "H5-001", "首页"),
            target_url="https://example.com",
            target_platform=TaskTargetPlatform.H5,
            execution_mode=TaskExecutionMode.FULL_INSPECTION,
            journey_steps=["首页"],
            status="failed",
            progress=0,
            report_status="failed",
            report_availability="none",
            failure_reason="模型调用失败",
            available_actions=["failure", "rerun", "copy"],
            created_at=datetime(2026, 7, 13, tzinfo=UTC),
            started_at=datetime(2026, 7, 13, tzinfo=UTC),
            finished_at=datetime(2026, 7, 13, tzinfo=UTC),
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            run_id="run-old",
        )

    async def retry_quarantined_finalization(
        self,
        *,
        task_id: str,
        access_scope: AccessScope,
    ) -> WorkstationTaskOperationResult | None:
        del access_scope
        self.retry_calls.append(task_id)
        return WorkstationTaskOperationResult(
            task_id=task_id,
            status="running",
            run_id="run-old",
            message="正式报告收口重试已恢复",
        )


class FakeTaskSubmissionService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit_task(self, **kwargs) -> TaskSubmissionResult:
        self.calls.append(kwargs)
        return TaskSubmissionResult(
            task_id="task-new",
            run_id="run-new",
            ruleset_id="ruleset-1",
            status="queued",
        )


async def test_admin_can_retry_quarantined_finalization_in_place() -> None:
    repository = FakeTaskRepository()
    repository.retry_calls = []
    service = WorkstationTaskService(repository=repository)

    result = await service.perform_operation(
        task_id="task-old",
        operation="retry-finalization",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.status == "running"
    assert repository.retry_calls == ["task-old"]


async def test_non_admin_cannot_retry_quarantined_finalization() -> None:
    service = WorkstationTaskService(repository=FakeTaskRepository())
    owner_scope = AccessScope(
        actor_user_id="owner-1",
        tenant_id="tenant-1",
        permissions=frozenset(),
    )

    with pytest.raises(InvalidTaskOperationError, match="管理员"):
        await service.perform_operation(
            task_id="task-old",
            operation="retry-finalization",
            access_scope=owner_scope,
        )


async def test_create_full_h5_scan_uses_ninety_minute_timeout() -> None:
    submission_service = FakeTaskSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
    )

    await service.create_task(
        WorkstationTaskCreateCommand(
            task_name="完整巡检",
            target_url="https://example.com",
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            product=ProductSnapshot("质量部", "H5-001", "首页"),
            scan_mode="full",
            journey_steps=[],
            remark=None,
        ),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert submission_service.calls[0]["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": 5400,
        "max_steps": 80,
        "max_ai_calls": 200,
    }
    assert submission_service.calls[0]["page_sampling_policy"] == {}
    assert submission_service.calls[0]["task_config"]["h5_exploration_target"] == {
        "required_page_types": []
    }
    assert submission_service.calls[0]["task_config"]["execution_mode"] == "full_inspection"
    assert (
        submission_service.calls[0]["task_config"]["h5_inspection_notification_mode"]
        == "off"
    )


async def test_create_h5_scan_uses_server_locked_notification_mode() -> None:
    submission_service = FakeTaskSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
        h5_inspection_notification_mode=H5InspectionNotificationMode.ENFORCED,
    )

    await service.create_task(
        WorkstationTaskCreateCommand(
            task_name="可靠发布巡检",
            target_url="https://example.com",
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            product=ProductSnapshot("质量部", "H5-002", "可靠发布页"),
            scan_mode="full",
            journey_steps=[],
            remark=None,
        ),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert (
        submission_service.calls[0]["task_config"]["h5_inspection_notification_mode"]
        == "enforced"
    )


async def test_create_quick_scan_keeps_small_explicit_step_budget() -> None:
    submission_service = FakeTaskSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
    )

    await service.create_task(
        WorkstationTaskCreateCommand(
            task_name="快速巡检",
            target_url="https://example.com",
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            product=ProductSnapshot("质量部", "H5-001", "首页"),
            scan_mode="quick",
            journey_steps=[],
            remark=None,
        ),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert submission_service.calls[0]["loop_guard"] == {
        "max_depth": 2,
        "max_steps": 40,
        "max_duration_seconds": 900,
        "max_ai_calls": 64,
    }
    assert submission_service.calls[0]["page_sampling_policy"] == {
        "mode": "first_n",
        "max_pages": 3,
    }


async def test_create_h5_scan_preserves_explicit_required_page_types() -> None:
    submission_service = FakeTaskSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
    )

    await service.create_task(
        WorkstationTaskCreateCommand(
            task_name="指定目标页型巡检",
            target_url="https://example.com",
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            product=ProductSnapshot("质量部", "H5-001", "首页"),
            scan_mode="custom",
            journey_steps=[],
            remark=None,
            required_page_types=("home", "content_list"),
        ),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert submission_service.calls[0]["page_sampling_policy"] == {
        "mode": "by_page_type",
        "include_page_types": ["home", "content_list"],
    }
    assert submission_service.calls[0]["task_config"]["h5_exploration_target"] == {
        "required_page_types": ["home", "content_list"],
    }
    assert submission_service.calls[0]["task_config"]["inspection_scope"] == {
        "page_types": ["content_list", "home"],
    }


class FakeLifecycleRepository:
    def __init__(self, detail: WorkstationTaskDetail) -> None:
        self.detail = detail
        self.status_updates: list[dict] = []

    async def get_task_detail(
        self,
        task_id: str,
        *,
        access_scope: AccessScope,
        for_write: bool = False,
    ) -> WorkstationTaskDetail | None:
        return self.detail if task_id == self.detail.task_id else None

    async def update_task_status(self, **kwargs):
        self.status_updates.append(kwargs)
        return type(
            "OperationResult",
            (),
            {
                "task_id": self.detail.task_id,
                "status": kwargs["status"],
                "run_id": self.detail.run_id,
                "message": "任务操作已提交",
            },
        )()


class FakeLifecycleSubmissionService(FakeTaskSubmissionService):
    def __init__(
        self,
        cancellation_outcome: RunCancellationOutcome = RunCancellationOutcome.CANCELED,
    ) -> None:
        super().__init__()
        self.ensure_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.cancellation_outcome = cancellation_outcome

    async def ensure_run_enqueued(self, run_id: str) -> bool:
        self.ensure_calls.append(run_id)
        return True

    async def cancel_run(self, run_id: str) -> RunCancellationOutcome:
        self.cancel_calls.append(run_id)
        return self.cancellation_outcome


async def test_rerun_creates_and_enqueues_new_task() -> None:
    submission_service = FakeTaskSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id="task-old",
        operation="rerun",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.task_id == "task-new"
    assert result.run_id == "run-new"
    assert result.status == "queued"
    assert submission_service.calls[0]["target"].platform is TaskTargetPlatform.H5
    assert submission_service.calls[0]["target"].target_url == "https://example.com"
    assert submission_service.calls[0]["task_config"]["source_task_id"] == "task-old"
    assert submission_service.calls[0]["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": 5400,
        "max_steps": 80,
        "max_ai_calls": 200,
    }
    assert submission_service.calls[0]["page_sampling_policy"] == {}
    assert submission_service.calls[0]["task_config"]["h5_exploration_target"] == {
        "required_page_types": []
    }
    # 老任务没有质检范围，重跑按全部处理
    assert submission_service.calls[0]["task_config"]["inspection_scope"] == {"page_types": []}


async def test_rerun_preserves_locked_required_page_types() -> None:
    detail = _task_detail(
        status="done",
        required_page_types=("home", "content_list"),
        inspection_scope_page_types=("home", "content_list"),
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=submission_service,
    )

    await service.perform_operation(
        task_id=detail.task_id,
        operation="rerun",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert submission_service.calls[0]["page_sampling_policy"] == {
        "mode": "by_page_type",
        "include_page_types": ["home", "content_list"],
    }
    assert submission_service.calls[0]["task_config"]["h5_exploration_target"] == {
        "required_page_types": ["home", "content_list"],
    }
    assert submission_service.calls[0]["task_config"]["inspection_scope"] == {
        "page_types": ["content_list", "home"],
    }


async def test_cross_tenant_task_operation_is_indistinguishable_from_missing_task() -> None:
    foreign_scope = AccessScope(
        actor_user_id="owner-2",
        tenant_id="tenant-2",
        permissions=frozenset(),
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
    )

    with pytest.raises(TaskNotFoundError, match="未找到任务"):
        await service.perform_operation(
            task_id="task-old",
            operation="rerun",
            access_scope=foreign_scope,
        )
    assert submission_service.calls == []


async def test_task_owner_can_rerun_without_tenant_wide_permission() -> None:
    owner_scope = AccessScope(
        actor_user_id="owner-1",
        tenant_id="tenant-1",
        permissions=frozenset(),
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id="task-old",
        operation="rerun",
        access_scope=owner_scope,
    )
    assert result.task_id == "task-new"


async def test_same_tenant_reader_can_read_but_cannot_write_task_operation() -> None:
    tenant_reader = AccessScope(
        actor_user_id="auditor-1",
        tenant_id="tenant-1",
        permissions=frozenset({"taskObjects:read:tenant"}),
    )
    service = WorkstationTaskService(
        repository=FakeTaskRepository(),
        submission_service=FakeLifecycleSubmissionService(),
    )

    # 列表/详情的仓储查询可使用同租户 reader；写操作的 API 依赖在进入服务前会
    # 拒绝该作用域，服务本身也只接受仓储返回的可见详情，不为其提升权限。
    detail = await service.get_task_detail("task-old", access_scope=tenant_reader)
    assert detail is not None
    with pytest.raises(TaskNotFoundError, match="未找到任务"):
        await service.perform_operation(
            task_id="task-old",
            operation="rerun",
            access_scope=tenant_reader,
        )


async def test_start_keeps_queued_status_and_ensures_run_is_enqueued() -> None:
    detail = _task_detail(status="queued")
    repository = FakeLifecycleRepository(detail)
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=repository,
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id=detail.task_id,
        operation="start",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.status == "queued"
    assert submission_service.ensure_calls == ["run-old"]
    assert repository.status_updates == []


async def test_web_queued_detail_exposes_position_workers_and_start_condition() -> None:
    detail = _task_detail(status="queued", target_platform=TaskTargetPlatform.WEB)
    detail = detail.__class__(
        **{
            **detail.__dict__,
            "run_status": "pending",
            "execution_phase": "pending",
        }
    )

    class QueueReader:
        async def queue_position(self, run_id: str) -> int | None:
            assert run_id == "run-old"
            return 3

        async def read_worker_heartbeats(self) -> list[dict]:
            return [
                {"roles": ["web_explorer"]},
                {"roles": ["judge"]},
            ]

    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        web_queue_status_reader=QueueReader(),
    )

    result = await service.get_task_detail("task-old", access_scope=TEST_ACCESS_SCOPE)

    assert result is not None
    assert result.execution_phase == "pending"
    assert result.queue_position == 3
    assert result.tasks_ahead == 2
    assert result.available_worker_count == 1
    assert result.start_condition == "已入队，等待Web Worker释放执行槽并领取当前 Run"


@pytest.mark.parametrize(
    ("platform", "expected_position", "expected_workers", "worker_name"),
    [
        (TaskTargetPlatform.H5, 3, 1, "探索 Worker"),
        (TaskTargetPlatform.APP, 3, 1, "探索 Worker"),
        (TaskTargetPlatform.WEB, 2, 2, "Web Worker"),
    ],
)
async def test_queued_detail_observes_actual_platform_fifo_and_worker_roles(
    platform, expected_position, expected_workers, worker_name
) -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(client, ttl_seconds=3600, queue_key="test:default-queue")
    web_queue = RedisAgentRunQueue(client, ttl_seconds=3600, queue_key="test:web-queue")
    try:
        await client.rpush("test:default-queue", "app-ahead", "h5-ahead", "run-old")
        await client.rpush("test:web-queue", "web-ahead", "run-old")
        await queue.record_worker_heartbeat("explorer", roles={"explorer", "judge"})
        await queue.record_worker_heartbeat("web-one", roles={"web_explorer"})
        await queue.record_worker_heartbeat("web-two", roles={"web_explorer", "judge"})
        await queue.record_worker_heartbeat("judge", roles={"judge"})
        detail = replace(
            _task_detail(status="queued", target_platform=platform),
            execution_phase="queued",
            queue_position=99,
            tasks_ahead=98,
        )
        service = WorkstationTaskService(
            repository=FakeLifecycleRepository(detail),
            queue_status_reader=queue,
            web_queue_status_reader=web_queue,
        )

        result = await service.get_task_detail("task-old", access_scope=TEST_ACCESS_SCOPE)

        assert result is not None
        assert result.status == "queued"
        assert result.queue_position == expected_position
        assert result.tasks_ahead == expected_position - 1
        assert result.available_worker_count == expected_workers
        assert result.start_condition == f"已入队，等待{worker_name}释放执行槽并领取当前 Run"
    finally:
        await client.aclose()


@pytest.mark.parametrize("platform", list(TaskTargetPlatform))
async def test_queue_observation_failure_keeps_detail_readable_and_position_unknown(
    platform,
) -> None:
    class FailingQueueReader:
        async def queue_position(self, run_id):
            raise ConnectionError("队列观测暂不可用")

        async def read_worker_heartbeats(self):
            return []

    detail = replace(
        _task_detail(status="queued", target_platform=platform),
        execution_phase="queued",
        queue_position=99,
        tasks_ahead=98,
        available_worker_count=7,
    )
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        queue_status_reader=FailingQueueReader(),
        web_queue_status_reader=FailingQueueReader(),
    )

    result = await service.get_task_detail("task-old", access_scope=TEST_ACCESS_SCOPE)

    assert result is not None
    assert result.task_id == detail.task_id
    assert result.status == "queued"
    assert result.queue_position is None
    assert result.tasks_ahead is None
    assert result.available_worker_count is None
    assert "暂未确认" in result.start_condition


async def test_hanging_queue_observation_times_out_without_hiding_task_status(monkeypatch) -> None:
    position_canceled = asyncio.Event()
    heartbeat_canceled = asyncio.Event()

    class HangingQueueReader:
        async def queue_position(self, run_id):
            try:
                await asyncio.Event().wait()
            finally:
                position_canceled.set()

        async def read_worker_heartbeats(self):
            try:
                await asyncio.Event().wait()
            finally:
                heartbeat_canceled.set()

    monkeypatch.setattr(workstation_tasks_module, "TASK_QUEUE_OBSERVATION_TIMEOUT_SECONDS", 0.01)
    detail = replace(_task_detail(status="queued"), execution_phase="pending")
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail), queue_status_reader=HangingQueueReader()
    )

    result = await asyncio.wait_for(
        service.get_task_detail("task-old", access_scope=TEST_ACCESS_SCOPE), timeout=0.2
    )

    assert result is not None
    assert result.status == "queued"
    assert result.queue_position is None
    assert result.available_worker_count is None
    assert position_canceled.is_set()
    assert heartbeat_canceled.is_set()


@pytest.mark.parametrize("position", [None, 1])
async def test_queued_detail_does_not_invent_position_or_count_judge_as_explorer(
    position,
) -> None:
    class QueueReader:
        async def queue_position(self, run_id):
            return position

        async def read_worker_heartbeats(self):
            return [{"roles": ["judge"]}, {"roles": ["web_explorer"]}]

    detail = replace(
        _task_detail(status="queued"),
        execution_phase="pending",
        queue_position=99,
        tasks_ahead=98,
    )
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail), queue_status_reader=QueueReader()
    )

    result = await service.get_task_detail("task-old", access_scope=TEST_ACCESS_SCOPE)

    assert result is not None
    assert result.available_worker_count == 0
    assert result.queue_position == position
    assert result.tasks_ahead == (0 if position == 1 else None)
    assert result.start_condition == (
        "已入队，等待探索 Worker上线并领取当前 Run"
        if position is not None
        else "尚未确认排队位置，等待调度状态同步"
    )


@pytest.mark.parametrize("phase", ["running", "done", "canceled", "navigation_failed"])
async def test_nonqueued_detail_does_not_read_or_change_queue_observation(phase) -> None:
    class QueueReader:
        async def queue_position(self, run_id):
            raise AssertionError("非排队任务不读取队列")

    detail = replace(_task_detail(status="running"), execution_phase=phase)
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        queue_status_reader=QueueReader(),
        web_queue_status_reader=QueueReader(),
    )

    result = await service.get_task_detail("task-old", access_scope=TEST_ACCESS_SCOPE)

    assert result is detail


async def test_cancel_requests_run_cancellation_before_updating_task() -> None:
    detail = _task_detail(status="running")
    repository = FakeLifecycleRepository(detail)
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=repository,
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id=detail.task_id,
        operation="cancel",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.status == "canceled"
    assert submission_service.cancel_calls == ["run-old"]
    assert repository.status_updates[0]["allowed_statuses"] == [
        "queued",
        "running",
        "canceled",
    ]


async def test_running_cancel_stays_canceling_until_worker_confirms_terminal() -> None:
    detail = _task_detail(status="running")
    repository = FakeLifecycleRepository(detail)
    submission_service = FakeLifecycleSubmissionService(RunCancellationOutcome.CANCELING)
    service = WorkstationTaskService(
        repository=repository,
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id=detail.task_id,
        operation="cancel",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.status == "canceling"
    assert result.message == "取消请求已提交，等待 Worker 安全终止当前步骤"
    assert submission_service.cancel_calls == ["run-old"]
    assert repository.status_updates == []


@pytest.mark.parametrize("run_status", ["done", "failed", "aborted", "canceled"])
async def test_cancel_terminal_main_uses_task_cancellation_without_changing_run(run_status) -> None:
    detail = _task_detail(status="running", run_status=run_status)
    repository = FakeLifecycleRepository(detail)
    submission_service = FakeLifecycleSubmissionService()
    calls = []

    class TaskCanceler:
        async def cancel_finished_run_task(self, **kwargs):
            calls.append(kwargs)
            return WorkstationTaskOperationResult("task-old", "canceled", "run-old", "任务已取消")

    service = WorkstationTaskService(
        repository=repository,
        submission_service=submission_service,
        terminal_run_task_canceler=TaskCanceler(),
    )
    result = await service.perform_operation(
        task_id=detail.task_id, operation="cancel", access_scope=TEST_ACCESS_SCOPE
    )
    assert result.status == "canceled"
    assert calls == [
        {"task_id": "task-old", "run_id": "run-old", "access_scope": TEST_ACCESS_SCOPE}
    ]
    assert submission_service.cancel_calls == []
    assert repository.status_updates == []


async def test_cancel_run_finishing_during_click_rechecks_task_cancellation_fence() -> None:
    detail = _task_detail(status="running", run_status="running")
    submission_service = FakeLifecycleSubmissionService(None)

    class TaskCanceler:
        async def cancel_finished_run_task(self, **kwargs):
            assert kwargs["run_id"] == "run-old"
            return WorkstationTaskOperationResult("task-old", "canceled", "run-old", "任务已取消")

    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=submission_service,
        terminal_run_task_canceler=TaskCanceler(),
    )
    result = await service.perform_operation(
        task_id=detail.task_id, operation="cancel", access_scope=TEST_ACCESS_SCOPE
    )
    assert result.status == "canceled"
    assert submission_service.cancel_calls == ["run-old"]


async def test_running_cancel_syncs_when_run_is_already_canceled() -> None:
    """Run 已终态 canceled 但任务仍 running 时，再次取消应幂等同步任务终态。"""
    detail = _task_detail(status="running")
    repository = FakeLifecycleRepository(detail)
    submission_service = FakeLifecycleSubmissionService(RunCancellationOutcome.CANCELED)
    service = WorkstationTaskService(
        repository=repository,
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id=detail.task_id,
        operation="cancel",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.status == "canceled"
    assert submission_service.cancel_calls == ["run-old"]
    assert repository.status_updates[0]["status"] == "canceled"
    assert repository.status_updates[0]["allowed_statuses"] == [
        "queued",
        "running",
        "canceled",
    ]


async def test_copy_creates_new_task_instead_of_returning_source_task() -> None:
    detail = _task_detail(status="done")
    repository = FakeLifecycleRepository(detail)
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=repository,
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id=detail.task_id,
        operation="copy",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.task_id == "task-new"
    assert submission_service.calls[0]["task_config"]["source_operation"] == "copy"
    assert "h5_exploration_target" in submission_service.calls[0]["task_config"]


async def test_exploration_only_rerun_and_copy_preserve_locked_execution_mode() -> None:
    for status in ("done", "completed", "review"):
        for operation in ("rerun", "copy"):
            detail = _task_detail(
                status=status,
                execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
            )
            submission_service = FakeLifecycleSubmissionService()
            service = WorkstationTaskService(
                repository=FakeLifecycleRepository(detail),
                submission_service=submission_service,
            )

            await service.perform_operation(
                task_id=detail.task_id,
                operation=operation,
                access_scope=TEST_ACCESS_SCOPE,
            )

            assert submission_service.calls[0]["task_config"]["execution_mode"] == (
                TaskExecutionMode.EXPLORATION_ONLY.value
            )
            assert submission_service.calls[0]["task_config"]["workflow_version"] == "v2"


async def test_full_inspection_review_still_rejects_rerun() -> None:
    detail = _task_detail(status="review")
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=FakeLifecycleSubmissionService(),
    )

    with pytest.raises(InvalidTaskOperationError, match="只有已完成或失败任务"):
        await service.perform_operation(
            task_id=detail.task_id,
            operation="rerun",
            access_scope=TEST_ACCESS_SCOPE,
        )


async def test_web_rerun_creates_current_policy_with_source_lineage() -> None:
    attestation = AdultSelfAttestation(
        declared=True,
        statement_version="adult-v1",
        declared_by="test-user",
        declared_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    detail = _task_detail(
        status="done",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
        target_platform=TaskTargetPlatform.WEB,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        required_page_types=("video_list", "video_detail"),
        optional_page_types=("content_list",),
        not_applicable_page_types=("content_detail",),
        applicability_mode="catalog_declared",
        applicability_catalog_version="web-catalog-v2",
        applicability_catalog_hash="sha256:frozen",
        adult_self_attestation=attestation,
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=submission_service,
    )

    await service.perform_operation(
        task_id=detail.task_id,
        operation="rerun",
        access_scope=TEST_ACCESS_SCOPE,
    )

    call = submission_service.calls[0]
    assert call["target"].platform is TaskTargetPlatform.WEB
    assert call["target"].web is not None
    assert call["target"].web.browser_profile_id == "desktop_fhd_1920x1080_v1"
    assert call["target"].web.adult_self_attestation is attestation
    target = call["task_config"]["web_exploration_target"]
    assert target["browser_profile_id"] == "desktop_fhd_1920x1080_v1"
    assert target["adult_self_attestation"] == attestation.to_dict()
    # 首页没选就不强制必检
    assert target["required_page_types"] == ["video_detail", "video_list"]
    assert target["applicability_mode"] == "runtime_dynamic"
    assert target["catalog_version"] != "web-catalog-v2"
    assert target["catalog_hash"] != "sha256:frozen"
    assert call["task_config"]["source_task_id"] == detail.task_id
    assert call["task_config"]["source_operation"] == "rerun"
    # 源任务是仅探索：通知模式和新建时一样锁 off，Web 运行时只认这个配对
    assert call["task_config"]["web_inspection_notification_mode"] == "off"
    assert call["task_config"]["h5_inspection_notification_mode"] == "off"
    assert "h5_exploration_target" not in call["task_config"]
    assert "viewport" not in call["task_config"]


async def test_web_reexploration_creates_current_run_with_source_lineage() -> None:
    attestation = AdultSelfAttestation(
        declared=True,
        statement_version="adult-v1",
        declared_by="test-user",
        declared_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    detail = _task_detail(
        status="completed",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
        target_platform=TaskTargetPlatform.WEB,
        required_page_types=("video_list", "age_gate", "unknown"),
        applicability_mode="strict_four",
        applicability_catalog_version="2026-09-04.web.v3",
        applicability_catalog_hash="sha256:legacy",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        adult_self_attestation=attestation,
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=submission_service,
    )

    result = await service.perform_operation(
        task_id=detail.task_id,
        operation="exploration",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert result.task_id == "task-new"
    assert result.run_id == "run-new"
    call = submission_service.calls[0]
    assert call["submission_kind"] == "workstation_exploration"
    assert call["task_config"]["source_task_id"] == detail.task_id
    assert call["task_config"]["source_operation"] == "exploration"
    assert call["task_config"]["execution_mode"] == "exploration_only"
    target = call["task_config"]["web_exploration_target"]
    assert target["required_page_types"] == ["video_list"]
    assert target["applicability_mode"] == "runtime_dynamic"
    assert target["catalog_version"] != detail.applicability_catalog_version
    assert target["catalog_hash"] != detail.applicability_catalog_hash
    assert "discovery_policy" not in target


async def test_legacy_web_rerun_fallback_excludes_lifecycle_and_unknown_types() -> None:
    attestation = AdultSelfAttestation(
        declared=True,
        statement_version="adult-v1",
        declared_by="test-user",
        declared_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    detail = _task_detail(
        status="done",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
        target_platform=TaskTargetPlatform.WEB,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        required_page_types=("video_list", "age_gate", "unknown"),
        adult_self_attestation=attestation,
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=submission_service,
    )

    await service.perform_operation(
        task_id=detail.task_id,
        operation="rerun",
        access_scope=TEST_ACCESS_SCOPE,
    )

    target = submission_service.calls[0]["task_config"]["web_exploration_target"]
    assert target["required_page_types"] == ["video_list"]
    assert target["optional_page_types"] == [
        "activity",
        "collection_page",
        "content_detail",
        "content_list",
        "home",
        "search_page",
        "video_detail",
    ]
    assert target["not_applicable_page_types"] == []
    assert "age_gate" not in target["not_applicable_page_types"]
    assert "unknown" not in target["not_applicable_page_types"]


@pytest.mark.parametrize(
    ("operation", "execution_mode", "status"),
    [
        ("rerun", TaskExecutionMode.FULL_INSPECTION, "done"),
        ("copy", TaskExecutionMode.FULL_INSPECTION, "done"),
        ("rerun", TaskExecutionMode.EXPLORATION_ONLY, "done"),
        ("exploration", TaskExecutionMode.EXPLORATION_ONLY, "completed"),
        ("copy", TaskExecutionMode.EXPLORATION_ONLY, "completed"),
    ],
)
async def test_web_resubmission_config_is_accepted_by_web_runtime(
    operation: str,
    execution_mode: TaskExecutionMode,
    status: str,
) -> None:
    """工作台重跑/重新探索/复制出来的 Web 任务配置，必须过得了 Web 运行时开跑前的配置校验。"""

    detail = _task_detail(
        status=status,
        execution_mode=execution_mode,
        target_platform=TaskTargetPlatform.WEB,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        required_page_types=("video_list",),
        adult_self_attestation=AdultSelfAttestation(
            declared=True,
            statement_version="adult-v1",
            declared_by="test-user",
            declared_at=datetime(2026, 8, 30, tzinfo=UTC),
        ),
    )
    submission_service = FakeLifecycleSubmissionService()
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(detail),
        submission_service=submission_service,
    )

    await service.perform_operation(
        task_id=detail.task_id,
        operation=operation,
        access_scope=TEST_ACCESS_SCOPE,
    )

    call = submission_service.calls[0]
    assert call["task_config"]["browser_profile_snapshot"]["profile_id"] == (
        "desktop_fhd_1920x1080_v1"
    )
    # 提交服务会把任务目标写进 _task_target，这里照样补上，再交给运行时自己的校验
    run_config = {**call["task_config"], "_task_target": call["target"].to_dict()}
    target_url, profile_id = _locked_web_runtime_config(SimpleNamespace(task_config=run_config))
    assert target_url == "https://example.com"
    assert profile_id == "desktop_fhd_1920x1080_v1"


def _task_detail(
    *,
    status: str,
    execution_mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION,
    workflow_version: str = "v2",
    required_page_types: tuple[str, ...] = (),
    inspection_scope_page_types: tuple[str, ...] = (),
    optional_page_types: tuple[str, ...] = (),
    not_applicable_page_types: tuple[str, ...] = (),
    applicability_mode: str | None = None,
    applicability_catalog_version: str | None = None,
    applicability_catalog_hash: str | None = None,
    target_platform: TaskTargetPlatform = TaskTargetPlatform.H5,
    browser_profile_id: str | None = None,
    adult_self_attestation: AdultSelfAttestation | None = None,
    run_status: str | None = None,
) -> WorkstationTaskDetail:
    return WorkstationTaskDetail(
        task_id="task-old",
        task_name="首页质检",
        product=ProductSnapshot("质量部", "H5-001", "首页"),
        target_url="https://example.com",
        target_platform=target_platform,
        execution_mode=execution_mode,
        journey_steps=["首页"],
        status=status,
        progress=0,
        report_status="not_started",
        report_availability="none",
        failure_reason=None,
        available_actions=[],
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
        started_at=None,
        finished_at=None,
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        run_id="run-old",
        workflow_version=workflow_version,
        required_page_types=required_page_types,
        inspection_scope_page_types=inspection_scope_page_types,
        optional_page_types=optional_page_types,
        not_applicable_page_types=not_applicable_page_types,
        applicability_mode=applicability_mode,
        applicability_catalog_version=applicability_catalog_version,
        applicability_catalog_hash=applicability_catalog_hash,
        browser_profile_id=browser_profile_id,
        adult_self_attestation=adult_self_attestation,
        run_status=run_status,
    )
