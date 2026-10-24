"""系统状态可观测链路的测试：Worker 心跳 → Redis → 系统状态接口。"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app import worker as worker_module
from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.workstation import system as system_module
from services.api.app.application.delivery_operations import (
    DeliveryIncident,
    DeliveryOperationAction,
    DeliveryOperationConflictError,
    DeliveryOperationResult,
    DeliveryPlatform,
)
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.main import create_app


@pytest.fixture
def queue() -> RedisAgentRunQueue:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisAgentRunQueue(client, ttl_seconds=3600)


async def test_heartbeat_roundtrip_and_queue_depth(queue: RedisAgentRunQueue) -> None:
    assert await queue.read_worker_heartbeats() == []
    assert await queue.queue_depth() == 0

    await queue.record_worker_heartbeat("host-a:1", roles={"explorer"})
    await queue.record_worker_heartbeat("host-b:2", roles={"judge"})
    await queue.enqueue("质检任务")

    heartbeats = await queue.read_worker_heartbeats()
    assert {item["instance_id"] for item in heartbeats} == {"host-a:1", "host-b:2"}
    assert all(item["heartbeat_at"].tzinfo is not None for item in heartbeats)
    assert {role for item in heartbeats for role in item["roles"]} == {"explorer", "judge"}
    assert await queue.queue_depth() == 1


async def test_worker_heartbeat_loop_writes_and_stops(queue: RedisAgentRunQueue) -> None:
    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        worker_module._worker_heartbeat_loop(queue, stop_event, interval_seconds=0.05)
    )
    await asyncio.sleep(0.02)
    heartbeats = await queue.read_worker_heartbeats()
    assert len(heartbeats) == 1
    stop_event.set()
    await asyncio.wait_for(loop_task, timeout=1)


async def test_worker_heartbeat_loop_survives_redis_failure() -> None:
    class ExplodingQueue:
        async def record_worker_heartbeat(self, instance_id: str, **kwargs: Any) -> None:
            raise RuntimeError("redis down")

    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        worker_module._worker_heartbeat_loop(
            ExplodingQueue(), stop_event, interval_seconds=0.05
        )
    )
    await asyncio.sleep(0.02)
    stop_event.set()
    await asyncio.wait_for(loop_task, timeout=1)


class FakeInsightRepositoryForSystem:
    def __init__(self) -> None:
        self.overview: dict[str, Any] = {
            "queued_count": 1,
            "oldest_created_at": datetime(2026, 7, 17, 3, 46, tzinfo=UTC),
        }

    async def queued_task_overview(self) -> dict[str, Any]:
        return self.overview


class BrokenRedisQueue:
    async def read_worker_heartbeats(self) -> list[dict]:
        raise ConnectionError("redis unreachable")


def _delivery_incident() -> DeliveryIncident:
    return DeliveryIncident(
        platform=DeliveryPlatform.APP,
        publication_id="1f098f61-a5d1-46c4-b2ec-d64767d3f011",
        task_id="9b98ef01-4d6e-4328-b8c8-4d060bc0dcc5",
        status="pending",
        row_version=3,
        finalization_epoch=2,
        ruleset_id="58cf94bc-aa44-4286-a560-417ddfcfa6ac",
        ruleset_version=4,
        ruleset_snapshot_hash="sha256:rules",
        source_identity={
            "evidence_index_id": "app-index-1",
            "evidence_index_hash": "sha256:index",
        },
        outbox_pending_count=0,
        outbox_dead_letter_count=1,
        inbox_pending_count=0,
        inbox_dead_letter_count=1,
        linked_work_item_count=2,
        linked_work_active_count=1,
        linked_work_failed_count=0,
        failure_code="DELIVERY_DEAD_LETTER",
        failure_message="投递重试耗尽",
        allowed_actions=(
            DeliveryOperationAction.RECONCILE,
            DeliveryOperationAction.REQUEUE,
            DeliveryOperationAction.MARK_NEEDS_REVIEW,
        ),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


class FakeDeliveryOperationService:
    def __init__(self) -> None:
        self.incident = _delivery_incident()
        self.list_calls: list[tuple[str, int]] = []
        self.execute_calls: list[dict[str, Any]] = []
        self.conflict: str | None = None

    async def list_incidents(
        self,
        *,
        task_id: str,
        limit: int,
    ) -> list[DeliveryIncident]:
        self.list_calls.append((task_id, limit))
        return [self.incident]

    async def execute(self, **kwargs: Any) -> DeliveryOperationResult:
        self.execute_calls.append(kwargs)
        if self.conflict:
            raise DeliveryOperationConflictError(self.conflict)
        return DeliveryOperationResult(
            operation_id="f67ac6b5-75ec-46dc-b5e5-ce1650910c5a",
            action=kwargs["action"],
            platform=kwargs["platform"],
            publication_id=kwargs["publication_id"],
            task_id=self.incident.task_id,
            result="applied",
            affected_counts={"outbox_requeued": 1, "inbox_requeued": 1},
            incident=self.incident,
        )

    async def queue_depth(self) -> int:
        raise ConnectionError("redis unreachable")


@pytest.fixture
def client(queue: RedisAgentRunQueue) -> tuple[TestClient, FakeInsightRepositoryForSystem]:
    repository = FakeInsightRepositoryForSystem()
    app = create_app()
    app.dependency_overrides[system_module._get_agent_run_queue] = lambda: queue
    app.dependency_overrides[system_module._get_task_insight_repository] = lambda: repository
    return TestClient(app), repository


def test_system_status_reports_offline_worker(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    response = test_client.get("/api/v1/workstation/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["worker"]["alive"] is False
    assert body["worker"]["active_instances"] == 0
    assert body["queue"] == {"depth": 0, "redis_available": True}
    assert body["queued_tasks"]["count"] == 1
    assert body["queued_tasks"]["oldest_waiting_seconds"] >= 0


def test_system_status_reports_alive_worker(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
    queue: RedisAgentRunQueue,
) -> None:
    test_client, _ = client
    asyncio.get_event_loop_policy()
    asyncio.run(queue.record_worker_heartbeat("host-a:1"))
    response = test_client.get("/api/v1/workstation/system/status")
    body = response.json()
    assert body["worker"]["alive"] is True
    assert body["worker"]["active_instances"] == 1
    assert body["worker"]["last_heartbeat_at"] is not None


def test_system_status_degrades_when_redis_unavailable(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    test_client.app.dependency_overrides[system_module._get_agent_run_queue] = (
        lambda: BrokenRedisQueue()
    )
    response = test_client.get("/api/v1/workstation/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["worker"]["alive"] is False
    assert body["queue"] == {"depth": None, "redis_available": False}


def test_system_status_rejects_non_operator(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    test_client.app.dependency_overrides[auth_module.require_auth_user] = lambda: (
        auth_module.AuthUserResponse(
            id="user-1",
            username="reader",
            full_name="普通用户",
            role="规则配置员",
            status="enabled",
            permissions=["dashboard"],
            tenant_id="tenant-1",
        )
    )

    response = test_client.get("/api/v1/workstation/system/status")

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "无权限读取系统运行状态"


def test_delivery_incidents_returns_unified_h5_app_projection(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    service = FakeDeliveryOperationService()
    test_client.app.dependency_overrides[
        system_module._get_delivery_operation_service
    ] = lambda: service

    response = test_client.get(
        "/api/v1/workstation/system/delivery-incidents",
        params={"task_id": service.incident.task_id, "limit": 50},
    )

    assert response.status_code == 200
    assert service.list_calls == [(service.incident.task_id, 50)]
    body = response.json()
    assert body[0]["platform"] == "app"
    assert body[0]["publication_id"] == service.incident.publication_id
    assert body[0]["source_identity"]["evidence_index_id"] == "app-index-1"
    assert body[0]["outbox_dead_letter_count"] == 1
    assert body[0]["inbox_dead_letter_count"] == 1
    assert body[0]["allowed_actions"] == [
        "reconcile",
        "requeue",
        "mark_needs_review",
    ]


def test_delivery_command_passes_operator_reason_and_version_fence(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    service = FakeDeliveryOperationService()
    test_client.app.dependency_overrides[
        system_module._get_delivery_operation_service
    ] = lambda: service

    response = test_client.post(
        "/api/v1/workstation/system/delivery-incidents/"
        f"app/{service.incident.publication_id}/commands",
        json={
            "action": "requeue",
            "reason": "确认 Redis 故障已恢复",
            "expected_row_version": 3,
        },
    )

    assert response.status_code == 200
    call = service.execute_calls[0]
    assert call["platform"] is DeliveryPlatform.APP
    assert call["action"] is DeliveryOperationAction.REQUEUE
    assert call["publication_id"] == service.incident.publication_id
    assert call["reason"] == "确认 Redis 故障已恢复"
    assert call["expected_row_version"] == 3
    assert call["operator_user_id"]
    assert response.json()["affected_counts"] == {
        "outbox_requeued": 1,
        "inbox_requeued": 1,
    }


def test_delivery_command_returns_409_when_source_identity_drifted(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    service = FakeDeliveryOperationService()
    service.conflict = "任务 epoch、规则快照或主证据来源身份已漂移，拒绝重新投递"
    test_client.app.dependency_overrides[
        system_module._get_delivery_operation_service
    ] = lambda: service

    response = test_client.post(
        "/api/v1/workstation/system/delivery-incidents/"
        f"app/{service.incident.publication_id}/commands",
        json={
            "action": "reconcile",
            "reason": "尝试恢复",
            "expected_row_version": 3,
        },
    )

    assert response.status_code == 409
    assert "主证据来源身份已漂移" in response.json()["error"]["message"]


def test_delivery_incidents_rejects_non_operator(
    client: tuple[TestClient, FakeInsightRepositoryForSystem],
) -> None:
    test_client, _ = client
    test_client.app.dependency_overrides[auth_module.require_auth_user] = lambda: (
        auth_module.AuthUserResponse(
            id="user-1",
            username="reader",
            full_name="普通用户",
            role="规则配置员",
            status="enabled",
            permissions=["dashboard"],
            tenant_id="tenant-1",
        )
    )

    response = test_client.get(
        "/api/v1/workstation/system/delivery-incidents",
        params={"task_id": "9b98ef01-4d6e-4328-b8c8-4d060bc0dcc5"},
    )

    assert response.status_code == 403
