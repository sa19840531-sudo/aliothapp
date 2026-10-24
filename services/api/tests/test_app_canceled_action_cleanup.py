"""正式 App 任务取消时 Work 与 Action 收尾的边界测试。"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from services.api.app import worker as worker_module
from services.api.app.application.action_executions import (
    APP_WORKFLOW_CANCELED_ERROR_CODE,
    APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
)
from services.api.app.application.app_runtime_composition import (
    _close_canceled_exploration,
)
from services.api.app.application.app_v2_runtime import AppV2Runtime, build_app_v2_runtime
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionSpec
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.tests import test_app_postgres_integration as _postgres_integration
from services.api.tests.test_app_v2_runtime import _dependencies

_app_database = _postgres_integration._app_database


@pytest.fixture(scope="session")
def _upgrade_isolated_postgres() -> None:
    """仅由真实 PG 案例显式触发既有隔离库迁移，不污染单元测试。"""

    _postgres_integration._upgrade_isolated_postgres.__wrapped__()


class _RecordingWorkQueue:
    def __init__(self, events: list[tuple[str, dict[str, object]]]) -> None:
        self._events = events

    async def cancel_nonterminal(self, **kwargs: object) -> int:
        self._events.append(("work", dict(kwargs)))
        return 2


class _RecordingActionRepository:
    def __init__(self, events: list[tuple[str, dict[str, object]]]) -> None:
        self._events = events

    async def fail_nonterminal_for_task(self, **kwargs: object) -> int:
        self._events.append(("action", dict(kwargs)))
        return 3


def _runtime(*, turn_runner: object, action_executions: object) -> AppV2Runtime:
    driver = object()
    return AppV2Runtime(
        runtime_bundle=SimpleNamespace(bootstrap_port=driver, driver=driver),
        bootstrap_workflow=object(),
        exploration_workflow=object(),
        turn_runner=turn_runner,
        action_executions=action_executions,
    )


async def _insert_captured_surface(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    capture_id: str,
    *,
    capture_role: str,
    command_sequence: int,
    action_execution_id: str | None = None,
    parent_capture_id: str | None = None,
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_capture (
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            parent_capture_id, action_execution_id, capture_role,
            session_command_sequence, capture_status, captured_at, metadata
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid, 'android', $4, $5::uuid, $6,
            $7, $8, 'captured', now(), $9::jsonb
        )
        """,
        capture_id,
        ids["task"],
        ids["run"],
        ids["session"],
        parent_capture_id,
        action_execution_id,
        capture_role,
        command_sequence,
        json.dumps(
            {
                "business_page_identity": capture_id,
                "surface_facts": {"local_state": capture_role},
            }
        ),
    )


async def _insert_action_execution(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    action_id: str,
    *,
    status: str,
    command_sequence: int,
    source_capture_id: str | None = None,
    result_capture_id: str | None = None,
    row_version: int = 0,
    error_code: str | None = None,
) -> None:
    now = datetime.now(UTC)
    started_at = now if status in {"executing", "evidence_pending", "completed"} else None
    action_finished_at = (
        now if status in {"evidence_pending", "completed", "failed"} else None
    )
    evidence_finished_at = now if status == "completed" else None
    await connection.execute(
        """
        INSERT INTO qc_action_execution (
            action_execution_id, task_id, session_id, intent_id, idempotency_key,
            intent_hash, intent_protocol_version, intent_payload, command_sequence,
            finalization_epoch, requested_source_capture_id, source_capture_id,
            result_capture_id, status, result_payload, error_code, error_message,
            row_version, started_at, action_finished_at, evidence_finished_at
        ) VALUES (
            $1, $2::uuid, $3, $4, $5, 'cancel-test-hash', 'cancel-test/v1', $6::jsonb,
            $7, 0, $8::uuid, $9::uuid, $10::uuid, $11, '{}'::jsonb, $12, $13,
            $14, $15, $16, $17
        )
        """,
        action_id,
        ids["task"],
        ids["session"],
        f"cancel-intent-{action_id}",
        f"cancel-idempotency-{action_id}",
        json.dumps(
            {
                "surface_kind": "android",
                "operation": "click",
                "risk_level": "low",
                "semantic_target": "取消收尾测试动作",
            }
        ),
        command_sequence,
        source_capture_id,
        source_capture_id,
        result_capture_id,
        status,
        error_code,
        "已有终态，不应被取消覆盖" if error_code else None,
        row_version,
        started_at,
        action_finished_at,
        evidence_finished_at,
    )


