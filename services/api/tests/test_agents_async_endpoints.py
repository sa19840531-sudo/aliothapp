"""Agent 异步运行接口测试：/agents/runs 系列端点（提交/查询/取消），基于 fakeredis
替换真实 Redis 依赖，不发起真实网络调用、不要求本地跑 Redis 服务。
"""

from datetime import UTC, datetime

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import agents as agents_module
from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.auth import AuthUserResponse
from services.api.app.infrastructure.db.object_access_repository import ObjectAccessAction
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> RedisAgentRunQueue:
    """用 fakeredis 支撑的队列替换 agents.py 里的单例构建函数，测试间互不干扰。"""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    fake_queue = RedisAgentRunQueue(client, ttl_seconds=3600)
    monkeypatch.setattr(agents_module, "_get_agent_run_queue", lambda: fake_queue)
    return fake_queue


@pytest.fixture
def state_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryAgentRunStateStore:
    """用内存态长期状态 Store 替换默认 no-op 实现，验证 API 生命周期写入。"""
    store = InMemoryAgentRunStateStore()
    monkeypatch.setattr(agents_module, "_get_agent_run_state_store", lambda: store)
    return store


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_submit_run_returns_202_with_run_id_and_queued_status(
    client: TestClient, queue: RedisAgentRunQueue, state_store: InMemoryAgentRunStateStore
) -> None:
    response = client.post("/api/v1/agents/runs", json={"message": "帮我质检这通电话"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"]
    assert [event.event_type for event in state_store.events] == ["submitted"]
    assert state_store.events[0].snapshot is not None
    assert state_store.events[0].snapshot.run_id == body["run_id"]
    assert state_store.events[0].snapshot.status == AgentRunQueueStatus.QUEUED
    assert state_store.events[0].snapshot.submitted_by_user_id == "test-user"
    assert state_store.events[0].snapshot.tenant_id == "tenant-1"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/agents/runs"),
        ("get", "/api/v1/agents/runs/run-1"),
        ("post", "/api/v1/agents/runs/run-1/cancel"),
    ],
)
def test_async_agent_run_endpoints_require_login(method: str, path: str) -> None:
    app = create_app()
    pop_test_auth(app)
    client = TestClient(app)

    response = client.request(
        method,
        path,
        json={"message": "hello"} if path.endswith("/runs") else None,
    )

    assert response.status_code == 401


def test_submit_run_rejects_empty_message(client: TestClient, queue: RedisAgentRunQueue) -> None:
    response = client.post("/api/v1/agents/runs", json={"message": ""})
    assert response.status_code == 422


