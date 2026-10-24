"""App Starting Session 与 Bootstrap 原子激活测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    AppBootstrapStage,
    AppReadyHandoff,
    CaptureKind,
    CaptureRef,
    SurfaceKind,
    SurfaceSessionSpec,
    build_android_screenshot_only_manifest,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionPersistenceError,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> SessionConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class SessionConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.session_status = "starting"

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "RETURNING session.task_id::text AS task_id" in normalized:
            if self.session_status != "active":
                return None
            return {
                "task_id": args[0],
                "session_id": args[1],
                "surface_kind": args[2],
                "lease_owner": args[3],
                "lease_token": args[4],
                "command_sequence": 1,
                "finalization_epoch": 0,
                "lease_expires_at": datetime.now(UTC) + timedelta(minutes=2),
            }
        if "UPDATE qc_app_bootstrap AS bootstrap" in normalized:
            return {"ready_at": datetime.now(UTC)}
        if "UPDATE qc_surface_session AS session" in normalized:
            self.session_status = "active"
        return _session_row(status=self.session_status)

    async def fetchval(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchval", normalized, args))
        if "INSERT INTO qc_surface_ref" in normalized:
            return args[0]
        if "INSERT INTO qc_outbox_event" in normalized:
            return args[0]
        return "app-session-1"


class CloseReadinessConnection(SessionConnection):
    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        return {
            "session_id": "app-session-1",
            "ready": False,
            "session_status": "draining",
            "has_command_lease": False,
            "pending_actions": 0,
            "pending_capture_commits": 0,
            "active_evidence_requests": 0,
            "running_restorations": 0,
            "nonterminal_active_work_items": 0,
            "nonterminal_app_frontier_items": 2,
            "pinned_surfaces": 0,
            "pending_manifests": 1,
            "building_evidence_indexes": 1,
        }


class ExplorationPendingFactsConnection(SessionConnection):
    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        return {
            "session_id": "app-session-1",
            "pending_action_count": 2,
            "pending_evidence_count": 3,
        }


class ExplorationCloseConnection(SessionConnection):
    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "AS ready" in normalized:
            return {
                "session_id": "app-session-1",
                "row_version": 1,
                "ready": True,
                "session_status": "draining",
                "has_command_lease": False,
                "pending_actions": 0,
                "pending_capture_commits": 0,
                "active_evidence_requests": 0,
                "running_restorations": 0,
                "nonterminal_active_work_items": 0,
                "nonterminal_app_frontier_items": 0,
                "pinned_surfaces": 0,
                "pending_manifests": 0,
                "building_evidence_indexes": 0,
            }
        return _session_row(status="closing")


def _session_row(*, status: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "session_id": "app-session-1",
        "task_id": "11111111-1111-1111-1111-111111111111",
        "surface_kind": "android",
        "session_owner": "app-workflow",
        "status": status,
        "command_sequence": 0,
        "finalization_epoch": 0,
        "row_version": 1 if status == "active" else 0,
        "account_environment_ref": None,
        "platform_build_ref": "build-1",
        "device_ref": None,
        "current_surface_ref": "app-surface-1",
        "current_capture_id": (
            "33333333-3333-3333-3333-333333333333" if status == "active" else None
        ),
        "started_at": now,
        "updated_at": now,
    }


def _handoff() -> AppReadyHandoff:
    capture = CaptureRef(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        capture_id="33333333-3333-3333-3333-333333333333",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-bootstrap",),
    )
    return AppReadyHandoff(
        task_id=capture.task_id,
        agent_run_id="44444444-4444-4444-4444-444444444444",
        session_id=capture.session_id,
        installed_package="com.example.target",
        capture=capture,
        capability_manifest=build_android_screenshot_only_manifest(
            adapter_id="fake-app-runtime",
            adapter_version="1.0",
            backend="fake",
        ),
        bootstrap_row_version=8,
    )


async def test_register_starting_preserves_web_active_path_and_blocks_early_commands() -> None:
    connection = SessionConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    spec = SurfaceSessionSpec(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-1",
    )

    snapshot = await repository.register_starting(
        spec=spec,
        session_owner="app-workflow",
        primary_surface_ref="app-surface-1",
    )

    queries = [call[1] for call in connection.calls]
    session_insert = next(query for query in queries if "INSERT INTO qc_surface_session" in query)
    assert snapshot.status.value == "starting"
    assert "'starting'" in session_insert
    assert "spec.surface_kind" not in session_insert
    assert "existing.status IN ('starting', 'active')" in session_insert
    assert any("INSERT INTO qc_surface_ref" in query for query in queries)

    with pytest.raises(SurfaceSessionPersistenceError, match="不可执行"):
        await repository.acquire_command(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=spec.surface_kind,
            lease_owner="app-worker-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            lease_seconds=120,
            allow_draining=False,
        )
    acquire_query = connection.calls[-1][1]
    assert "session.status = 'active'" in acquire_query


async def test_activate_from_bootstrap_updates_checkpoint_session_surface_and_outbox() -> None:
    connection = SessionConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    handoff = _handoff()

    snapshot = await repository.activate_from_bootstrap(
        handoff=handoff,
        expected_bootstrap_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
        expected_bootstrap_row_version=7,
        expected_session_row_version=0,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="22222222-2222-2222-2222-222222222222",
    )

    queries = [call[1] for call in connection.calls]
    bootstrap_index = next(
        index for index, query in enumerate(queries) if "UPDATE qc_app_bootstrap" in query
    )
    session_index = next(
        index for index, query in enumerate(queries) if "UPDATE qc_surface_session" in query
    )
    surface_index = next(
        index for index, query in enumerate(queries) if "UPDATE qc_surface_ref" in query
    )
    outbox_index = next(
        index for index, query in enumerate(queries) if "INSERT INTO qc_outbox_event" in query
    )

    assert snapshot.status.value == "active"
    assert bootstrap_index < session_index < surface_index < outbox_index
    bootstrap_query = queries[bootstrap_index]
    assert "bootstrap.stage = $4" in bootstrap_query
    assert "bootstrap.row_version = $5" in bootstrap_query
    assert "bootstrap.workflow_lease_owner = $6" in bootstrap_query
    assert "bootstrap.workflow_lease_token = $7::uuid" in bootstrap_query
    assert "bootstrap_capture_id = $3::uuid" in bootstrap_query
    session_query = queries[session_index]
    assert "status = 'active'" in session_query
    assert "session.status = 'starting'" in session_query
    assert "current_capture_id = $3::uuid" in session_query
    outbox_call = connection.calls[outbox_index]
    assert outbox_call[2][2] == "app_bootstrap"
    assert outbox_call[2][3] == handoff.session_id
    assert outbox_call[2][4] == handoff.bootstrap_row_version
    assert outbox_call[2][5] == "alioth.v2.app-workflow.ready"
    assert handoff.capture.capture_id in outbox_call[2][9]
    assert "app-ready-handoff.v2" in outbox_call[2][9]
    assert handoff.installed_package in outbox_call[2][9]


async def test_release_command_uses_common_capture_after_0035() -> None:
    connection = SessionConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    permit = SessionCommandPermit(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        lease_owner="app-worker-1",
        lease_token="22222222-2222-2222-2222-222222222222",
        command_sequence=1,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    released = await repository.release_command(
        permit=permit,
        current_capture_id="33333333-3333-3333-3333-333333333333",
    )

    assert released is True
    release_query = connection.calls[-1][1]
    assert "qc_capture AS capture" in release_query
    assert "qc_page_capture AS capture" not in release_query


async def test_expired_web_command_lease_can_be_atomically_marked_lost() -> None:
    connection = SessionConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))

    cancelled = await repository.mark_web_lost_if_command_lease_expired(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="web:run-1",
        reason="web_formal_orphaned_command_lease_expired",
    )

    assert cancelled is True
    update_query = next(
        call[1]
        for call in connection.calls
        if call[0] == "fetchrow" and "lease_expires_at <= now()" in call[1]
    )
    assert "surface_kind = 'web'" in update_query
    assert "lease_token IS NOT NULL" in update_query
    assert "status IN ('active', 'draining', 'closing')" in update_query
    assert any("UPDATE qc_surface_ref" in call[1] for call in connection.calls)


async def test_close_readiness_blocks_pending_manifest_and_building_index() -> None:
    connection = CloseReadinessConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))

    readiness = await repository.close_readiness(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
    )

    assert readiness.ready is False
    assert readiness.pending_manifests == 1
    assert readiness.building_evidence_indexes == 1
    assert readiness.nonterminal_app_frontier_items == 2
    query = connection.calls[-1][1]
    assert "qc_evidence_manifest" in query
    # 只拦未裁决清单：fail 是可审计终态，算作阻塞会让裁决失败的页面永远关不掉会话
    assert "latest.quality_status IS NULL OR latest.quality_status = 'pending'" in query
    assert "IS DISTINCT FROM 'pass'" not in query
    assert "qc_app_evidence_index" in query
    assert "qc_h5_evidence_inventory_item" in query
    assert "h5_inventory.status IN ('pass', 'insufficient', 'failed')" in query
    assert "capture.id IN (" in query
    assert "h5_inventory.before_capture_id" in query and "h5_inventory.after_capture_id" in query
    assert "jsonb_array_elements" in query
    assert "evidence_gap->>'capture_id' = capture.id::text" in query
    assert "evidence_index.status = 'building'" in query
    assert "counts.nonterminal_app_frontier_items = 0" in query
    assert "qc_inspection_work_item" in query
    assert "inspection_work.session_dependency = 'active_session'" in query


async def test_close_readiness_only_counts_captures_carrying_requirements() -> None:
    """无需求集的伴生帧永远停在 pending，若计入阻塞项会让 Session 永远关不掉。"""
    connection = CloseReadinessConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))

    await repository.close_readiness(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
    )

    query = connection.calls[-1][1]
    assert "FROM qc_evidence_exit_requirement_set AS requirement" in query
    assert "requirement.capture_id = capture.id" in query


async def test_exploration_pending_facts_reads_action_and_evidence_blockers() -> None:
    connection = ExplorationPendingFactsConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))

    facts = await repository.exploration_pending_facts(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
    )

    assert facts.task_id == "11111111-1111-1111-1111-111111111111"
    assert facts.session_id == "app-session-1"
    assert facts.pending_action_count == 2
    assert facts.pending_evidence_count == 3
    query = connection.calls[-1][1]
    assert "qc_action_execution" in query
    assert "'planned', 'executing', 'evidence_pending'" in query
    assert "qc_capture" in query
    assert "capture.capture_status = 'pending'" in query
    assert "qc_evidence_request" in query
    assert "request.status IN ('pending', 'routed', 'running')" in query
    assert "qc_evidence_manifest" not in query


async def test_exploration_close_ignores_quality_inventory_but_keeps_runtime_gates() -> None:
    connection = ExplorationCloseConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))

    snapshot = await repository.seal_exploration_for_close(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        expected_row_version=1,
    )

    assert snapshot.status.value == "closing"
    readiness_query = connection.calls[0][1]
    assert "qc_action_execution" in readiness_query
    assert "qc_capture" in readiness_query
    assert "qc_restoration_attempt" in readiness_query
    assert "qc_v2_work_item" in readiness_query
    assert "qc_surface_ref" in readiness_query
    assert "qc_evidence_request" not in readiness_query
    assert "qc_evidence_manifest" not in readiness_query
    assert "qc_h5_evidence_inventory_item" not in readiness_query
    assert "qc_app_evidence_index" not in readiness_query
