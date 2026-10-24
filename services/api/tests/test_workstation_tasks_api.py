from dataclasses import dataclass, replace
from datetime import UTC, datetime

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import tasks as workstation_tasks_module
from services.api.app.application.workstation_tasks import (
    ProductSnapshot,
    WebStageDurations,
    WorkstationTaskCreateCommand,
    WorkstationTaskDetail,
    WorkstationTaskListFilter,
    WorkstationTaskListItem,
    WorkstationTaskListResult,
    WorkstationTaskOperationResult,
    available_actions_for_status,
    report_status_from_task_status,
)
from services.api.app.core.config import QueueSettings, Settings
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import TaskExecutionMode, TaskTargetPlatform
from services.api.app.main import create_app


@dataclass
class FakeWorkstationTaskService:
    list_filter: WorkstationTaskListFilter | None = None
    create_command: WorkstationTaskCreateCommand | None = None
    create_idempotency_key: str | None = None
    operation_calls: list[tuple[str, str, str | None]] | None = None

    async def list_tasks(
        self,
        filters: WorkstationTaskListFilter,
        *,
        access_scope: AccessScope,
    ) -> WorkstationTaskListResult:
        self.list_filter = filters
        item = WorkstationTaskListItem(
            task_id="task-1",
            task_name="首页改版回归测试",
            product=ProductSnapshot(
                department_name="质量保障部",
                product_code="SSC-H5-001",
                product_name="移动首页",
            ),
            target_url="https://example.com/mobile/home",
            target_platform=TaskTargetPlatform.H5,
            execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
            journey_steps=["首页", "商品详情"],
            status="running",
            progress=72,
            report_status="generating",
            report_availability="none",
            failure_reason=None,
            available_actions=["detail", "cancel"],
            created_at=datetime(2026, 7, 10, 9, 15, tzinfo=UTC),
        )
        return WorkstationTaskListResult(
            items=[item],
            total=1,
            page=filters.page,
            page_size=filters.page_size,
        )

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope
    ) -> WorkstationTaskDetail | None:
        if task_id == "task-web-formal":
            return WorkstationTaskDetail(
                task_id="task-web-formal",
                task_name="Web 正式闭环",
                product=ProductSnapshot("质量保障部", "WEB-002", "桌面官网"),
                target_url="https://example.com",
                target_platform=TaskTargetPlatform.WEB,
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                journey_steps=[],
                status="done",
                progress=100,
                report_status="ready",
                report_availability="final",
                failure_reason=None,
                available_actions=["detail", "report"],
                created_at=datetime(2026, 9, 2, 9, 15, tzinfo=UTC),
                started_at=datetime(2026, 9, 2, 9, 16, tzinfo=UTC),
                finished_at=datetime(2026, 9, 2, 9, 20, tzinfo=UTC),
                ruleset_code="alioth_first_ruleset",
                ruleset_version=1,
                channel="workbench",
                priority=100,
                run_id="run-web-formal",
                workflow_version="v2",
                execution_status="completed_with_gaps",
                finalization_state="sealed",
                completion_gate_passed=True,
                report_available=True,
                coverage_status="completed_with_gaps",
                coverage_observed_page_types=("home", "content_list"),
                coverage_missing_page_types=("video_detail",),
                coverage_unrecognized_page_types=("web_unknown",),
                coverage_normalization_protocol_version="web-page-type-normalization.v1",
                stage_durations=WebStageDurations(
                    exploration_ms=120_000,
                    inspection_ms=90_000,
                    report_generation_ms=30_000,
                    total_ms=300_000,
                ),
            )
        if task_id == "task-web":
            return WorkstationTaskDetail(
                task_id="task-web",
                task_name="Web 官网探索",
                product=ProductSnapshot(
                    department_name="质量保障部",
                    product_code="WEB-001",
                    product_name="桌面官网",
                ),
                target_url="https://example.com",
                target_platform=TaskTargetPlatform.WEB,
                execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
                journey_steps=[],
                status="queued",
                progress=0,
                report_status="not_applicable",
                report_availability="none",
                failure_reason=None,
                available_actions=["detail", "cancel"],
                created_at=datetime(2026, 8, 30, 9, 15, tzinfo=UTC),
                started_at=None,
                finished_at=None,
                ruleset_code="alioth_first_ruleset",
                ruleset_version=1,
                channel="workbench",
                priority=100,
                run_id="run-web",
                scan_mode="full",
                remark=None,
                browser_profile_id="desktop_fhd_1920x1080_v1",
                viewport_width=1920,
                viewport_height=1080,
            )
        if task_id != "task-1":
            return None
        detail = WorkstationTaskDetail(
            task_id="task-1",
            task_name="首页改版回归测试",
            product=ProductSnapshot(
                department_name="质量保障部",
                product_code="SSC-H5-001",
                product_name="移动首页",
            ),
            target_url="https://example.com/mobile/home",
            target_platform=TaskTargetPlatform.H5,
            execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
            journey_steps=["首页", "商品详情"],
            status="running",
            progress=72,
            report_status="generating",
            report_availability="none",
            failure_reason=None,
            available_actions=["detail", "cancel"],
            created_at=datetime(2026, 7, 10, 9, 15, tzinfo=UTC),
            started_at=datetime(2026, 7, 10, 9, 16, tzinfo=UTC),
            finished_at=None,
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            run_id="run-1",
            scan_mode="quick",
            remark="双十一活动页重点巡检",
            browser_profile_id="desktop_fhd_1920x1080_v1",
        )
        return detail

    async def create_task(
        self,
        command: WorkstationTaskCreateCommand,
        *,
        access_scope,
        idempotency_key: str | None = None,
    ) -> WorkstationTaskOperationResult:
        del access_scope
        self.create_command = command
        self.create_idempotency_key = idempotency_key
        return WorkstationTaskOperationResult(
            task_id="task-created",
            status="queued",
            run_id="run-created",
            message="任务已提交",
        )

    async def perform_operation(
        self,
        task_id: str,
        operation: str,
        *,
        access_scope,
        idempotency_key: str | None = None,
    ) -> WorkstationTaskOperationResult:
        del access_scope
        if self.operation_calls is None:
            self.operation_calls = []
        self.operation_calls.append((task_id, operation, idempotency_key))
        return WorkstationTaskOperationResult(
            task_id=task_id,
            status="running",
            run_id="run-1",
            message="任务操作已提交",
        )


