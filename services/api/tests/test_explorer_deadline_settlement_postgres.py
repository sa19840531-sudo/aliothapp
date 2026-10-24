"""在隔离 PostgreSQL 中验证 H5 deadline 后终态结算的身份和原子性。"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationState,
)
from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    ExplorerWorkPersistenceError,
    ExplorerWorkStatus,
    ExplorerWorkType,
    TransitionExplorerWorkCommand,
)
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.exploration.h5_coverage import (
    H5_COVERAGE_METADATA_KEY,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 迁移显式隔离测试库
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


class _PinnedConnectionAcquire:
    """测试中把 repository 绑定到当前连接，便于回滚封存变更。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return False


class _PinnedConnectionPool:
    """只提供 repository 所需的 acquire 接口，不创建第二条连接。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    def acquire(self) -> _PinnedConnectionAcquire:
        return _PinnedConnectionAcquire(self._connection)


def _coverage(*, capture_id: str, revision: int) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": "h5-coverage-snapshot.v1",
        "revision": revision,
        "authority_source": "h5_page_type_assessment",
        "required_page_types": ["home"],
        "covered_page_types": ["home"],
        "missing_page_types": [],
        "scopes": [
            {
                "scope_id": f"scope:{capture_id}",
                "route_identity": "https://example.test/",
                "page_type": "home",
                "page_state": "stable",
                "page_capture_id": capture_id,
                "assessment_status": "covered",
                "assessment_hash": "sha256:settlement-test",
            }
        ],
        "assessment_scope_ready": True,
        "frontier_terminal": False,
        "completion_ready": False,
    }
    values["payload_hash"] = stable_protocol_hash(values)
    return values


def _frontier_metadata() -> dict[str, object]:
    return H5TaskFrontierSnapshot.create(max_pages=None).to_dict()


def _work_pair(ids: dict[str, str]) -> tuple[object, object]:
    now = datetime.now(UTC)
    deadline = now + timedelta(minutes=5)
    parent_metadata = {
        "agent_run_id": ids["run"],
        H5_TASK_FRONTIER_METADATA_KEY: _frontier_metadata(),
        H5_COVERAGE_METADATA_KEY: _coverage(
            capture_id=ids["capture"],
            revision=2,
        ),
    }
    parent_observation = SurfaceObservation(
        capture_id=ids["capture"],
        surface_kind=SurfaceKind.WEB,
        location="https://example.test/",
        state_version="state:settlement-parent",
        observed_at=now,
        page_type="home",
        page_state="stable",
        metadata=parent_metadata,
    )
    parent_frontier = FrontierSnapshot(
        snapshot_id=f"frontier:{ids['capture']}",
        observation=parent_observation,
        candidates=(
            CandidateAction(
                candidate_id="candidate:settlement",
                semantic_target="下一页面",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=80,
                navigation_depth=1,
            ),
        ),
    )
    boundaries = ExplorationBoundaries(
        allowed_hosts=("example.test",),
        allowed_operations=(InteractionOperation.CLICK,),
        max_navigation_depth=3,
        side_effect_policy=SideEffectPolicy.READ_ONLY,
    )
    parent = build_exploration_work(
        task_id=ids["task"],
        session_id=ids["session"],
        branch_id=f"settlement-parent:{ids['capture']}",
        objective="验证 H5 deadline 终态结算",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=ids["capture"],
        frontier_snapshot_id=parent_frontier.snapshot_id,
        frontier=parent_frontier,
        boundaries=boundaries,
        budget=ExplorationBudget(
            max_actions=4,
            remaining_actions=4,
            max_depth=3,
            remaining_duration_ms=20_000,
        ),
        deadline=deadline,
    )
    result_metadata = {
        "agent_run_id": ids["run"],
        "parent_work_id": parent.work_id,
        "parent_branch_id": parent.branch_id,
        "parent_capture_id": parent.source_capture_id,
        "exploration_scheduler_facts_scope": {
            "task_id": ids["task"],
            "agent_run_id": ids["run"],
            "session_id": ids["session"],
            "finalization_epoch": 0,
        },
        BOUNDED_EXPLORATION_STATE_KEY: BoundedExplorationState(
            max_actions=4,
            max_duration_ms=20_000,
            deadline=deadline,
            consumed_actions={"required": 1},
            consumed_duration_ms={"required": 100},
            retry_attempts={"required": 0},
            required_page_types=("home",),
        ).to_dict(),
        H5_TASK_FRONTIER_METADATA_KEY: _frontier_metadata(),
        H5_COVERAGE_METADATA_KEY: _coverage(
            capture_id=ids["old_capture"],
            revision=3,
        ),
    }
    result_observation = SurfaceObservation(
        capture_id=ids["old_capture"],
        surface_kind=SurfaceKind.WEB,
        location="https://example.test/home",
        state_version="state:settlement-result",
        observed_at=now + timedelta(milliseconds=100),
        page_type="home",
        page_state="stable",
        metadata=result_metadata,
    )
    result_frontier = FrontierSnapshot(
        snapshot_id=f"frontier:result:{ids['old_capture']}",
        observation=result_observation,
        candidates=(),
        visited_state_ids=(parent.frontier.observation.state_version,),
    )
    result = build_exploration_work(
        task_id=parent.task_id,
        session_id=parent.session_id,
        branch_id=f"settlement-result:{ids['old_capture']}",
        objective=parent.objective,
        surface_kind=parent.surface_kind,
        source_capture_id=ids["old_capture"],
        frontier_snapshot_id=result_frontier.snapshot_id,
        frontier=result_frontier,
        boundaries=parent.boundaries,
        budget=ExplorationBudget(
            max_actions=parent.budget.max_actions,
            remaining_actions=parent.budget.remaining_actions - 1,
            max_depth=parent.budget.max_depth,
            remaining_duration_ms=parent.budget.remaining_duration_ms - 100,
        ),
        deadline=deadline,
    )
    return parent, result


async def _insert_page_captures(connection, ids: dict[str, str]) -> None:
    for capture_id, sequence, role in (
        (ids["capture"], 1, "bootstrap"),
        (ids["old_capture"], 2, "after_action"),
    ):
        await connection.execute(
            """
            INSERT INTO qc_page_capture(
                id, task_id, agent_run_id, page_seq, page_slug, url, page_type,
                page_state, capture_status, metadata, surface_session_id,
                capture_role, session_command_sequence, captured_at
            )
            VALUES(
                $1::uuid, $2::uuid, $3::uuid, $4::integer, 'settlement',
                'https://example.test/', 'home', 'stable', 'captured',
                '{"finalization_epoch": 0}'::jsonb, $5, $6, $4::integer, now()
            )
            """,
            capture_id,
            ids["task"],
            ids["run"],
            sequence,
            ids["session"],
            role,
        )
    await connection.execute(
        """
        UPDATE qc_capture
        SET metadata = '{"finalization_epoch": 0}'::jsonb
        WHERE id = $1::uuid
        """,
        ids["old_capture"],
    )


async def _prepare_running(connection, pool, ids):
    await _insert_page_captures(connection, ids)
    parent, result = _work_pair(ids)
    repository = PostgresExplorerWorkRepository(pool=pool)
    published = await repository.publish(parent)
    assert published.created is True
    lease = await repository.claim(
        command=ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="settlement-worker",
            task_id=ids["task"],
        ),
        lease_token=ids["lease_token"],
    )
    assert lease is not None
    row_version = await repository.transition(
        command=TransitionExplorerWorkCommand(
            work_id=parent.work_id,
            expected_status=ExplorerWorkStatus.LEASED,
            target_status=ExplorerWorkStatus.RUNNING,
            lease_token=lease.lease_token,
            row_version=lease.row_version,
        )
    )
    expired_deadline = datetime.now(UTC) - timedelta(seconds=1)
    await connection.execute(
        """
        UPDATE qc_v2_work_item
        SET deadline = $2::timestamptz,
            work_payload = jsonb_set(work_payload, '{deadline}', $3::jsonb)
        WHERE work_id = $1
        """,
        parent.work_id,
        expired_deadline,
        json.dumps(expired_deadline.isoformat()),
    )
    return (
        repository,
        replace(parent, deadline=expired_deadline),
        replace(result, deadline=expired_deadline),
        lease.lease_token,
        row_version,
    )


async def _insert_alternate_agent_run(connection, ids: dict[str, str]) -> str:
    run_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_agent_run(id, task_id, agent_type, agent_name)
        VALUES($1::uuid, $2::uuid, 'explorer', 'settlement-alternate-run')
        """,
        run_id,
        ids["task"],
    )
    return run_id


