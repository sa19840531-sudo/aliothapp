from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import tasks as tasks_module
from services.api.app.application.h5_exploration_cancellation import (
    H5CancellationCheckpoint,
    H5ExplorationCanceled,
    RedisH5CancellationPort,
)
from services.api.app.application.task_submission import (
    CreatedTask,
    RunCancellationOutcome,
    TaskSubmissionResult,
    TaskSubmissionService,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import (
    AdultSelfAttestation,
    TaskTarget,
    TaskTargetPlatform,
    WebTaskConfig,
)
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import (
    InMemoryAgentRunStateStore,
    NoopAgentRunStateStore,
)
from services.api.app.main import create_app

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


class FakeTaskRepository:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_task(
        self,
        *,
        access_scope: AccessScope,
        target: TaskTarget,
        ruleset_code: str,
        ruleset_version: int,
        channel: str,
        priority: int,
        task_config: dict,
        page_sampling_policy: dict,
        loop_guard: dict,
    ) -> CreatedTask:
        task_id = str(uuid4())
        ruleset_id = str(uuid4())
        self.created.append(
            {
                "access_scope": access_scope,
                "task_id": task_id,
                "target": target,
                "target_url": target.target_url,
                "ruleset_code": ruleset_code,
                "ruleset_version": ruleset_version,
                "channel": channel,
                "priority": priority,
                "task_config": task_config,
                "page_sampling_policy": page_sampling_policy,
                "loop_guard": loop_guard,
            }
        )
        return CreatedTask(
            task_id=task_id,
            ruleset_id=ruleset_id,
            target=target,
            status="queued",
            task_config=task_config,
            page_sampling_policy=page_sampling_policy,
            loop_guard=loop_guard,
        )


async def test_task_submission_service_creates_task_and_enqueues_bound_agent_run() -> None:
    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    state_store = InMemoryAgentRunStateStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )

    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com/h5",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=80,
        task_config={
            "browser": "chromium",
            "submitter_id": "spoofed-user",
            "tenant_id": "spoofed-tenant",
        },
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
        loop_guard={"max_steps": 20},
    )

    assert result.task_id == repository.created[0]["task_id"]
    assert repository.created[0]["access_scope"] is TEST_ACCESS_SCOPE
    assert result.status == "queued"
    queued_run_id = await queue.dequeue(timeout=0.01)
    assert queued_run_id == result.run_id
    queued_record = await queue.get(result.run_id)
    assert queued_record is not None
    assert queued_record.task_id == result.task_id
    assert queued_record.submitted_by_user_id == TEST_ACCESS_SCOPE.actor_user_id
    assert queued_record.tenant_id == TEST_ACCESS_SCOPE.tenant_id
    assert "https://example.com/h5" in queued_record.message
    assert repository.created[0]["task_config"] == {
        "browser": "chromium",
        "workflow_version": "v1",
    }
    assert queued_record.task_config == {
        "browser": "chromium",
        "workflow_version": "v1",
        "_task_target": {
            "platform": "h5",
            "target_url": "https://example.com/h5",
            "app": None,
        },
        "_trusted_target_url": "https://example.com/h5",
    }
    assert queued_record.page_sampling_policy == {"mode": "first_n", "max_pages": 3}
    assert queued_record.loop_guard == {"max_steps": 20}
    assert '"max_pages": 3' in queued_record.message
    assert '"max_steps": 20' in queued_record.message
    assert [event.event_type for event in state_store.events] == ["submitted"]
    assert state_store.events[0].snapshot is not None
    assert state_store.events[0].snapshot.task_id == result.task_id


@pytest.mark.asyncio
async def test_web_exploration_task_and_run_freeze_identical_target_snapshot() -> None:
    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        web_agent_run_queue=queue,
        state_store=InMemoryAgentRunStateStore(),
    )
    target = TaskTarget(
        platform=TaskTargetPlatform.WEB,
        target_url="https://example.com/",
        web=WebTaskConfig(
            browser_profile_id="desktop_fhd_1920x1080_v1",
            adult_self_attestation=AdultSelfAttestation(
                declared=True,
                statement_version="adult-v1",
                declared_by="deployment-owner",
                declared_at=datetime(2026, 9, 11, tzinfo=UTC),
            ),
        ),
    )

    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target=target,
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=80,
        task_config={"workflow_version": "v2", "execution_mode": "exploration_only"},
        page_sampling_policy={},
        loop_guard={},
    )

    queued_record = await queue.get(result.run_id)
    assert queued_record is not None
    assert repository.created[0]["task_config"]["_task_target"] == target.to_dict()
    assert queued_record.task_config["_task_target"] == target.to_dict()


async def test_task_submission_service_starts_run_idempotently() -> None:
    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=InMemoryAgentRunStateStore(),
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )

    assert await service.ensure_run_enqueued(result.run_id) is True
    assert await queue.dequeue(timeout=0.1) == result.run_id
    assert await queue.dequeue(timeout=0.1) is None