async def _create_other_task(
    connection: asyncpg.Connection,
    pool: asyncpg.Pool,
    ids: dict[str, str],
) -> dict[str, str]:
    other = {
        "task": str(uuid4()),
        "run": str(uuid4()),
        "session": f"other-session-{uuid4()}",
        "build_ref": f"other-build:{uuid4()}",
    }
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform,
            submitter_id, tenant_id, task_config
        ) VALUES (
            $1::uuid, NULL, $2::uuid, 'running', 'app', $3::uuid, $4::uuid,
            jsonb_build_object(
                'app', jsonb_build_object('platform_build_ref', $5::text)
            )
        )
        """,
        other["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
        other["build_ref"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Action cleanup other task', 'running')
        """,
        other["run"],
        other["task"],
    )
    await PostgresSurfaceSessionRepository(pool=pool).register_starting(
        spec=SurfaceSessionSpec(
            task_id=other["task"],
            session_id=other["session"],
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref=other["build_ref"],
        ),
        session_owner="cancel-cleanup-other-task",
        primary_surface_ref=app_primary_surface_ref(other["session"]),
    )
    return other


def test_formal_runtime_builder_keeps_action_repository_dependency() -> None:
    dependencies = _dependencies()

    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-cancel-test",
    )

    assert runtime.action_executions is dependencies.action_executions


@pytest.mark.asyncio
async def test_worker_cancel_attempts_action_after_work_failure_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class FailingWorkQueue(_RecordingWorkQueue):
        async def cancel_nonterminal(self, **kwargs: object) -> int:
            self._events.append(("work", dict(kwargs)))
            raise ConnectionError("work cleanup unavailable")

    work = FailingWorkQueue(events)
    actions = _RecordingActionRepository(events)
    monkeypatch.setattr(worker_module, "PostgresExplorerWorkRepository", lambda: work)
    monkeypatch.setattr(worker_module, "PostgresActionExecutionRepository", lambda: actions)

    with pytest.raises(ConnectionError, match="work cleanup unavailable"):
        await worker_module._cancel_app_nonterminal_work(task_id="task-cancel")

    assert [name for name, _ in events] == ["work", "action"]
    assert events[1][1] == {
        "task_id": "task-cancel",
        "error_code": APP_WORKFLOW_CANCELED_ERROR_CODE,
        "error_message": APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
    }


