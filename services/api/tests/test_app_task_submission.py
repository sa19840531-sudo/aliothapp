from __future__ import annotations

import json
from dataclasses import dataclass

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import tasks as tasks_module
from services.api.app.application import task_submission as submission
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import AppTaskConfig, TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
from services.api.app.main import create_app

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


class RecordingTaskRepository:
    def __init__(self) -> None:
        self.targets: list[TaskTarget] = []

    async def create_task(self, *, target: TaskTarget, **kwargs):
        self.targets.append(target)
        return submission.CreatedTask(
            task_id="task-1",
            ruleset_id="ruleset-1",
            target=target,
            status="queued",
            task_config=kwargs["task_config"],
            page_sampling_policy=kwargs["page_sampling_policy"],
            loop_guard=kwargs["loop_guard"],
        )


def _app_target() -> TaskTarget:
    return TaskTarget(
        platform=TaskTargetPlatform.APP,
        app=AppTaskConfig(
            platform_build_ref="app-build:00000000-0000-0000-0000-000000000001",
            package_name="com.example.target",
        ),
    )


async def test_submission_rejects_app_target_when_feature_flag_is_off() -> None:
    repository = RecordingTaskRepository()
    service = submission.TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=RedisAgentRunQueue(
            fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=3600,
        ),
        state_store=InMemoryAgentRunStateStore(),
        app_qc_enabled=False,
        default_workflow_version="v2",
    )

    with pytest.raises(submission.AppQcDisabledError, match="App 质检未启用"):
        await service.submit_task(
            access_scope=TEST_ACCESS_SCOPE,
            target=_app_target(),
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="api",
            priority=100,
            task_config={},
            page_sampling_policy={},
            loop_guard={},
        )

    assert repository.targets == []


async def test_submission_enqueues_only_app_target_references() -> None:
    repository = RecordingTaskRepository()
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    service = submission.TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=InMemoryAgentRunStateStore(),
        app_qc_enabled=True,
        default_workflow_version="v2",
    )

    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target=_app_target(),
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="api",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )

    record = await queue.get(result.run_id)
    assert record is not None
    assert record.task_config["workflow_version"] == "v2"
    assert record.task_config["_task_target"] == {
        "platform": "app",
        "target_url": None,
        "app": {
            "platform_build_ref": "app-build:00000000-0000-0000-0000-000000000001",
            "package_name": "com.example.target",
            "device": {"backend": "cloud_phone", "profile_id": "default"},
        },
    }
    serialized = json.dumps(record.task_config)
    assert "_trusted_target_url" not in record.task_config
    assert "storage_path" not in serialized
    assert "apk_base64" not in serialized


@dataclass
class RecordingSubmissionService:
    calls: list[dict]

    async def submit_task(self, **kwargs):
        self.calls.append(kwargs)
        return submission.TaskSubmissionResult(
            task_id="task-1",
            run_id="run-1",
            ruleset_id="ruleset-1",
            status="queued",
        )


def test_tasks_api_maps_app_payload_to_domain_target() -> None:
    service = RecordingSubmissionService(calls=[])
    app = create_app()
    app.dependency_overrides[tasks_module._get_task_submission_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/api/v1/tasks",
        json={
            "target_platform": "app",
            "target_url": None,
            "task_config": {
                "workflow_version": "v2",
                "app": {
                    "platform_build_ref": (
                        "app-build:00000000-0000-0000-0000-000000000001"
                    ),
                    "package_name": "com.example.target",
                    "device": {"backend": "cloud_phone", "profile_id": "default"},
                },
            },
        },
    )

    assert response.status_code == 202
    target = service.calls[0]["target"]
    assert target.platform is TaskTargetPlatform.APP
    assert target.target_url is None
    assert target.app is not None
    assert target.app.platform_build_ref.endswith("000000000001")
    assert "target_url" not in service.calls[0]


def test_tasks_api_rejects_app_without_platform_build_reference() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/api/v1/tasks",
        json={
            "target_platform": "app",
            "target_url": None,
            "task_config": {"workflow_version": "v2", "app": {}},
        },
    )

    assert response.status_code == 422