async def test_task_submission_service_cancels_queued_run_immediately() -> None:
    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    state_store = InMemoryAgentRunStateStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )

    assert await service.cancel_run(result.run_id) == RunCancellationOutcome.CANCELED
    assert await queue.dequeue(timeout=0.1) is None
    assert [event.event_type for event in state_store.events] == [
        "submitted",
        "cancel_requested",
        "finished",
    ]


async def test_queued_cancel_retries_durable_finish_after_postgres_recovers() -> None:
    class FlakyFinishStateStore(InMemoryAgentRunStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.finish_calls = 0

        async def record_finished(self, record) -> None:
            self.finish_calls += 1
            if self.finish_calls == 1:
                raise ConnectionError("postgres temporarily unavailable")
            await super().record_finished(record)

    repository = FakeTaskRepository()
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    state_store = FlakyFinishStateStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )

    with pytest.raises(ConnectionError, match="postgres temporarily unavailable"):
        await service.cancel_run(result.run_id)

    interrupted = await queue.get(result.run_id)
    assert interrupted is not None
    assert interrupted.status is AgentRunQueueStatus.CANCELED
    assert interrupted.cancellation_cleanup_completed is True
    assert interrupted.cancellation_finalized is False

    assert await service.cancel_run(result.run_id) is RunCancellationOutcome.CANCELED

    recovered = await queue.get(result.run_id)
    assert recovered is not None
    assert recovered.cancellation_cleanup_completed is True
    assert recovered.cancellation_finalized is True
    assert state_store.finish_calls == 2


async def test_task_submission_service_signals_running_run_cancellation() -> None:
    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    state_store = InMemoryAgentRunStateStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )
    assert await queue.dequeue(timeout=0.1) == result.run_id
    record = await queue.get(result.run_id)
    assert record is not None
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "worker-1"
    await queue.save(record)
    await queue.record_worker_heartbeat("worker-1")

    assert await service.cancel_run(result.run_id) == RunCancellationOutcome.CANCELING
    assert await queue.is_cancel_requested(result.run_id) is True
    assert [event.event_type for event in state_store.events] == [
        "submitted",
        "cancel_requested",
    ]


async def test_durable_cancel_stops_h5_when_redis_run_record_is_missing() -> None:
    """Redis 短期记录丢失后，H5 仍须读取已接受的 PostgreSQL 取消事实。"""

    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    state_store = InMemoryAgentRunStateStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )
    assert await queue.dequeue(timeout=0.1) == result.run_id
    await redis_client.delete(f"alioth:agent-runs:{result.run_id}")

    assert await service.cancel_run(result.run_id) == RunCancellationOutcome.CANCELING
    assert await queue.is_cancel_requested(result.run_id) is False

    checkpoint = H5CancellationCheckpoint(
        RedisH5CancellationPort(queue, durable_source=state_store)
    )
    with pytest.raises(H5ExplorationCanceled):
        await checkpoint.raise_if_requested(
            task_id=result.task_id,
            agent_run_id=result.run_id,
            stage="before_action",
        )


async def test_running_cancel_does_not_publish_redis_signal_when_durable_rejects() -> None:
    """完成已先线性化时，旧 Redis 信号不得让 Worker 再把 Run 投影为取消。"""

    class RejectingCancelStore(InMemoryAgentRunStateStore):
        async def record_cancel_requested(self, run_id: str) -> bool:
            await super().record_cancel_requested(run_id)
            return False

    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=RejectingCancelStore(),
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )
    assert await queue.dequeue(timeout=0.1) == result.run_id
    record = await queue.get(result.run_id)
    assert record is not None
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "worker-1"
    await queue.save(record)
    await queue.record_worker_heartbeat("worker-1")

    assert await service.cancel_run(result.run_id) is None
    assert await queue.is_cancel_requested(result.run_id) is False


async def test_task_submission_service_terminalizes_orphaned_running_run() -> None:
    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    state_store = InMemoryAgentRunStateStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )
    assert await queue.dequeue(timeout=0.1) == result.run_id
    record = await queue.get(result.run_id)
    assert record is not None
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-worker"
    await queue.save(record)

    outcome = await service.cancel_run(result.run_id)

    assert outcome == RunCancellationOutcome.CANCELED
    persisted = await queue.get(result.run_id)
    assert persisted is not None
    assert persisted.status == AgentRunQueueStatus.CANCELED
    assert [event.event_type for event in state_store.events] == [
        "submitted",
        "cancel_requested",
        "finished",
    ]