@pytest.mark.asyncio
async def test_runtime_cancel_workflow_orders_work_then_action_and_deadline_path_is_work_only(
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    runtime = _runtime(
        turn_runner=_RecordingWorkQueue(events),
        action_executions=_RecordingActionRepository(events),
    )

    assert await runtime.cancel_exploration_work(task_id="task-deadline") == 2
    assert events == [("work", {"task_id": "task-deadline"})]

    assert await runtime.cancel_workflow(task_id="task-cancel") == 5
    assert [name for name, _ in events] == ["work", "work", "action"]
    assert events[-1][1] == {
        "task_id": "task-cancel",
        "error_code": APP_WORKFLOW_CANCELED_ERROR_CODE,
        "error_message": APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
    }


@pytest.mark.asyncio
async def test_runtime_cancel_workflow_attempts_action_after_work_failure() -> None:
    events: list[str] = []

    class FailingWorkQueue:
        async def cancel_nonterminal(self, *, task_id: str) -> int:
            events.append(f"work:{task_id}")
            raise ConnectionError("work cleanup unavailable")

    class ActionRepository:
        async def fail_nonterminal_for_task(self, **kwargs: object) -> int:
            events.append(f"action:{kwargs['task_id']}")
            return 1

    runtime = _runtime(
        turn_runner=FailingWorkQueue(),
        action_executions=ActionRepository(),
    )

    with pytest.raises(ConnectionError, match="work cleanup unavailable"):
        await runtime.cancel_workflow(task_id="task-cancel")

    assert events == ["work:task-cancel", "action:task-cancel"]


@pytest.mark.asyncio
async def test_canceled_closure_uses_workflow_cleanup_before_closure_and_logs_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    class Runtime:
        async def cancel_workflow(self, *, task_id: str) -> int:
            assert task_id == "task-cancel"
            events.append("cancel_workflow")
            raise ConnectionError("action cleanup unavailable")

        async def close_exploration(self, command: object) -> object:
            del command
            events.append("close")
            return SimpleNamespace(close_error=None)

    record = SimpleNamespace(task_id="task-cancel", run_id="run-cancel")
    command = SimpleNamespace(session_id="app:task-cancel")
    handoff = SimpleNamespace(capture=SimpleNamespace(surface_kind=SurfaceKind.ANDROID))
    target = SimpleNamespace(
        surface_kind=SurfaceKind.ANDROID,
        app=SimpleNamespace(platform_build_ref="build-1"),
    )
    runtime_policy = SimpleNamespace(to_dict=lambda: {})
    started_at = datetime(2026, 9, 7, tzinfo=UTC)

    with caplog.at_level(logging.ERROR):
        result = await _close_canceled_exploration(
            Runtime(),
            record=record,
            command=command,
            handoff=handoff,
            target=target,
            started_at=started_at,
            deadline=started_at + timedelta(minutes=5),
            consumed_turns=1,
            runtime_policy=runtime_policy,
        )

    assert events == ["cancel_workflow", "close"]
    assert result.termination_reason.value == "canceled"
    assert "task_id=task-cancel" in caplog.text
    assert "run_id=run-cancel" in caplog.text
    assert "error_type=ConnectionError" in caplog.text
    assert "action cleanup unavailable" not in caplog.text
    assert caplog.records[-1].exc_info is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式提供隔离的 ALIOTH_TEST_DATABASE_URL",
)
async def test_real_cancel_workflow_closes_lost_session_actions_and_rebuilds_graph(
    _upgrade_isolated_postgres: None,
) -> None:
    """真实 PG 验证租约失效后的任务取消收束与无 After 图边。"""

    del _upgrade_isolated_postgres
    async with _app_database() as (connection, pool, ids):
        sessions = PostgresSurfaceSessionRepository(pool=pool)
        await sessions.register_active(
            spec=SurfaceSessionSpec(
                task_id=ids["task"],
                session_id=ids["session"],
                surface_kind=SurfaceKind.ANDROID,
                platform_build_ref=ids["build_ref"],
            ),
            session_owner="app-workflow",
            primary_surface_ref=app_primary_surface_ref(ids["session"]),
        )
        lease_token = str(uuid4())
        await sessions.acquire_command(
            task_id=ids["task"],
            session_id=ids["session"],
            surface_kind=SurfaceKind.ANDROID,
            lease_owner="cancel-cleanup-worker",
            lease_token=lease_token,
            lease_seconds=120,
            allow_draining=False,
        )
        await connection.execute(
            """
            UPDATE qc_surface_session
            SET lease_expires_at = now() - interval '1 second'
            WHERE task_id = $1::uuid AND session_id = $2
            """,
            ids["task"],
            ids["session"],
        )

        action_ids = {
            name: str(uuid4())
            for name in (
                "planned",
                "executing",
                "evidence_pending",
                "completed",
                "existing_failed",
            )
        }
        source_ids = {
            name: str(uuid4())
            for name in ("executing", "evidence_pending", "completed")
        }
        completed_target_id = str(uuid4())
        await _insert_captured_surface(
            connection,
            ids,
            source_ids["executing"],
            capture_role="before_action",
            command_sequence=2,
            action_execution_id=action_ids["executing"],
        )
        await _insert_captured_surface(
            connection,
            ids,
            source_ids["evidence_pending"],
            capture_role="before_action",
            command_sequence=3,
            action_execution_id=action_ids["evidence_pending"],
        )
        await _insert_captured_surface(
            connection,
            ids,
            source_ids["completed"],
            capture_role="before_action",
            command_sequence=4,
            action_execution_id=action_ids["completed"],
        )
        await _insert_captured_surface(
            connection,
            ids,
            completed_target_id,
            capture_role="after_action",
            command_sequence=4,
            action_execution_id=action_ids["completed"],
            parent_capture_id=source_ids["completed"],
        )
        await _insert_action_execution(
            connection,
            ids,
            action_ids["planned"],
            status="planned",
            command_sequence=5,
        )
        await _insert_action_execution(
            connection,
            ids,
            action_ids["executing"],
            status="executing",
            command_sequence=2,
            source_capture_id=source_ids["executing"],
        )
        await _insert_action_execution(
            connection,
            ids,
            action_ids["evidence_pending"],
            status="evidence_pending",
            command_sequence=3,
            source_capture_id=source_ids["evidence_pending"],
        )
        await _insert_action_execution(
            connection,
            ids,
            action_ids["completed"],
            status="completed",
            command_sequence=4,
            source_capture_id=source_ids["completed"],
            result_capture_id=completed_target_id,
            row_version=8,
        )
        await _insert_action_execution(
            connection,
            ids,
            action_ids["existing_failed"],
            status="failed",
            command_sequence=6,
            row_version=9,
            error_code="PREEXISTING_FAILURE",
        )

        other = await _create_other_task(connection, pool, ids)
        other_action_id = str(uuid4())
        await _insert_action_execution(
            connection,
            other,
            other_action_id,
            status="planned",
            command_sequence=1,
        )

        try:
            session_row_version = await connection.fetchval(
                """
                SELECT row_version
                FROM qc_surface_session
                WHERE task_id = $1::uuid AND session_id = $2
                """,
                ids["task"],
                ids["session"],
            )
            lost = await sessions.mark_lost(
                task_id=ids["task"],
                session_id=ids["session"],
                expected_row_version=int(session_row_version),
                reason="cancel cleanup integration: command lease expired",
            )
            assert lost.status.value == "lost"

            class _NoopWorkQueue:
                async def cancel_nonterminal(self, *, task_id: str) -> int:
                    assert task_id == ids["task"]
                    return 0

            runtime = _runtime(
                turn_runner=_NoopWorkQueue(),
                action_executions=PostgresActionExecutionRepository(pool=pool),
            )
            assert await runtime.cancel_workflow(task_id=ids["task"]) == 3

            rows = await connection.fetch(
                """
                SELECT action_execution_id, status, source_capture_id::text,
                       result_capture_id::text, error_code, error_message,
                       action_finished_at, evidence_finished_at, row_version
                FROM qc_action_execution
                WHERE task_id = $1::uuid
                ORDER BY action_execution_id
                """,
                ids["task"],
            )
            by_id = {str(row["action_execution_id"]): row for row in rows}
            for name in ("executing", "evidence_pending"):
                row = by_id[action_ids[name]]
                assert row["status"] == (
                    "evidence_failed" if name == "evidence_pending" else "failed"
                )
                assert row["error_code"] == APP_WORKFLOW_CANCELED_ERROR_CODE
                assert row["error_message"] == APP_WORKFLOW_CANCELED_ERROR_MESSAGE
                assert row["result_capture_id"] is None
                assert row["action_finished_at"] is not None
            assert by_id[action_ids["evidence_pending"]]["evidence_finished_at"] is not None

            planned = by_id[action_ids["planned"]]
            assert planned["status"] == "failed"
            assert planned["error_code"] == APP_WORKFLOW_CANCELED_ERROR_CODE
            assert planned["error_message"] == APP_WORKFLOW_CANCELED_ERROR_MESSAGE
            assert planned["action_finished_at"] is not None

            completed = by_id[action_ids["completed"]]
            assert completed["status"] == "completed"
            assert completed["error_code"] is None
            assert completed["result_capture_id"] == completed_target_id
            assert completed["row_version"] == 8
            existing_failed = by_id[action_ids["existing_failed"]]
            assert existing_failed["status"] == "failed"
            assert existing_failed["error_code"] == "PREEXISTING_FAILURE"
            assert existing_failed["row_version"] == 9
            assert planned["result_capture_id"] is None

            graph_rows = await connection.fetch(
                """
                SELECT action_execution_id, status, target_capture_id::text,
                       target_state_id::text, proof,
                       counts_as_business_arrival, replay_eligible
                FROM qc_page_graph_transition
                WHERE task_id = $1::uuid
                """,
                ids["task"],
            )
            graph_by_action = {
                str(row["action_execution_id"]): row for row in graph_rows
            }
            for name in ("executing", "evidence_pending"):
                edge = graph_by_action[action_ids[name]]
                assert edge["status"] == (
                    "evidence_failed" if name == "evidence_pending" else "failed"
                )
                assert edge["target_capture_id"] is None
                assert edge["target_state_id"] is None
                assert edge["proof"] == "insufficient"
                assert edge["counts_as_business_arrival"] is False
                assert edge["replay_eligible"] is False
            completed_edge = graph_by_action[action_ids["completed"]]
            assert completed_edge["target_capture_id"] == completed_target_id
            assert completed_edge["counts_as_business_arrival"] is False
            assert completed_edge["replay_eligible"] is False
            assert action_ids["planned"] not in graph_by_action

            assert (
                await connection.fetchval(
                    """
                    SELECT status
                    FROM qc_action_execution
                    WHERE action_execution_id = $1
                    """,
                    other_action_id,
                )
                == "planned"
            )
            assert (
                await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM qc_page_graph_transition
                    WHERE task_id = $1::uuid
                    """,
                    other["task"],
                )
                == 0
            )
        finally:
            await connection.execute(
                "DELETE FROM qc_task WHERE id = $1::uuid", other["task"]
            )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式提供隔离的 ALIOTH_TEST_DATABASE_URL",
)
async def test_real_bulk_cancel_rebuilds_stale_graph_after_prior_rebuild_failure(
    _upgrade_isolated_postgres: None,
) -> None:
    """动作批量更新已提交但图重建失败时，重试仍必须修复旧图。"""

    del _upgrade_isolated_postgres
    async with _app_database() as (connection, pool, ids):
        sessions = PostgresSurfaceSessionRepository(pool=pool)
        await sessions.register_active(
            spec=SurfaceSessionSpec(
                task_id=ids["task"],
                session_id=ids["session"],
                surface_kind=SurfaceKind.ANDROID,
                platform_build_ref=ids["build_ref"],
            ),
            session_owner="app-workflow",
            primary_surface_ref=app_primary_surface_ref(ids["session"]),
        )
        action_id = str(uuid4())
        source_capture_id = str(uuid4())
        await _insert_captured_surface(
            connection,
            ids,
            source_capture_id,
            capture_role="before_action",
            command_sequence=2,
            action_execution_id=action_id,
        )
        await _insert_action_execution(
            connection,
            ids,
            action_id,
            status="executing",
            command_sequence=2,
            source_capture_id=source_capture_id,
        )

        graph = PostgresPageGraphRepository(pool=pool)
        await graph.rebuild_task(task_id=ids["task"])
        initial_edge = await connection.fetchrow(
            """
            SELECT status, target_capture_id::text, target_state_id::text,
                   counts_as_business_arrival, proof, replay_eligible
            FROM qc_page_graph_transition
            WHERE action_execution_id = $1
            """,
            action_id,
        )
        assert initial_edge is not None
        assert initial_edge["status"] == "executing"
        assert initial_edge["target_capture_id"] is None

        class _FailOnceGraph:
            def __init__(self) -> None:
                self.calls = 0

            async def rebuild_task(self, *, task_id: str) -> object:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("injected graph rebuild failure")
                return await graph.rebuild_task(task_id=task_id)

        graph_with_fault = _FailOnceGraph()
        actions = PostgresActionExecutionRepository(
            pool=pool,
            page_graph_repository=graph_with_fault,
        )
        with pytest.raises(RuntimeError, match="injected graph rebuild failure"):
            await actions.fail_nonterminal_for_task(
                task_id=ids["task"],
                error_code=APP_WORKFLOW_CANCELED_ERROR_CODE,
                error_message=APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
            )

        committed = await connection.fetchrow(
            """
            SELECT status, error_code, result_capture_id::text
            FROM qc_action_execution
            WHERE action_execution_id = $1
            """,
            action_id,
        )
        assert committed["status"] == "failed"
        assert committed["error_code"] == APP_WORKFLOW_CANCELED_ERROR_CODE
        assert committed["result_capture_id"] is None
        stale_edge = await connection.fetchrow(
            "SELECT status FROM qc_page_graph_transition WHERE action_execution_id = $1",
            action_id,
        )
        assert stale_edge["status"] == "executing"
        assert graph_with_fault.calls == 1

        # 第二次入口更新已是 0；仍必须执行同任务图重建，不能因 count=0 留下旧边。
        assert (
            await actions.fail_nonterminal_for_task(
                task_id=ids["task"],
                error_code=APP_WORKFLOW_CANCELED_ERROR_CODE,
                error_message=APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
            )
            == 0
        )
        assert graph_with_fault.calls == 2

        final_edge = await connection.fetchrow(
            """
            SELECT status, target_capture_id::text, target_state_id::text,
                   counts_as_business_arrival, proof, replay_eligible
            FROM qc_page_graph_transition
            WHERE action_execution_id = $1
            """,
            action_id,
        )
        assert final_edge["status"] == "failed"
        assert final_edge["target_capture_id"] is None
        assert final_edge["target_state_id"] is None
        assert final_edge["counts_as_business_arrival"] is False
        assert final_edge["proof"] == "insufficient"
        assert final_edge["replay_eligible"] is False
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_capture
                WHERE action_execution_id = $1
                  AND capture_role = 'after_action'
                """,
                action_id,
            )
            == 0
        )