def test_submit_run_rejects_overly_long_message(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    response = client.post("/api/v1/agents/runs", json={"message": "x" * 10001})
    assert response.status_code == 422


def test_get_run_returns_full_status_for_freshly_queued_run(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    submit_response = client.post("/api/v1/agents/runs", json={"message": "hello"})
    run_id = submit_response.json()["run_id"]

    response = client.get(f"/api/v1/agents/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["status"] == "queued"
    assert body["submitted_at"] is not None
    assert body["started_at"] is None
    assert body["final_text"] is None
    assert body["action_trace"] == []
    assert body["working_memory"] == {}


def test_get_run_returns_404_for_unknown_run_id(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    response = client.get("/api/v1/agents/runs/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AliothError"


class AllowTaskAccess:
    async def can_access_task(self, *, task_id, scope, action=ObjectAccessAction.READ):
        del task_id, scope, action
        return True


async def test_get_run_falls_back_to_durable_snapshot_when_redis_record_expired(
    client: TestClient,
    queue: RedisAgentRunQueue,
    state_store: InMemoryAgentRunStateStore,
) -> None:
    """Redis TTL 过期后，绑定任务 Run 仍可从长期状态读取状态与动作留痕。"""

    del queue
    run_id = "11111111-1111-1111-1111-111111111111"
    record = AgentRunRecord(
        run_id=run_id,
        task_id="task-1",
        message="持久化任务",
        submitted_at=datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 7, 6, 10, 1, tzinfo=UTC),
        finished_at=datetime(2026, 7, 6, 10, 2, tzinfo=UTC),
        status=AgentRunQueueStatus.COMPLETED,
        termination_reason="completed",
        action_trace=[
            {
                "step": 1,
                "tool_name": "page_capture_tool",
                "arguments": '{"mode": "goto"}',
                "status": "ok",
                "duration_seconds": 0.12,
                "error_message": None,
                "retry_count": 0,
            }
        ],
    )
    await state_store.record_finished(record)
    client.app.dependency_overrides[agents_module._get_object_access_repository] = AllowTaskAccess

    response = client.get(f"/api/v1/agents/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["status"] == "completed"
    assert body["action_trace"][0]["tool_name"] == "page_capture_tool"


async def test_get_run_backfills_empty_redis_action_trace_from_durable_snapshot(
    client: TestClient,
    queue: RedisAgentRunQueue,
    state_store: InMemoryAgentRunStateStore,
) -> None:
    """Redis 运行快照存在但动作轨迹为空时，用长期状态补齐动作留痕。"""

    record = await queue.enqueue(
        "绑定任务",
        task_id="task-1",
        submitted_by_user_id="test-user",
        tenant_id="tenant-1",
    )
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = datetime(2026, 7, 6, 10, 1, tzinfo=UTC)
    await queue.save(record)
    durable = record.model_copy(deep=True)
    durable.action_trace = [
        {
            "step": 1,
            "tool_name": "candidate_rules_tool",
            "arguments": '{"page_type": "home"}',
            "status": "ok",
            "duration_seconds": 0.03,
            "error_message": None,
            "retry_count": 0,
        }
    ]
    await state_store.record_started(durable)
    client.app.dependency_overrides[agents_module._get_object_access_repository] = AllowTaskAccess

    response = client.get(f"/api/v1/agents/runs/{record.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["action_trace"][0]["tool_name"] == "candidate_rules_tool"


async def test_get_run_reflects_completed_state_written_by_worker(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    """模拟 Worker 进程写回的完成态记录，验证查询端点的序列化/字段透传正确。"""
    record = await queue.enqueue(
        "质检任务",
        submitted_by_user_id="test-user",
        tenant_id="tenant-1",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.started_at = datetime.now(UTC)
    record.finished_at = datetime.now(UTC)
    record.termination_reason = "completed"
    record.final_text = "质检完成，未发现异常"
    record.step_count = 3
    record.usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    record.action_trace = [
        {
            "step": 1,
            "tool_name": "echo_tool",
            "arguments": '{"x": "a"}',
            "status": "ok",
            "duration_seconds": 0.01,
            "error_message": None,
            "retry_count": 0,
        }
    ]
    record.working_memory = {
        "schema_version": 1,
        "current_page": None,
        "inspected_page_count": 1,
    }
    await queue.save(record)

    response = client.get(f"/api/v1/agents/runs/{record.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["final_text"] == "质检完成，未发现异常"
    assert body["step_count"] == 3
    assert body["usage"] == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    assert len(body["action_trace"]) == 1
    assert body["action_trace"][0]["tool_name"] == "echo_tool"
    assert body["working_memory"]["inspected_page_count"] == 1


async def test_get_run_accepts_running_action_trace_without_duration(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    """运行中 Action Trace 可能尚未写入耗时，查询接口不得因此返回 500。"""

    record = await queue.enqueue(
        "质检任务",
        submitted_by_user_id="test-user",
        tenant_id="tenant-1",
    )
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = datetime.now(UTC)
    record.action_trace = [
        {
            "step": 1,
            "tool_name": "h5_exploration_work",
            "arguments": "{}",
            "status": "running",
            "duration_seconds": None,
            "error_message": None,
            "retry_count": 0,
        }
    ]
    await queue.save(record)

    response = client.get(f"/api/v1/agents/runs/{record.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["action_trace"][0]["duration_seconds"] is None


async def test_cancel_run_succeeds_for_queued_run(
    client: TestClient, queue: RedisAgentRunQueue, state_store: InMemoryAgentRunStateStore
) -> None:
    """Direct Run 取消只写 Redis 取消标记，不写任务长期状态。"""

    submit_response = client.post("/api/v1/agents/runs", json={"message": "hello"})
    run_id = submit_response.json()["run_id"]
    state_store.events.clear()

    response = client.post(f"/api/v1/agents/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "cancel_requested": True}
    assert state_store.events == []
    assert await queue.is_cancel_requested(run_id) is True


async def test_cancel_task_bound_run_records_durable_cancellation(
    client: TestClient,
    queue: RedisAgentRunQueue,
    state_store: InMemoryAgentRunStateStore,
) -> None:
    """绑定任务的 Run 取消必须继续写长期取消事实。"""

    record = await queue.enqueue(
        "绑定任务",
        task_id="task-1",
        submitted_by_user_id="test-user",
        tenant_id="tenant-1",
    )
    state_store.events.clear()
    client.app.dependency_overrides[agents_module._get_object_access_repository] = AllowTaskAccess

    response = client.post(f"/api/v1/agents/runs/{record.run_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"run_id": record.run_id, "cancel_requested": True}
    assert [event.event_type for event in state_store.events] == ["cancel_requested"]
    assert state_store.events[0].run_id == record.run_id


def test_cancel_run_returns_404_for_unknown_run_id(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    response = client.post("/api/v1/agents/runs/does-not-exist/cancel")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AliothError"


async def test_cancel_run_returns_404_for_already_completed_run(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    record = await queue.enqueue(
        "hello",
        submitted_by_user_id="test-user",
        tenant_id="tenant-1",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    await queue.save(record)

    response = client.post(f"/api/v1/agents/runs/{record.run_id}/cancel")

    assert response.status_code == 404


async def test_direct_run_cross_owner_is_hidden_and_cannot_be_canceled(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    """普通 Agent 权限不能通过 run_id 探测或取消其他用户的 direct Run。"""

    record = await queue.enqueue(
        "他人的调试运行",
        submitted_by_user_id="other-user",
        tenant_id="tenant-1",
    )
    run_id = record.run_id

    assert client.get(f"/api/v1/agents/runs/{run_id}").status_code == 404
    assert client.post(f"/api/v1/agents/runs/{run_id}/cancel").status_code == 404
    assert await queue.is_cancel_requested(run_id) is False


async def test_direct_run_same_owner_from_other_tenant_is_hidden(
    client: TestClient, queue: RedisAgentRunQueue
) -> None:
    """Direct Run 同时绑定 owner 与 tenant，不能只命中相同 user_id。"""

    record = await queue.enqueue(
        "旧租户运行",
        submitted_by_user_id="test-user",
        tenant_id="tenant-2",
    )

    assert client.get(f"/api/v1/agents/runs/{record.run_id}").status_code == 404
    assert client.post(f"/api/v1/agents/runs/{record.run_id}/cancel").status_code == 404


async def test_direct_run_historical_missing_owner_requires_any_permission(
    queue: RedisAgentRunQueue,
) -> None:
    """submitted_by_user_id 缺失的历史记录只允许显式跨主体权限访问。"""

    record = await queue.enqueue("历史 direct Run")
    run_id = record.run_id
    app = create_app()
    app.dependency_overrides[agents_module._get_agent_run_queue] = lambda: queue
    app.dependency_overrides[auth_module.require_auth_user] = lambda: AuthUserResponse(
        id="reader",
        username="reader",
        full_name="Reader",
        role="审计员",
        status="enabled",
        permissions=["agentRuns"],
        tenant_id="tenant-1",
    )
    reader_client = TestClient(app)
    assert reader_client.get(f"/api/v1/agents/runs/{run_id}").status_code == 404

    app.dependency_overrides[auth_module.require_auth_user] = lambda: AuthUserResponse(
        id="admin",
        username="admin",
        full_name="Admin",
        role="平台管理员",
        status="enabled",
        permissions=["agentRuns", "taskObjects:read:any"],
        tenant_id="tenant-2",
    )
    admin_client = TestClient(app)
    assert admin_client.get(f"/api/v1/agents/runs/{run_id}").status_code == 200
    assert admin_client.post(f"/api/v1/agents/runs/{run_id}/cancel").status_code == 404

    app.dependency_overrides[auth_module.require_auth_user] = lambda: AuthUserResponse(
        id="admin",
        username="admin",
        full_name="Admin",
        role="平台管理员",
        status="enabled",
        permissions=["agentRuns", "taskObjects:read:any", "taskObjects:write:any"],
        tenant_id="tenant-2",
    )
    assert admin_client.post(f"/api/v1/agents/runs/{run_id}/cancel").status_code == 200


async def test_task_bound_run_uses_task_object_access_for_read_and_write(
    queue: RedisAgentRunQueue,
) -> None:
    """绑定业务任务的 Run 不使用 Redis owner 快照，读写均复核任务归属。"""

    class DenyTaskAccess:
        async def can_access_task(self, *, task_id, scope, action=ObjectAccessAction.READ):
            del task_id, scope, action
            return False

    record = await queue.enqueue(
        "绑定任务",
        task_id="task-1",
        submitted_by_user_id="test-user",
        tenant_id="tenant-1",
    )
    run_id = record.run_id
    app = create_app()
    app.dependency_overrides[agents_module._get_agent_run_queue] = lambda: queue
    app.dependency_overrides[agents_module._get_object_access_repository] = DenyTaskAccess
    denied_client = TestClient(app)

    assert denied_client.get(f"/api/v1/agents/runs/{run_id}").status_code == 404
    assert denied_client.post(f"/api/v1/agents/runs/{run_id}/cancel").status_code == 404