async def test_orphan_cancel_persists_durable_marker_before_redis_mutation() -> None:
    """即使误判孤儿，活跃 Worker 也必须先能看见长期取消事实。"""

    order: list[str] = []

    class OrderedStore(InMemoryAgentRunStateStore):
        async def record_cancel_requested(self, run_id: str) -> bool:
            order.append("durable")
            return await super().record_cancel_requested(run_id)

    class OrderedQueue(RedisAgentRunQueue):
        async def cancel_queued(self, run_id: str):
            order.append("queued_probe")
            return await super().cancel_queued(run_id)

        async def cancel_orphaned_running(self, run_id: str):
            order.append("orphan_redis_mutation")
            return await super().cancel_orphaned_running(run_id)

    repository = FakeTaskRepository()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = OrderedQueue(redis_client, ttl_seconds=3600)
    state_store = OrderedStore()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=queue,
        state_store=state_store,
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )
    assert await queue.dequeue(timeout=0.1) == result.run_id
    record = await queue.get(result.run_id)
    assert record is not None
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-worker"
    await queue.save(record)

    assert await service.cancel_run(result.run_id) is RunCancellationOutcome.CANCELED
    assert order == ["durable", "queued_probe", "orphan_redis_mutation"]


async def test_noop_store_does_not_accept_cancel_for_missing_run() -> None:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = TaskSubmissionService(
        task_repository=FakeTaskRepository(),
        agent_run_queue=RedisAgentRunQueue(redis_client, ttl_seconds=3600),
        state_store=NoopAgentRunStateStore(),
    )

    assert await service.cancel_run(str(uuid4())) is None


async def test_noop_store_still_cancels_existing_queued_run() -> None:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    service = TaskSubmissionService(
        task_repository=FakeTaskRepository(),
        agent_run_queue=queue,
        state_store=NoopAgentRunStateStore(),
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )

    assert await service.cancel_run(result.run_id) is RunCancellationOutcome.CANCELED
    assert await queue.dequeue(timeout=0.1) is None


async def test_noop_store_does_not_report_terminal_redis_record_as_canceling() -> None:
    """未启用长期存储时，Redis 已终态记录不得被误报为取消处理中。"""

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    service = TaskSubmissionService(
        task_repository=FakeTaskRepository(),
        agent_run_queue=queue,
        state_store=NoopAgentRunStateStore(),
    )
    result = await service.submit_task(
        access_scope=TEST_ACCESS_SCOPE,
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
    )
    record = await queue.get(result.run_id)
    assert record is not None
    record.status = AgentRunQueueStatus.COMPLETED
    await queue.save(record)

    assert await service.cancel_run(result.run_id) is None


@dataclass
class FakeTaskSubmissionService:
    result: TaskSubmissionResult
    calls: list[dict]

    async def submit_task(self, **kwargs) -> TaskSubmissionResult:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeTaskSubmissionService]:
    service = FakeTaskSubmissionService(
        result=TaskSubmissionResult(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            ruleset_id=str(uuid4()),
            status="queued",
        ),
        calls=[],
    )
    app = create_app()
    app.dependency_overrides[tasks_module._get_task_submission_service] = lambda: service
    return TestClient(app), service


def test_submit_task_endpoint_returns_task_and_run_ids(
    client: tuple[TestClient, FakeTaskSubmissionService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/tasks",
        json={
            "target_url": "https://example.com/h5",
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "channel": "workbench",
            "priority": 80,
            "task_config": {"browser": "chromium"},
            "page_sampling_policy": {"mode": "first_n", "max_pages": 3},
            "loop_guard": {"max_steps": 20},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "task_id": service.result.task_id,
        "run_id": service.result.run_id,
        "ruleset_id": service.result.ruleset_id,
        "status": "queued",
    }
    assert len(service.calls) == 1
    assert service.calls[0]["access_scope"].actor_user_id == "test-user"
    assert service.calls[0]["access_scope"].tenant_id == "tenant-1"
    call = {
        key: value for key, value in service.calls[0].items() if key != "access_scope"
    }
    assert call == {
        "target": TaskTarget(
            platform=TaskTargetPlatform.H5,
            target_url="https://example.com/h5",
        ),
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "ruleset_codes": None,
        "channel": "workbench",
        "priority": 80,
        "task_config": {"browser": "chromium"},
        "page_sampling_policy": {"mode": "first_n", "max_pages": 3},
        "loop_guard": {"max_steps": 20},
    }


def test_submit_task_endpoint_rejects_invalid_channel(
    client: tuple[TestClient, FakeTaskSubmissionService],
) -> None:
    test_client, _service = client

    response = test_client.post(
        "/api/v1/tasks",
        json={
            "target_url": "https://example.com/h5",
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "channel": "email",
        },
    )

    assert response.status_code == 422


def test_submit_task_endpoint_forwards_idempotency_key(
    client: tuple[TestClient, FakeTaskSubmissionService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "browser-request-1"},
        json={
            "target_url": "https://example.com/h5",
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
        },
    )

    assert response.status_code == 202
    assert service.calls[0]["idempotency_key"] == "browser-request-1"