@pytest.fixture
def client() -> tuple[TestClient, FakeWorkstationTaskService]:
    service = FakeWorkstationTaskService()
    app = create_app()
    app.dependency_overrides[workstation_tasks_module._get_workstation_task_service] = lambda: (
        service
    )
    return TestClient(app), service


@pytest.mark.parametrize("platform", list(TaskTargetPlatform))
async def test_task_service_factory_observes_platform_queue_for_actual_queued_detail(
    monkeypatch, platform,
) -> None:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(
        queue=QueueSettings(
            agent_run_queue_key="test:api-default-queue",
            web_agent_run_queue_key="test:api-web-queue",
        )
    )
    source_detail = await FakeWorkstationTaskService().get_task_detail(
        "task-1", access_scope=None
    )
    detail = replace(
        source_detail, target_platform=platform, status="queued", execution_phase="pending"
    )

    class Repository:
        async def get_task_detail(self, task_id, *, access_scope):
            return detail

    monkeypatch.setattr(workstation_tasks_module, "get_settings", lambda: settings)
    monkeypatch.setattr(workstation_tasks_module, "build_redis_client", lambda _: redis_client)
    monkeypatch.setattr(workstation_tasks_module, "PostgresWorkstationQueryRepository", Repository)
    monkeypatch.setattr(workstation_tasks_module, "_get_task_submission_service", lambda: None)
    workstation_tasks_module._get_workstation_task_service.cache_clear()
    try:
        await redis_client.rpush("test:api-default-queue", "h5-ahead", "app-ahead", "run-1")
        await redis_client.rpush("test:api-web-queue", "run-1")
        service = workstation_tasks_module._get_workstation_task_service()

        result = await service.get_task_detail("task-1", access_scope=None)

        assert result.queue_position == (1 if platform is TaskTargetPlatform.WEB else 3)
        assert result.tasks_ahead == (0 if platform is TaskTargetPlatform.WEB else 2)
        assert result.available_worker_count == 0
    finally:
        workstation_tasks_module._get_workstation_task_service.cache_clear()
        await redis_client.aclose()