async def _seal_task_for_test(connection, task_id: str) -> None:
    """在外层事务内构造合法 sealed 任务，调用结束后由测试回滚。"""
    gate_snapshot_id = f"settlement-sealed-{uuid4()}"
    await connection.execute(
        """
        INSERT INTO qc_completion_gate_snapshot(
            gate_snapshot_id, task_id, finalization_epoch, evaluation_sequence,
            protocol_version, facts_hash, exploration_terminal,
            no_pending_action, no_pending_capture_commit,
            no_nonterminal_enforced_work_item, no_active_session_request,
            candidate_rule_coverage_terminal, no_background_execution,
            review_policy_satisfied, pending_action_count,
            pending_capture_commit_count, nonterminal_enforced_work_item_count,
            active_session_request_count, candidate_rule_count,
            terminal_candidate_rule_count, background_execution_count,
            open_review_count, review_generation, block_reasons, gate_passed,
            evaluated_at
        )
        VALUES(
            $2, $1::uuid, 0, 1,
            'task-completion-gate.v1', 'settlement-sealed-facts',
            true, true, true, true, true, true, true, true,
            0, 0, 0, 0, 0, 0, 0, 0, 0, '[]'::jsonb, true, now()
        )
        """,
        task_id,
        gate_snapshot_id,
    )
    await connection.execute(
        """
        UPDATE qc_task
            SET status = 'canceled', execution_status = 'canceled',
                finalization_state = 'sealed',
                completion_gate_snapshot_id = $2,
            quality_outcome = 'unknown',
            exploration_draining_at = now(),
            session_draining_at = now(),
            sealed_at = now()
        WHERE id = $1::uuid
        """,
        task_id,
        gate_snapshot_id,
    )


