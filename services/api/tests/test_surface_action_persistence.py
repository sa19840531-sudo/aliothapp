"""Surface Session 与 Action Execution PostgreSQL 约束测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    ActionExecutionStatus,
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
    SurfaceReferenceStatus,
    SurfaceSessionSpec,
)
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.surface_registry_repository import (
    PostgresSurfaceRegistryRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class SurfaceConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.session = _session_row()
        self.last_fetchval_args: tuple[Any, ...] = ()

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        del args
        self.queries.append(" ".join(query.split()))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "RETURNING session.task_id::text AS task_id" in normalized:
            return {
                "task_id": args[0],
                "session_id": args[1],
                "surface_kind": args[2],
                "lease_owner": args[3],
                "lease_token": args[4],
                "command_sequence": 1,
                "finalization_epoch": 0,
                "lease_expires_at": datetime.now(UTC) + timedelta(minutes=2),
                "current_capture_id": "22222222-2222-2222-2222-222222222222",
            }
        return self.session

    async def fetchval(self, query: str, *args: Any):
        self.queries.append(" ".join(query.split()))
        self.last_fetchval_args = args
        return args[1]


class ActionConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        intent_payload = args[7]
        return {
            "action_execution_id": args[0],
            "task_id": args[1],
            "session_id": args[2],
            "intent_id": args[3],
            "intent_hash": args[5],
            "command_sequence": args[12],
            "finalization_epoch": args[13],
            "requested_source_capture_id": args[10],
            "source_capture_id": None,
            "result_capture_id": None,
            "status": "planned",
            "result_payload": {},
            "error_code": None,
            "error_message": None,
            "row_version": 0,
            "created_at": datetime.now(UTC),
            "started_at": None,
            "action_finished_at": None,
            "evidence_finished_at": None,
            "intent_payload": intent_payload,
        }


class RegistryConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.status = "active"
        self.row_version = 0

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "UPDATE qc_surface_ref" in normalized:
            self.status = args[3]
            self.row_version += 1
        now = datetime.now(UTC)
        return {
            "surface_ref_id": "surface-2",
            "session_id": "session-1",
            "task_id": "11111111-1111-1111-1111-111111111111",
            "surface_kind": "web",
            "surface_key": "tab-2",
            "status": self.status,
            "current_capture_id": None,
            "pin_reason": args[5] if self.status == "pinned" else None,
            "row_version": self.row_version,
            "created_at": now,
            "updated_at": now,
        }


def _session_row() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "session_id": "session-1",
        "task_id": "11111111-1111-1111-1111-111111111111",
        "surface_kind": "web",
        "session_owner": "interaction-runtime",
        "status": "active",
        "command_sequence": 0,
        "finalization_epoch": 0,
        "row_version": 0,
        "account_environment_ref": None,
        "platform_build_ref": None,
        "device_ref": None,
        "current_surface_ref": "surface-1",
        "current_capture_id": None,
        "started_at": now,
        "updated_at": now,
    }


def _intent() -> ActionIntent:
    return ActionIntent(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="task:branch:intent",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="22222222-2222-2222-2222-222222222222",
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        purpose="进入登录页",
        capture_profile=CaptureProfile(
            profile_id="baseline",
            before=CaptureDirective(role="before", artifact_types=("screenshot",)),
            after=CaptureDirective(
                role="after",
                artifact_types=("screenshot", "action_trace"),
            ),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


async def test_surface_repository_registers_session_and_uses_database_lease() -> None:
    connection = SurfaceConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    spec = SurfaceSessionSpec(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
    )

    snapshot = await repository.register_active(
        spec=spec,
        session_owner="interaction-runtime",
        primary_surface_ref=web_primary_surface_ref("session-1"),
    )
    permit = await repository.acquire_command(
        task_id=spec.task_id,
        session_id=spec.session_id,
        surface_kind=spec.surface_kind,
        lease_owner="worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        lease_seconds=120,
        allow_draining=False,
    )
    released = await repository.release_command(permit=permit, current_capture_id=None)

    assert snapshot.status.value == "active"
    assert permit.command_sequence == 1
    assert permit.current_capture_id == "22222222-2222-2222-2222-222222222222"
    assert released is True
    assert "session.current_capture_id" in connection.queries[-2]
    assert any("lease_token = $5::uuid" in query for query in connection.queries)
    assert any("task.status IN ('queued', 'running')" in query for query in connection.queries)
    release_query = connection.queries[-1]
    assert "$8::uuid IS NULL" in release_query
    assert "$9" not in release_query
    assert len(connection.last_fetchval_args) == 8


async def test_action_plan_is_bound_to_session_lease_epoch_and_capture() -> None:
    connection = ActionConnection()
    repository = PostgresActionExecutionRepository(
        pool=FakePool(connection), page_graph_repository=None
    )
    intent = _intent()
    permit = SessionCommandPermit(
        task_id=intent.task_id,
        session_id=intent.session_id,
        surface_kind=SurfaceKind.WEB,
        lease_owner="worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        command_sequence=1,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    action = await repository.plan(intent=intent, permit=permit)

    assert action.status is ActionExecutionStatus.PLANNED
    query = connection.queries[0]
    assert "session.lease_token = $10::uuid" in query
    assert "session.finalization_epoch = $14" in query
    assert "source.id = $11::uuid" in query
    assert "source.surface_session_id = session.session_id" in query
    assert "source.surface_kind = session.surface_kind" in query


async def test_entry_action_plans_without_parent_capture() -> None:
    """首跳发生在任何采集之前，父采集只能为空——不能拿入口哨兵去 JOIN Capture。"""
    connection = ActionConnection()
    repository = PostgresActionExecutionRepository(
        pool=FakePool(connection), page_graph_repository=None
    )
    entry_intent = replace(
        _intent(),
        action_type=InteractionOperation.NAVIGATE,
        source_capture_id="h5-entry:run-1",
        semantic_target="H5 任务锁定入口",
        parameters={
            "destination_url": "https://m.example.test/home",
            "initial_surface_action": True,
        },
    )
    permit = SessionCommandPermit(
        task_id=entry_intent.task_id,
        session_id=entry_intent.session_id,
        surface_kind=SurfaceKind.WEB,
        lease_owner="worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        command_sequence=1,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    action = await repository.plan(intent=entry_intent, permit=permit)

    assert action.status is ActionExecutionStatus.PLANNED
    assert action.requested_source_capture_id is None
    query = connection.queries[0]
    assert "INNER JOIN qc_capture" not in query
    # 归属、租约与幂等条件一个都不能少
    assert "session.lease_token = $10::uuid" in query
    assert "session.finalization_epoch = $14" in query
    assert "session.status IN ('active', 'draining')" in query


async def test_surface_registry_pins_only_owned_logical_surface() -> None:
    connection = RegistryConnection()
    repository = PostgresSurfaceRegistryRepository(pool=FakePool(connection))

    surface = await repository.register_surface(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="session-1",
        surface_ref_id="surface-2",
        surface_key="tab-2",
        surface_kind=SurfaceKind.WEB,
    )
    pinned = await repository.transition_surface(
        task_id=surface.task_id,
        surface_ref_id=surface.surface_ref_id,
        expected_status=SurfaceReferenceStatus.ACTIVE,
        target_status=SurfaceReferenceStatus.PINNED,
        row_version=surface.row_version,
        pin_reason="active_session_evidence",
    )

    assert pinned.status is SurfaceReferenceStatus.PINNED
    assert pinned.pin_reason == "active_session_evidence"
    assert "capture.surface_session_id = surface.session_id" in connection.queries[-1]