def test_list_workstation_tasks_uses_page_filters(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, service = client

    response = test_client.get(
        "/api/v1/workstation/tasks"
        "?task_name=首页"
        "&product_name=移动首页"
        "&product_code=SSC-H5-001"
        "&target_url=mobile"
        "&status=running"
        "&report_status=generating"
        "&created_from=2026-07-10T00:00:00%2B08:00"
        "&created_to=2026-07-10T23:59:59%2B08:00"
        "&page=2"
        "&page_size=10"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["task_name"] == "首页改版回归测试"
    assert body["items"][0]["product"]["product_code"] == "SSC-H5-001"
    assert body["items"][0]["available_actions"] == ["detail", "cancel"]
    assert body["items"][0]["target_platform"] == "h5"
    assert body["items"][0]["execution_mode"] == "exploration_only"
    assert body["items"][0]["report_availability"] == "none"
    assert body["total"] == 1
    assert body["page"] == 2
    assert body["page_size"] == 10
    assert service.list_filter == WorkstationTaskListFilter(
        task_name="首页",
        product_name="移动首页",
        product_code="SSC-H5-001",
        target_url="mobile",
        status="running",
        report_status="generating",
        created_from=datetime(2026, 7, 9, 16, 0, tzinfo=UTC),
        created_to=datetime(2026, 7, 10, 15, 59, 59, tzinfo=UTC),
        page=2,
        page_size=10,
    )


def test_get_workstation_task_detail_returns_page_shape(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, _service = client

    response = test_client.get("/api/v1/workstation/tasks/task-1")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "task-1"
    assert body["task_name"] == "首页改版回归测试"
    assert body["progress"] == 72
    assert body["report_status"] == "generating"
    assert body["ruleset_code"] == "alioth_first_ruleset"
    assert body["scan_mode"] == "quick"
    assert body["remark"] == "双十一活动页重点巡检"
    assert body["target_platform"] == "h5"
    assert body["execution_mode"] == "exploration_only"
    assert body["required_page_types"] == []
    assert body["browser_profile_id"] == "desktop_fhd_1920x1080_v1"
    assert "stage_durations" not in body


def test_get_web_task_detail_returns_frozen_browser_profile(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, _service = client

    response = test_client.get("/api/v1/workstation/tasks/task-web")

    assert response.status_code == 200
    body = response.json()
    assert body["target_platform"] == "web"
    assert body["browser_profile_id"] == "desktop_fhd_1920x1080_v1"
    assert body["viewport_width"] == 1920
    assert body["viewport_height"] == 1080


def test_get_web_formal_task_detail_returns_authoritative_closure_projection(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, _service = client

    response = test_client.get("/api/v1/workstation/tasks/task-web-formal")

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_version"] == "v2"
    assert body["execution_status"] == "completed_with_gaps"
    assert body["finalization_state"] == "sealed"
    assert body["completion_gate_passed"] is True
    assert body["report_available"] is True
    assert body["report_availability"] == "final"
    assert body["coverage_status"] == "completed_with_gaps"
    assert body["coverage_observed_page_types"] == ["home", "content_list"]
    assert body["coverage_missing_page_types"] == ["video_detail"]
    assert body["coverage_unrecognized_page_types"] == ["web_unknown"]
    assert body["coverage_normalization_protocol_version"] == (
        "web-page-type-normalization.v1"
    )
    assert body["stage_durations"] == {
        "exploration_ms": 120_000,
        "inspection_ms": 90_000,
        "report_generation_ms": 30_000,
        "total_ms": 300_000,
    }


def test_create_workstation_task_maps_page_payload(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/workstation/tasks",
        headers={"Idempotency-Key": "workstation-create-key"},
        json={
            "task_name": "双十一主会场页面质检",
            "target_url": "https://example.com/mobile/home",
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "channel": "workbench",
            "priority": 100,
            "target_platform": "h5",
            "execution_mode": "exploration_only",
            "required_page_types": ["home", "content_list"],
            "product": {
                "department_name": "营销中台",
                "product_code": "MKT-CAMP-618",
                "product_name": "活动主会场",
            },
            "scan_mode": "full",
            "journey_steps": ["活动首页", "优惠券页"],
            "remark": "活动前全量质检",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] == "task-created"
    assert body["status"] == "queued"
    assert service.create_command is not None
    assert service.create_command.task_name == "双十一主会场页面质检"
    assert service.create_command.product.product_code == "MKT-CAMP-618"
    assert service.create_command.target_platform == TaskTargetPlatform.H5
    assert service.create_command.execution_mode is TaskExecutionMode.EXPLORATION_ONLY
    assert service.create_command.required_page_types == ("home", "content_list")
    assert service.create_command.journey_steps == ["活动首页", "优惠券页"]
    assert service.create_idempotency_key == "workstation-create-key"


def test_task_operation_endpoint_calls_service(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/workstation/tasks/task-1/start",
        headers={"Idempotency-Key": "workstation-start-key"},
    )

    assert response.status_code == 202
    assert response.json()["message"] == "任务操作已提交"
    assert service.operation_calls == [("task-1", "start", "workstation-start-key")]


def test_existing_task_operation_endpoint_accepts_admin_finalization_retry(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/workstation/tasks/task-1/retry-finalization",
        headers={"Idempotency-Key": "retry-finalization-key"},
    )

    assert response.status_code == 202
    assert service.operation_calls == [
        ("task-1", "retry-finalization", "retry-finalization-key")
    ]


def test_task_operation_endpoint_accepts_reexploration(
    client: tuple[TestClient, FakeWorkstationTaskService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/workstation/tasks/task-web/exploration",
        headers={"Idempotency-Key": "workstation-exploration-key"},
    )

    assert response.status_code == 202
    assert service.operation_calls == [
        ("task-web", "exploration", "workstation-exploration-key")
    ]


@pytest.mark.parametrize("total_rules", [0, 73])
@pytest.mark.parametrize(
    "execution_mode", [TaskExecutionMode.FULL_INSPECTION, TaskExecutionMode.EXPLORATION_ONLY],
)
def test_canceled_task_without_report_is_not_started(total_rules, execution_mode) -> None:
    assert report_status_from_task_status(
        "canceled", total_rules=total_rules, execution_mode=execution_mode,
    ) == "not_started"


def test_exploration_only_completed_report_is_not_applicable() -> None:
    report_status = report_status_from_task_status(
        "completed",
        total_rules=0,
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )
    actions = available_actions_for_status(
        "completed",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )

    assert report_status == "not_applicable"
    assert "report" not in actions
    assert {"detail", "exploration", "evidence"}.issubset(actions)


@pytest.mark.parametrize(
    "execution_mode", [TaskExecutionMode.FULL_INSPECTION, TaskExecutionMode.EXPLORATION_ONLY]
)
def test_task_actions_are_isolated_from_caller_mutation(execution_mode: TaskExecutionMode) -> None:
    first = available_actions_for_status("done", execution_mode=execution_mode)
    expected = first.copy()
    first.append("delete")
    first.remove("detail")

    second = available_actions_for_status("done", execution_mode=execution_mode)

    assert second == expected
    assert second is not first