def _terminal(parent) -> ExplorationTerminal:
    return ExplorationTerminal(
        work_id=parent.work_id,
        task_id=parent.task_id,
        status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        reason="deadline 已到，保存本轮结果并停止探索",
        outcome="bounded",
        reason_code="h5_work_deadline_reached",
        coverage_summary={
            "consumed_action_count": 999,
            "result_capture_id": "caller-controlled-value",
        },
    )


@pytest.mark.asyncio
async def test_deadline_settlement_preserves_parent_and_writes_coverage_atomically() -> None:
    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        pool,
        ids,
    ):
        ids["lease_token"] = "00000000-0000-0000-0000-000000000017"
        repository, parent, result, lease_token, row_version = await _prepare_running(
            connection, pool, ids
        )
        before = await connection.fetchrow(
            """
            SELECT status, source_capture_id::text AS source_capture_id,
                   work_payload, terminal_payload, lease_token, lease_expires_at
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            parent.work_id,
        )
        outbox_before = await connection.fetchval(
            "SELECT count(*) FROM qc_outbox_event WHERE task_id = $1::uuid",
            ids["task"],
        )

        settled_version = await repository.transition(
            command=TransitionExplorerWorkCommand(
                work_id=parent.work_id,
                expected_status=ExplorerWorkStatus.RUNNING,
                target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                lease_token=lease_token,
                row_version=row_version,
                terminal=_terminal(parent),
                error_code="H5_WORK_DEADLINE_REACHED",
                settlement_work=result,
            )
        )
        after = await connection.fetchrow(
            """
            SELECT status, row_version, source_capture_id::text AS source_capture_id,
                   work_payload, terminal_payload, lease_token, lease_expires_at
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            parent.work_id,
        )
        assert settled_version == row_version + 1
        assert after["status"] == "budget_exhausted"
        assert after["source_capture_id"] == before["source_capture_id"] == parent.source_capture_id
        assert after["work_payload"] == before["work_payload"]
        terminal_payload = json.loads(after["terminal_payload"])
        assert terminal_payload["coverage_summary"]["result_capture_id"] == ids[
            "old_capture"
        ]
        assert terminal_payload["coverage_summary"]["consumed_action_count"] == 1
        scheduler_summary = terminal_payload["coverage_summary"][
            BOUNDED_EXPLORATION_STATE_KEY
        ]
        assert scheduler_summary["consumed_actions"]["required"] == 1
        assert after["lease_token"] is None
        assert after["lease_expires_at"] is None
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_v2_work_item WHERE work_id = $1",
                result.work_id,
            )
            == 0
        )
        coverage = await connection.fetchrow(
            """
            SELECT task_id::text AS task_id, agent_run_id::text AS agent_run_id,
                   finalization_epoch, revision
            FROM qc_h5_coverage_snapshot
            WHERE task_id = $1::uuid AND agent_run_id = $2::uuid
              AND finalization_epoch = 0 AND revision = 3
            """,
            ids["task"],
            ids["run"],
        )
        assert coverage is not None
        assert coverage["task_id"] == ids["task"]
        assert coverage["agent_run_id"] == ids["run"]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_outbox_event WHERE task_id = $1::uuid",
                ids["task"],
            )
            == outbox_before
        )

        duplicate_before = await connection.fetchrow(
            """
            SELECT status, row_version, terminal_payload, source_capture_id::text,
                   lease_token, lease_expires_at
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            parent.work_id,
        )
        with pytest.raises(ExplorerWorkPersistenceError, match="RUNNING"):
            await repository.transition(
                command=TransitionExplorerWorkCommand(
                    work_id=parent.work_id,
                    expected_status=ExplorerWorkStatus.RUNNING,
                    target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                    lease_token=lease_token,
                    row_version=row_version,
                    terminal=_terminal(parent),
                    settlement_work=result,
                )
            )
        duplicate_after = await connection.fetchrow(
            """
            SELECT status, row_version, terminal_payload, source_capture_id::text,
                   lease_token, lease_expires_at
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            parent.work_id,
        )
        assert duplicate_after == duplicate_before
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_outbox_event WHERE task_id = $1::uuid",
                ids["task"],
            )
            == outbox_before
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_kind",
    [
        "lease",
        "expired_lease",
        "epoch",
        "session",
        "deadline",
        "sealed",
        "row_version",
        "parent_capture_run",
        "result_capture_run",
        "budget_zero",
        "budget_two",
        "max_depth",
    ],
)
async def test_deadline_settlement_rejects_scope_or_lease_without_parent_write(
    invalid_kind: str,
) -> None:
    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        pool,
        ids,
    ):
        ids["lease_token"] = "00000000-0000-0000-0000-000000000018"
        repository, parent, result, lease_token, row_version = await _prepare_running(
            connection, pool, ids
        )
        if invalid_kind == "sealed":
            before = await connection.fetchrow(
                """
                SELECT status, row_version, terminal_payload,
                       source_capture_id::text, lease_token, lease_expires_at
                FROM qc_v2_work_item WHERE work_id = $1
                """,
                parent.work_id,
            )
            pinned_repository = PostgresExplorerWorkRepository(
                pool=_PinnedConnectionPool(connection)
            )
            transaction = connection.transaction()
            await transaction.start()
            try:
                await _seal_task_for_test(connection, ids["task"])
                with pytest.raises(ExplorerWorkPersistenceError):
                    await pinned_repository.transition(
                        command=TransitionExplorerWorkCommand(
                            work_id=parent.work_id,
                            expected_status=ExplorerWorkStatus.RUNNING,
                            target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                            lease_token=lease_token,
                            row_version=row_version,
                            terminal=_terminal(parent),
                            settlement_work=result,
                        )
                    )
            finally:
                await transaction.rollback()
            after = await connection.fetchrow(
                """
                SELECT status, row_version, terminal_payload,
                       source_capture_id::text, lease_token, lease_expires_at
                FROM qc_v2_work_item WHERE work_id = $1
                """,
                parent.work_id,
            )
            assert after == before
            return
        if invalid_kind == "lease":
            lease_token = "00000000-0000-0000-0000-000000000099"
        elif invalid_kind == "expired_lease":
            await connection.execute(
                """
                UPDATE qc_v2_work_item
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE work_id = $1
                """,
                parent.work_id,
            )
        elif invalid_kind == "epoch":
            await connection.execute(
                "UPDATE qc_capture SET metadata = '{\"finalization_epoch\": 1}'::jsonb "
                "WHERE id = $1::uuid",
                ids["old_capture"],
            )
        elif invalid_kind == "session":
            result = replace(result, session_id="foreign-session")
        elif invalid_kind == "deadline":
            result = replace(result, deadline=parent.deadline + timedelta(seconds=1))
        elif invalid_kind == "row_version":
            await connection.execute(
                "UPDATE qc_v2_work_item SET row_version = row_version + 1 "
                "WHERE work_id = $1",
                parent.work_id,
            )
        elif invalid_kind in {"parent_capture_run", "result_capture_run"}:
            alternate_run = await _insert_alternate_agent_run(connection, ids)
            capture_id = (
                ids["capture"]
                if invalid_kind == "parent_capture_run"
                else ids["old_capture"]
            )
            await connection.execute(
                "UPDATE qc_capture SET agent_run_id = $2::uuid WHERE id = $1::uuid",
                capture_id,
                alternate_run,
            )
        elif invalid_kind == "budget_zero":
            result = replace(
                result,
                budget=replace(
                    result.budget,
                    remaining_actions=parent.budget.remaining_actions,
                    remaining_business_actions=parent.budget.remaining_actions,
                ),
            )
        elif invalid_kind == "budget_two":
            result = replace(
                result,
                budget=replace(
                    result.budget,
                    remaining_actions=parent.budget.remaining_actions - 2,
                    remaining_business_actions=parent.budget.remaining_actions - 2,
                ),
            )
        elif invalid_kind == "max_depth":
            result = replace(
                result,
                boundaries=replace(
                    result.boundaries,
                    max_navigation_depth=parent.boundaries.max_navigation_depth + 1,
                ),
                budget=replace(result.budget, max_depth=parent.budget.max_depth + 1),
            )

        before = await connection.fetchrow(
            """
            SELECT status, row_version, terminal_payload,
                   source_capture_id::text, lease_token, lease_expires_at
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            parent.work_id,
        )
        with pytest.raises(ExplorerWorkPersistenceError):
            await repository.transition(
                command=TransitionExplorerWorkCommand(
                    work_id=parent.work_id,
                    expected_status=ExplorerWorkStatus.RUNNING,
                    target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                    lease_token=lease_token,
                    row_version=row_version,
                    terminal=_terminal(parent),
                    settlement_work=result,
                )
            )
        after = await connection.fetchrow(
            """
            SELECT status, row_version, terminal_payload,
                   source_capture_id::text, lease_token, lease_expires_at
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            parent.work_id,
        )
        assert after == before


