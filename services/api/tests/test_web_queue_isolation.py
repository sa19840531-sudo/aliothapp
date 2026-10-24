"""Web Agent Run 等待队列与现有 H5/App 队列的隔离测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis

from services.api.app.api.v1 import tasks as v1_tasks
from services.api.app.application.task_submission_delivery import (
    TaskSubmissionProjectionService,
)
from services.api.app.core.config import QueueSettings
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunRecord,
    RedisAgentRunQueue,
)

DEFAULT_QUEUE_KEY = "test:agent-runs:default"
WEB_QUEUE_KEY = "test:agent-runs:web-v2"


def test_v1_task_service_composition_injects_dedicated_web_queue(monkeypatch) -> None:
    """工作台取消与查询复用 V1 组合时，也必须能路由到 Web 专用队列。"""

    built_queue_keys: list[str] = []

    class FakeQueue:
        def __init__(self, redis_client, *, ttl_seconds: int, queue_key: str) -> None:
            del redis_client, ttl_seconds
            self.queue_key = queue_key
            built_queue_keys.append(queue_key)

    class FakeTaskSubmissionService:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    settings = SimpleNamespace(
        queue=SimpleNamespace(
            run_record_ttl_seconds=60,
            agent_run_queue_key=DEFAULT_QUEUE_KEY,
            web_agent_run_queue_key=WEB_QUEUE_KEY,
        ),
        workflow=SimpleNamespace(app_qc_enabled=False, version="v1"),
    )
    repository = object()
    state_store = object()
    redis_client = object()

    monkeypatch.setattr(v1_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(v1_tasks, "build_redis_client", lambda queue: redis_client)
    monkeypatch.setattr(v1_tasks, "RedisAgentRunQueue", FakeQueue)
    monkeypatch.setattr(v1_tasks, "PostgresTaskRepository", lambda: repository)
    monkeypatch.setattr(v1_tasks, "build_agent_run_state_store", lambda: state_store)
    monkeypatch.setattr(v1_tasks, "TaskSubmissionService", FakeTaskSubmissionService)

    v1_tasks._get_task_submission_service.cache_clear()
    try:
        service = v1_tasks._get_task_submission_service()
    finally:
        v1_tasks._get_task_submission_service.cache_clear()

    assert built_queue_keys == [DEFAULT_QUEUE_KEY, WEB_QUEUE_KEY]
    assert service.kwargs["agent_run_queue"].queue_key == DEFAULT_QUEUE_KEY
    assert service.kwargs["web_agent_run_queue"].queue_key == WEB_QUEUE_KEY


def test_queue_settings_keep_legacy_key_and_define_web_key() -> None:
    settings = QueueSettings()

    assert settings.agent_run_queue_key == "alioth:agent-runs:queue"
    assert settings.web_agent_run_queue_key == "alioth:agent-runs:web-v2"


def _record(*, run_id: str, platform: str) -> AgentRunRecord:
    target_url = "https://example.test" if platform != "app" else None
    return AgentRunRecord(
        run_id=run_id,
        task_id=f"task-{platform}",
        message=f"{platform} task",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "_task_target": {
                "platform": platform,
                "target_url": target_url,
                "app": None,
                **(
                    {
                        "web": {
                            "browser_profile_id": "desktop_fhd_1920x1080_v1",
                        }
                    }
                    if platform == "web"
                    else {}
                ),
            },
        },
    )


async def test_queue_instances_use_independent_waiting_lists() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    default_queue = RedisAgentRunQueue(
        client,
        ttl_seconds=60,
        queue_key=DEFAULT_QUEUE_KEY,
    )
    web_queue = RedisAgentRunQueue(
        client,
        ttl_seconds=60,
        queue_key=WEB_QUEUE_KEY,
    )

    default_record = await default_queue.enqueue("h5")
    web_record = await web_queue.enqueue("web")

    assert await default_queue.dequeue(timeout=0.1) == default_record.run_id
    assert await default_queue.dequeue(timeout=0.1) is None
    assert await web_queue.dequeue(timeout=0.1) == web_record.run_id


async def test_submission_projection_routes_web_without_touching_default_queue() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    default_queue = RedisAgentRunQueue(
        client,
        ttl_seconds=60,
        queue_key=DEFAULT_QUEUE_KEY,
    )
    web_queue = RedisAgentRunQueue(
        client,
        ttl_seconds=60,
        queue_key=WEB_QUEUE_KEY,
    )
    record = _record(
        run_id="11111111-1111-1111-1111-111111111111",
        platform="web",
    )

    class Repository:
        async def load_pending_run(self, *, submission_id, task_id):
            del submission_id, task_id
            return record

        async def list_pending_runs(self, *, minimum_age_seconds, limit):
            del minimum_age_seconds, limit
            return (record,)

    service = TaskSubmissionProjectionService(
        repository=Repository(),
        queue=default_queue,
        web_queue=web_queue,
    )

    assert await service.project(submission_id="submission-web", task_id="task-web")
    assert await default_queue.queue_depth() == 0
    assert await web_queue.queue_depth() == 1


async def test_submission_projection_keeps_h5_on_default_queue() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    default_queue = RedisAgentRunQueue(
        client,
        ttl_seconds=60,
        queue_key=DEFAULT_QUEUE_KEY,
    )
    web_queue = RedisAgentRunQueue(
        client,
        ttl_seconds=60,
        queue_key=WEB_QUEUE_KEY,
    )
    record = _record(
        run_id="22222222-2222-2222-2222-222222222222",
        platform="h5",
    )

    class Repository:
        async def load_pending_run(self, *, submission_id, task_id):
            del submission_id, task_id
            return record

        async def list_pending_runs(self, *, minimum_age_seconds, limit):
            del minimum_age_seconds, limit
            return (record,)

    service = TaskSubmissionProjectionService(
        repository=Repository(),
        queue=default_queue,
        web_queue=web_queue,
    )

    assert await service.project(submission_id="submission-h5", task_id="task-h5")
    assert await default_queue.queue_depth() == 1
    assert await web_queue.queue_depth() == 0