@pytest.mark.asyncio
async def test_deadline_settlement_rolls_back_parent_when_coverage_is_invalid() -> None:
    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        pool,
        ids,
    ):
        ids["lease_token"] = "00000000-0000-0000-0000-000000000019"
        repository, parent, result, lease_token, row_version = await _prepare_running(
            connection, pool, ids
        )
        invalid_metadata = dict(result.frontier.observation.metadata)
        invalid_metadata[H5_COVERAGE_METADATA_KEY] = {"invalid": True}
        invalid_observation = replace(
            result.frontier.observation,
            metadata=invalid_metadata,
        )
        invalid_frontier = replace(result.frontier, observation=invalid_observation)
        invalid_result = replace(result, frontier=invalid_frontier)
        with pytest.raises(ExplorerWorkPersistenceError, match="coverage"):
            await repository.transition(
                command=TransitionExplorerWorkCommand(
                    work_id=parent.work_id,
                    expected_status=ExplorerWorkStatus.RUNNING,
                    target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                    lease_token=lease_token,
                    row_version=row_version,
                    terminal=_terminal(parent),
                    settlement_work=invalid_result,
                )
            )
        row = await connection.fetchrow(
            "SELECT status, row_version, terminal_payload FROM qc_v2_work_item WHERE work_id = $1",
            parent.work_id,
        )
        assert row["status"] == "running"
        assert row["row_version"] == row_version
        assert row["terminal_payload"] is None
