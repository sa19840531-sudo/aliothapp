"""读取 H5/App 探索调度所需的真实 PostgreSQL 事实。

该仓储只读三类已存在的权威写入：H5 Coverage Snapshot、App Evidence Index
和活动中的 Evidence Request。所有查询先锁定同一任务、会话和当前
``finalization_epoch``，再按 Agent Run 过滤，禁止跨运行借用缺口。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from services.api.app.application.app_rule_action_readiness import app_rule_action_requirement
from services.api.app.application.app_rule_coverage_facts import (
    APP_RULE_COVERAGE_FACT_KEY,
    project_app_rule_coverage_facts,
)
from services.api.app.application.exploration_scheduler_facts import (
    ExplorationSchedulerFactsReader,
)
from services.api.app.infrastructure.db.app_rule_action_facts_repository import (
    PostgresAppRuleActionFactsReader,
)
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    _capture_page_type_sql,
    _occluded_placeholder_sql,
    _placeholder_sql,
)
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    read_frozen_task_rule_inventory_on_connection,
)

logger = logging.getLogger(__name__)


class ExplorationSchedulerFactsScopeError(RuntimeError):
    """任务、会话和当前封存阶段不能组成一致读取范围。"""


class PostgresExplorationSchedulerFactsReader(ExplorationSchedulerFactsReader):
    """从同一 Task/Run/Session/Epoch 读取调度事实。"""

    def __init__(
        self, *, database_url: str | None = None, pool: Any | None = None,
        action_facts_reader: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._action_facts_reader = action_facts_reader or PostgresAppRuleActionFactsReader(
            database_url=database_url, pool=pool,
        )

    async def read_h5_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None = None,
    ) -> Mapping[str, object]:
        _validate_scope_arguments(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            async with _repeatable_read_snapshot(connection):
                scope = await self._read_scope(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    session_id=session_id,
                    finalization_epoch=finalization_epoch,
                    surface_kinds=("web",),
                )
                snapshot = await connection.fetchrow(
                    """
                    SELECT
                        coverage_snapshot_id,
                        task_id::text AS task_id,
                        agent_run_id::text AS agent_run_id,
                        finalization_epoch,
                        revision,
                        authority_source,
                        required_page_types,
                        covered_page_types,
                        missing_page_types,
                        coverage_scopes,
                        assessment_scope_ready,
                        frontier_terminal,
                        completion_ready,
                        schema_version,
                        payload_hash,
                        created_at
                    FROM qc_h5_coverage_snapshot
                    WHERE task_id = $1::uuid
                      AND agent_run_id = $2::uuid
                      AND finalization_epoch = $3
                    ORDER BY revision DESC, created_at DESC
                    LIMIT 1
                    """,
                    task_id,
                    agent_run_id,
                    scope["finalization_epoch"],
                )
                requests = await self._read_rule_evidence_requests(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    session_id=session_id,
                    finalization_epoch=scope["finalization_epoch"],
                    surface_kind=str(scope["surface_kind"]),
                )
        facts: dict[str, object] = {
            "exploration_scheduler_facts_scope": scope,
            # H5 Work 会在每轮探索中内嵌调度事实。完整 request_payload 已由
            # qc_evidence_request 权威持久化，调度器只消费缺口页型；这里保留
            # 请求身份和 evidence_gap，避免候选较多时重复正文挤占协议容量。
            "rule_evidence_requests": [_h5_rule_request_fact(request) for request in requests],
        }
        if snapshot is not None:
            # 运行时的 ``h5_coverage_snapshot`` 是由本轮 Frontier 生成的
            # H5CoverageSnapshot，不能用数据库行的列名直接覆盖它；原始表事实
            # 使用独立键保留，调度器仍会从该事实投影缺口。
            facts["h5_coverage_snapshot_fact"] = _h5_snapshot_payload(snapshot)
        return facts

    async def read_app_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None = None,
    ) -> Mapping[str, object]:
        _validate_scope_arguments(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            async with _repeatable_read_snapshot(connection):
                scope = await self._read_scope(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    session_id=session_id,
                    finalization_epoch=finalization_epoch,
                    surface_kinds=("android", "ios"),
                )
                index = await connection.fetchrow(
                    """
                    SELECT
                        index_id,
                        task_id::text AS task_id,
                        agent_run_id::text AS agent_run_id,
                        build_id,
                        surface_session_id,
                        protocol_version,
                        status,
                        result_context_sha256,
                        outcome,
                        terminal_reason_code,
                        content_sha256,
                        evidence_gaps,
                        sealed_at,
                        created_at,
                        updated_at
                    FROM qc_app_evidence_index
                    WHERE task_id = $1::uuid
                      AND agent_run_id = $2::uuid
                      AND surface_session_id = $3
                      AND build_id IS NOT DISTINCT FROM $4
                    """,
                    task_id,
                    agent_run_id,
                    session_id,
                    scope["platform_build_ref"],
                )
                entries: list[dict[str, object]] = []
                if index is not None:
                    rows = await connection.fetch(
                        f"""
                        SELECT
                            entry.sequence_no,
                            entry.capture_id::text AS capture_id,
                            entry.evidence_manifest_id,
                            entry.evidence_manifest_version,
                            entry.semantic_role,
                            entry.surface_phase,
                            entry.exploration_work_id,
                            entry.action_execution_id,
                            entry.candidate_id,
                            entry.page_identity,
                            entry.captured_at,
                            entry.metadata,
                            capture.id::text AS verified_capture_id,
                            capture.task_id::text AS verified_capture_task_id,
                            capture.agent_run_id::text AS verified_capture_agent_run_id,
                            capture.surface_kind AS verified_capture_surface_kind,
                            capture.surface_session_id AS verified_capture_session_id,
                            capture.parent_capture_id::text AS verified_capture_parent_capture_id,
                            capture.action_execution_id AS verified_capture_action_execution_id,
                            capture.capture_status AS verified_capture_status,
                            capture.capture_role AS verified_capture_role,
                            capture.metadata AS verified_capture_metadata,
                            ({capture_epoch_sql("capture")})
                                AS verified_capture_finalization_epoch,
                            capture_session.session_id AS verified_capture_session_record_id,
                            capture_session.surface_kind AS verified_capture_session_surface_kind,
                            capture_session.finalization_epoch
                                AS verified_capture_session_finalization_epoch,
                            action.action_execution_id AS verified_action_execution_id,
                            action.task_id::text AS verified_action_task_id,
                            action.session_id AS verified_action_session_id,
                            action.finalization_epoch AS verified_action_finalization_epoch,
                            action.source_capture_id::text AS verified_action_source_capture_id,
                            action.result_capture_id::text AS verified_action_result_capture_id,
                            action_source.id::text AS verified_action_source_id,
                            action_source.task_id::text AS verified_action_source_task_id,
                            action_source.agent_run_id::text AS verified_action_source_agent_run_id,
                            action_source.surface_kind AS verified_action_source_surface_kind,
                            action_source.surface_session_id AS verified_action_source_session_id,
                            action_source.parent_capture_id::text
                                AS verified_action_source_parent_capture_id,
                            action_source.action_execution_id
                                AS verified_action_source_action_execution_id,
                            action_source.capture_status AS verified_action_source_status,
                            action_source.metadata AS verified_action_source_metadata,
                            ({capture_epoch_sql("action_source")})
                                AS verified_action_source_finalization_epoch,
                            action_source_session.session_id
                                AS verified_action_source_session_record_id,
                            action_source_session.surface_kind
                                AS verified_action_source_session_surface_kind,
                            action_source_session.finalization_epoch
                                AS verified_action_source_session_finalization_epoch,
                            action_target.id::text AS verified_action_target_id,
                            action_target.task_id::text AS verified_action_target_task_id,
                            action_target.agent_run_id::text AS verified_action_target_agent_run_id,
                            action_target.surface_kind AS verified_action_target_surface_kind,
                            action_target.surface_session_id AS verified_action_target_session_id,
                            action_target.parent_capture_id::text
                                AS verified_action_target_parent_capture_id,
                            action_target.action_execution_id
                                AS verified_action_target_action_execution_id,
                            action_target.capture_status AS verified_action_target_status,
                            action_target.metadata AS verified_action_target_metadata,
                            ({capture_epoch_sql("action_target")})
                                AS verified_action_target_finalization_epoch,
                            action_target_session.session_id
                                AS verified_action_target_session_record_id,
                            action_target_session.surface_kind
                                AS verified_action_target_session_surface_kind,
                            action_target_session.finalization_epoch
                                AS verified_action_target_session_finalization_epoch,
                            work.work_id AS verified_work_id,
                            work.task_id::text AS verified_work_task_id,
                            work.session_id AS verified_work_session_id,
                            work.source_capture_id::text AS verified_work_source_capture_id,
                            work.finalization_epoch AS verified_work_finalization_epoch,
                            work_source.id::text AS verified_work_source_id,
                            work_source.task_id::text AS verified_work_source_task_id,
                            work_source.agent_run_id::text AS verified_work_source_agent_run_id,
                            work_source.surface_kind AS verified_work_source_surface_kind,
                            work_source.surface_session_id AS verified_work_source_session_id,
                            work_source.capture_status AS verified_work_source_status,
                            work_source.metadata AS verified_work_source_metadata,
                            ({capture_epoch_sql("work_source")})
                                AS verified_work_source_finalization_epoch,
                            work_source_session.session_id
                                AS verified_work_source_session_record_id,
                            work_source_session.surface_kind
                                AS verified_work_source_session_surface_kind,
                            work_source_session.finalization_epoch
                                AS verified_work_source_session_finalization_epoch
                        FROM qc_app_evidence_index_entry AS entry
                        LEFT JOIN qc_capture AS capture
                            ON capture.id = entry.capture_id
                        LEFT JOIN qc_surface_session AS capture_session
                            ON capture_session.task_id = capture.task_id
                           AND capture_session.session_id = capture.surface_session_id
                        LEFT JOIN qc_action_execution AS action
                            ON action.action_execution_id = capture.action_execution_id
                           AND action.task_id = capture.task_id
                        LEFT JOIN qc_capture AS action_source
                            ON action_source.id = action.source_capture_id
                           AND action_source.task_id = action.task_id
                        LEFT JOIN qc_surface_session AS action_source_session
                            ON action_source_session.task_id = action_source.task_id
                           AND action_source_session.session_id = action_source.surface_session_id
                        LEFT JOIN qc_capture AS action_target
                            ON action_target.id = action.result_capture_id
                           AND action_target.task_id = action.task_id
                        LEFT JOIN qc_surface_session AS action_target_session
                            ON action_target_session.task_id = action_target.task_id
                           AND action_target_session.session_id = action_target.surface_session_id
                        LEFT JOIN qc_v2_work_item AS work
                            ON work.work_id = entry.exploration_work_id
                        LEFT JOIN qc_capture AS work_source
                            ON work_source.id = work.source_capture_id
                           AND work_source.task_id = work.task_id
                        LEFT JOIN qc_surface_session AS work_source_session
                            ON work_source_session.task_id = work_source.task_id
                           AND work_source_session.session_id = work_source.surface_session_id
                        WHERE entry.index_id = $1
                        ORDER BY entry.sequence_no
                        """,
                        str(index["index_id"]),
                    )
                    _validate_app_index_entry_scope(
                        rows,
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        session_id=session_id,
                        finalization_epoch=int(scope["finalization_epoch"]),
                    )
                    entries = [_app_index_entry_payload(row) for row in rows]
                requests = await self._read_rule_evidence_requests(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    session_id=session_id,
                    finalization_epoch=scope["finalization_epoch"],
                    surface_kind=str(scope["surface_kind"]),
                )
                rule_coverage = await self._read_app_rule_coverage(connection, scope=scope)
        facts: dict[str, object] = {
            "exploration_scheduler_facts_scope": scope,
            "rule_evidence_requests": requests,
        }
        if rule_coverage is not None:
            facts[APP_RULE_COVERAGE_FACT_KEY] = rule_coverage
        if index is not None:
            facts["app_evidence_index"] = _app_index_payload(index, entries)
        return facts

    async def _read_app_rule_coverage(
        self, connection: Any, *, scope: Mapping[str, object]
    ) -> dict[str, object] | None:
        """冻结契约与当前证据在同一快照读取，历史 Run 不能填平本次探索缺口。"""
        inventory = await read_frozen_task_rule_inventory_on_connection(
            connection, task_id=str(scope["task_id"])
        )
        if inventory is None:
            return None
        arguments = (
            scope["task_id"],
            scope["agent_run_id"],
            scope["session_id"],
            scope["finalization_epoch"],
        )
        executions = await connection.fetch(
            f"""
            WITH scoped_executions AS (
            SELECT execution.rule_id::text AS rule_id,
                   {completed_rule_execution_sql("execution")} AS completed,
                   {_placeholder_sql("execution")} AS is_placeholder,
                   {_occluded_placeholder_sql("execution")} AS is_occluded,
                   {_capture_page_type_sql("execution")} AS page_type
            FROM qc_rule_execution AS execution
            INNER JOIN qc_capture AS capture
              ON capture.id = execution.page_capture_id AND capture.task_id = execution.task_id
            WHERE execution.task_id = $1::uuid AND capture.agent_run_id = $2::uuid
              AND capture.surface_session_id = $3
              AND ({capture_epoch_sql("capture")}) = $4
              AND (NOT (execution.judge_input ? 'finalization_epoch')
                   OR execution.judge_input->>'finalization_epoch' = $4::text)
            ), effective_executions AS (
                SELECT source.* FROM scoped_executions AS source
                WHERE source.is_placeholder IS NOT TRUE
                   OR NOT EXISTS (
                       SELECT 1 FROM scoped_executions AS concluded
                       WHERE concluded.rule_id = source.rule_id
                         AND concluded.completed IS TRUE
                         AND concluded.is_placeholder IS NOT TRUE
                         AND (source.is_occluded IS NOT TRUE
                              OR source.page_type = concluded.page_type)
                   )
            )
            SELECT rule_id, bool_and(completed IS TRUE) AS completed
            FROM effective_executions GROUP BY rule_id
            """,
            *arguments,
        )
        captures = await connection.fetch(
            f"""
            SELECT capture.id::text AS capture_id,
                   COALESCE(page_capture.url, capture.metadata->>'url',
                            'app://' || app_capture.package_name || '/' || app_capture.activity)
                       AS page_url,
                   visual.visual_page_type AS page_type, visual.page_state,
                   visual.overlay_signature, visual.surface_facts,
                   LOWER(COALESCE(capture.metadata->>'session_auth', 'unknown')) AS session_auth
            FROM qc_capture AS capture
            LEFT JOIN qc_page_capture AS page_capture ON page_capture.id = capture.id
            INNER JOIN LATERAL (
                SELECT observation.* FROM qc_app_visual_observation AS observation
                WHERE observation.capture_id = capture.id AND observation.task_id = capture.task_id
                  AND observation.session_id = capture.surface_session_id
                ORDER BY observation.created_at DESC, observation.observation_id DESC LIMIT 1
            ) AS visual ON TRUE
            INNER JOIN qc_app_capture AS app_capture
              ON app_capture.capture_id = capture.id
             AND app_capture.screenshot_artifact_id = visual.screenshot_artifact_id
            INNER JOIN qc_evidence_artifact AS artifact
              ON artifact.id = app_capture.screenshot_artifact_id
             AND artifact.task_id = capture.task_id AND artifact.page_capture_id = capture.id
             AND artifact.artifact_type = 'screenshot'
             AND artifact.checksum = app_capture.image_signature
            INNER JOIN LATERAL (
                SELECT candidate.quality_status FROM qc_evidence_manifest AS candidate
                WHERE candidate.task_id = capture.task_id AND candidate.capture_id = capture.id
                  AND candidate.session_id = capture.surface_session_id
                ORDER BY candidate.manifest_version DESC LIMIT 1
            ) AS manifest ON manifest.quality_status = 'pass'
            WHERE capture.task_id = $1::uuid AND capture.agent_run_id = $2::uuid
              AND capture.surface_session_id = $3 AND ({capture_epoch_sql("capture")}) = $4
              AND capture.capture_status = 'captured' AND capture.surface_kind IN ('android', 'ios')
            ORDER BY capture.created_at DESC, capture.id DESC
            """,
            *arguments,
        )
        pending = await connection.fetchval(
            """
            SELECT count(*)::integer FROM qc_inspection_work_item AS work
            INNER JOIN qc_capture AS capture
              ON capture.id = work.page_capture_id AND capture.task_id = work.task_id
            WHERE work.task_id = $1::uuid AND capture.agent_run_id = $2::uuid
              AND capture.surface_session_id = $3 AND work.finalization_epoch = $4
              AND work.session_dependency = 'active_session' AND work.control_mode = 'enforced'
              AND work.status IN ('pending', 'leased', 'running', 'retry_wait')
            """,
            *arguments,
        )
        completed = {str(row["rule_id"]) for row in executions if row["completed"]}
        action_facts = None
        if captures and any(
            rule["rule_id"] not in completed and app_rule_action_requirement(rule)
            for rule in inventory["rules"] if rule.get("applicability") == "applicable"
        ):
            try:
                # 仅为明确的动作契约读最新两步，所有待执行规则共用；不全量拉历史截图。
                action_facts = await self._action_facts_reader.read_on_connection(
                    connection, task_id=str(scope["task_id"]),
                    capture_id=str(captures[0]["capture_id"]),
                    agent_run_id=str(scope["agent_run_id"]),
                )
            except Exception:  # noqa: BLE001 - 读取失败保留具体规则缺口，不丢冻结清单需求
                logger.warning("读取 App 调度业务动作材料失败，保留待补证需求", exc_info=True)
        return project_app_rule_coverage_facts(
            inventory,
            scope=scope,
            completed_rule_ids=completed,
            captures=[
                {**dict(row), "surface_facts": _json_value(row["surface_facts"])}
                for row in captures
            ],
            pending_inspection_work_count=int(pending or 0),
            action_facts=action_facts,
        )

    async def _read_scope(
        self,
        connection: Any,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None,
        surface_kinds: Sequence[str],
    ) -> dict[str, object]:
        row = await connection.fetchrow(
            """
            SELECT
                task.id::text AS task_id,
                run.id::text AS agent_run_id,
                session.session_id,
                task.finalization_epoch AS task_finalization_epoch,
                session.finalization_epoch AS session_finalization_epoch,
                session.surface_kind,
                session.status AS session_status,
                session.platform_build_ref
            FROM qc_task AS task
            INNER JOIN qc_agent_run AS run
                ON run.id = $2::uuid
               AND run.task_id = task.id
            INNER JOIN qc_surface_session AS session
                ON session.task_id = task.id
               AND session.session_id = $3
               AND session.finalization_epoch = task.finalization_epoch
               AND session.surface_kind = ANY($5::text[])
               AND session.status IN ('starting', 'active', 'draining')
            WHERE task.id = $1::uuid
              AND ($4::integer IS NULL OR task.finalization_epoch = $4)
            """,
            task_id,
            agent_run_id,
            session_id,
            finalization_epoch,
            list(surface_kinds),
        )
        if row is None:
            raise ExplorationSchedulerFactsScopeError(
                "任务、会话或 finalization_epoch 不属于同一当前运行范围"
            )
        epoch = int(row["task_finalization_epoch"])
        if int(row["session_finalization_epoch"]) != epoch:
            raise ExplorationSchedulerFactsScopeError("任务与会话的 finalization_epoch 不一致")
        return {
            "task_id": str(row["task_id"]),
            "agent_run_id": str(row["agent_run_id"]),
            "session_id": str(row["session_id"]),
            "finalization_epoch": epoch,
            "surface_kind": str(row["surface_kind"]),
            "session_status": str(row["session_status"]),
            "platform_build_ref": row["platform_build_ref"],
            "source": "current_task_surface_session",
        }

    async def _read_rule_evidence_requests(
        self,
        connection: Any,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int,
        surface_kind: str,
    ) -> list[dict[str, object]]:
        rows = await connection.fetch(
            f"""
            SELECT
                request.request_id,
                request.task_id::text AS task_id,
                request.rule_id::text AS rule_id,
                request.rule_code,
                request.target_capture_id::text AS target_capture_id,
                request.session_dependency,
                request.status,
                request.request_payload,
                request.finalization_epoch,
                request.created_at,
                request.updated_at,
                capture.surface_session_id,
                work_item.consumer_agent_run_id::text AS work_consumer_run_id,
                attempt.consumer_agent_run_id::text AS attempt_consumer_run_id
            FROM qc_evidence_request AS request
            INNER JOIN qc_inspection_work_item AS work_item
                ON work_item.id = request.work_item_id
               AND work_item.task_id = request.task_id
            INNER JOIN qc_inspection_attempt AS attempt
                ON attempt.id = request.attempt_id
               AND attempt.work_item_id = work_item.id
            INNER JOIN qc_capture AS capture
                ON capture.id = request.target_capture_id
               AND capture.task_id = request.task_id
               AND capture.agent_run_id = $2::uuid
               AND capture.surface_session_id = $3
               AND capture.surface_kind = $5
            WHERE request.task_id = $1::uuid
              AND request.finalization_epoch = $4
              AND work_item.finalization_epoch = $4
              AND (
                    work_item.consumer_agent_run_id = $2::uuid
                    OR attempt.consumer_agent_run_id = $2::uuid
                  )
              AND request.status IN ('pending', 'routed', 'running')
              AND ({capture_epoch_sql("capture")}) = $4
            ORDER BY request.created_at, request.request_id
            """,
            task_id,
            agent_run_id,
            session_id,
            finalization_epoch,
            surface_kind,
        )
        return [_rule_request_payload(row) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


@asynccontextmanager
async def _repeatable_read_snapshot(connection: object):
    """在仓储内部固定一轮只读 Repeatable Read 快照。"""

    transaction_factory = getattr(connection, "transaction", None)
    if not callable(transaction_factory):
        # 纯单元测试使用轻量假连接；真实 asyncpg 连接一定会进入事务。
        yield
        return
    async with transaction_factory(isolation="repeatable_read", readonly=True):
        yield


def _validate_scope_arguments(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    finalization_epoch: int | None,
) -> None:
    for name, value in (
        ("task_id", task_id),
        ("agent_run_id", agent_run_id),
        ("session_id", session_id),
    ):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} 必须为无首尾空白的非空字符串")
    if finalization_epoch is not None and (
        isinstance(finalization_epoch, bool)
        or not isinstance(finalization_epoch, int)
        or finalization_epoch < 0
    ):
        raise ValueError("finalization_epoch 必须为非负整数或 None")


def _h5_snapshot_payload(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "source": "qc_h5_coverage_snapshot",
        "coverage_snapshot_id": str(row["coverage_snapshot_id"]),
        "task_id": str(row["task_id"]),
        "agent_run_id": str(row["agent_run_id"]),
        "finalization_epoch": int(row["finalization_epoch"]),
        "revision": int(row["revision"]),
        "authority_source": str(row["authority_source"]),
        "required_page_types": _json_value(row["required_page_types"]),
        "covered_page_types": _json_value(row["covered_page_types"]),
        "missing_page_types": _json_value(row["missing_page_types"]),
        "coverage_scopes": _json_value(row["coverage_scopes"]),
        "assessment_scope_ready": bool(row["assessment_scope_ready"]),
        "frontier_terminal": bool(row["frontier_terminal"]),
        "completion_ready": bool(row["completion_ready"]),
        "schema_version": str(row["schema_version"]),
        "payload_hash": str(row["payload_hash"]),
        "created_at": _datetime_value(row["created_at"]),
    }


def _validate_app_index_entry_scope(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    finalization_epoch: int,
) -> None:
    """拒绝把其他 Run、Session 或 Epoch 的 Entry 当成本轮 App 事实。

    Capture 没有独立的 epoch 列，不能因为 ``exploration_work_id`` 缺失就跳过
    校验。每个 Entry 先通过 Capture -> Surface Session 的身份关系，再通过
    Capture metadata 或实际 Action source/result 关联证明当前 epoch。Work 的
    source_capture_id 是当前工作起点，动作后的 Entry 通常是 result Capture，
    因此允许 source、parent、action source/result 组成的真实谱系，而不是错误地
    要求 source_capture_id 等于 Entry Capture。
    """

    for row in rows:
        capture_id = _row_text(row, "verified_capture_id")
        if capture_id is None:
            raise ExplorationSchedulerFactsScopeError(
                "App Evidence Index Entry 的 Capture 不存在或无法证明归属"
            )
        _validate_related_capture(
            row,
            prefix="verified_capture",
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
            expected_status="captured",
        )

        capture_action_id = _row_text(row, "verified_capture_action_execution_id")
        entry_action_id = _row_text(row, "action_execution_id")
        if capture_action_id != entry_action_id:
            raise ExplorationSchedulerFactsScopeError(
                "App Evidence Index Entry 与 Capture 的 Action 身份不一致"
            )
        capture_role = _row_text(row, "verified_capture_role")
        if capture_action_id is None:
            if capture_role not in {"bootstrap", "observation"}:
                raise ExplorationSchedulerFactsScopeError(
                    "无 Action 身份的 App Capture 不是合法 Bootstrap/Observation"
                )
        else:
            _validate_app_action_lineage(
                row,
                capture_id=capture_id,
                capture_role=capture_role,
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=session_id,
                finalization_epoch=finalization_epoch,
            )

        work_id = _row_text(row, "exploration_work_id")
        if work_id is None:
            # Bootstrap/Final Observation 可以没有 Work，但不能没有 Capture 自身的
            # 不可变阶段证明；上面的 Capture 校验已经强制 metadata/action 证明。
            continue
        if (
            _row_text(row, "verified_work_id") != work_id
            or _row_text(row, "verified_work_task_id") != task_id
            or _row_text(row, "verified_work_session_id") != session_id
            or _epoch_value(row.get("verified_work_finalization_epoch")) != finalization_epoch
        ):
            raise ExplorationSchedulerFactsScopeError(
                "App Evidence Index Entry 的 Exploration Work 不属于当前 Session/Epoch"
            )
        _validate_related_capture(
            row,
            prefix="verified_work_source",
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
            expected_status="captured",
        )
        work_source_id = _row_text(row, "verified_work_source_capture_id")
        if work_source_id is None:
            raise ExplorationSchedulerFactsScopeError(
                "Exploration Work 缺少可验证的 source Capture"
            )
        _validate_work_source_lineage(
            row,
            capture_id=capture_id,
            capture_role=capture_role,
            work_source_id=work_source_id,
        )


def _validate_app_action_lineage(
    row: Mapping[str, Any],
    *,
    capture_id: str,
    capture_role: str | None,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    finalization_epoch: int,
) -> None:
    """校验动作前后 Capture 与 Action Execution 的不可变关系。"""

    action_id = _row_text(row, "verified_action_execution_id")
    if (
        action_id is None
        or _row_text(row, "verified_action_task_id") != task_id
        or _row_text(row, "verified_action_session_id") != session_id
        or _epoch_value(row.get("verified_action_finalization_epoch")) != finalization_epoch
    ):
        raise ExplorationSchedulerFactsScopeError(
            "App Capture 的 Action Execution 不属于当前 Session/Epoch"
        )
    source_id = _row_text(row, "verified_action_source_capture_id")
    result_id = _row_text(row, "verified_action_result_capture_id")
    parent_id = _row_text(row, "verified_capture_parent_capture_id")
    if capture_role == "before_action":
        if source_id != capture_id:
            raise ExplorationSchedulerFactsScopeError(
                "App before_action Capture 不是 Action 的 source Capture"
            )
    elif capture_role == "after_action":
        if result_id != capture_id or source_id is None or parent_id != source_id:
            raise ExplorationSchedulerFactsScopeError(
                "App after_action Capture 未通过 Action result/parent 关系校验"
            )
    else:
        raise ExplorationSchedulerFactsScopeError("带 Action 身份的 App Capture role 不受支持")
    _validate_optional_related_capture(
        row,
        prefix="verified_action_source",
        expected_capture_id=source_id,
        expected_action_execution_id=action_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        finalization_epoch=finalization_epoch,
    )
    _validate_optional_related_capture(
        row,
        prefix="verified_action_target",
        expected_capture_id=result_id,
        expected_action_execution_id=action_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        finalization_epoch=finalization_epoch,
    )


def _validate_related_capture(
    row: Mapping[str, Any],
    *,
    prefix: str,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    finalization_epoch: int,
    expected_status: str,
) -> None:
    capture_id = _row_text(row, f"{prefix}_id")
    if (
        capture_id is None
        or _row_text(row, f"{prefix}_task_id") != task_id
        or _row_text(row, f"{prefix}_agent_run_id") != agent_run_id
        or _row_text(row, f"{prefix}_surface_kind") not in {"android", "ios"}
        or _row_text(row, f"{prefix}_session_id") != session_id
        or _row_text(row, f"{prefix}_session_record_id") != session_id
        or _row_text(row, f"{prefix}_status") != expected_status
    ):
        raise ExplorationSchedulerFactsScopeError(
            "App Capture 的任务、Run、Surface 或 Session 归属无效"
        )
    session_surface_kind = _row_text(row, f"{prefix}_session_surface_kind")
    surface_kind = _row_text(row, f"{prefix}_surface_kind")
    if session_surface_kind != surface_kind:
        raise ExplorationSchedulerFactsScopeError("App Capture 与 Session 的 Surface 不一致")
    _validate_capture_epoch_proof(
        row,
        prefix=prefix,
        finalization_epoch=finalization_epoch,
    )


def _validate_optional_related_capture(
    row: Mapping[str, Any],
    *,
    prefix: str,
    expected_capture_id: str | None,
    expected_action_execution_id: str,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    finalization_epoch: int,
) -> None:
    capture_id = _row_text(row, f"{prefix}_id")
    if capture_id is None:
        if expected_capture_id is not None:
            raise ExplorationSchedulerFactsScopeError("Action 的 source/result Capture 关联缺失")
        return
    if expected_capture_id is None:
        raise ExplorationSchedulerFactsScopeError("Action 存在未声明的 source/result Capture")
    if expected_capture_id is not None and capture_id != expected_capture_id:
        raise ExplorationSchedulerFactsScopeError(
            "Action 的 source/result Capture 与实际关联不一致"
        )
    if _row_text(row, f"{prefix}_action_execution_id") != expected_action_execution_id:
        raise ExplorationSchedulerFactsScopeError(
            "Action 的 source/result Capture 未绑定同一 Action Execution"
        )
    _validate_related_capture(
        row,
        prefix=prefix,
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        finalization_epoch=finalization_epoch,
        expected_status="captured",
    )


def _validate_capture_epoch_proof(
    row: Mapping[str, Any],
    *,
    prefix: str,
    finalization_epoch: int,
) -> None:
    """要求 Capture 自身或实际动作关联给出不可变阶段证明。

    ``capture_epoch_sql`` 的结果始终会出现在生产查询中：它优先使用 Capture
    metadata，并只在 before/result 的真实 Action 外键关系成立时回退到 Action
    的固定阶段。单元测试还可以只提供 metadata，不能用会话当前阶段代替证明。
    """

    metadata = _json_value(row.get(f"{prefix}_metadata"))
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ExplorationSchedulerFactsScopeError("App Capture metadata 不是对象")
    metadata_epoch: int | None = None
    if isinstance(metadata, Mapping) and "finalization_epoch" in metadata:
        metadata_epoch = _epoch_value(metadata.get("finalization_epoch"))
        if metadata_epoch is None or metadata_epoch != finalization_epoch:
            raise ExplorationSchedulerFactsScopeError(
                "App Capture metadata 的 finalization_epoch 无法证明当前阶段"
            )

    effective_key = f"{prefix}_finalization_epoch"
    if effective_key in row:
        effective_epoch = _epoch_value(row.get(effective_key))
        if effective_epoch != finalization_epoch:
            raise ExplorationSchedulerFactsScopeError(
                "App Capture 没有可验证的不可变 finalization_epoch"
            )
        return
    if metadata_epoch != finalization_epoch:
        raise ExplorationSchedulerFactsScopeError(
            "App Capture 缺少 metadata/action 的不可变 finalization_epoch 证明"
        )


def _validate_work_source_lineage(
    row: Mapping[str, Any],
    *,
    capture_id: str,
    capture_role: str | None,
    work_source_id: str,
) -> None:
    """按实际动作链校验 Work 起点，允许 after 的 source 是 before Capture。"""

    if capture_role == "after_action":
        expected_source_id = _row_text(row, "verified_action_source_capture_id")
        if expected_source_id is None or work_source_id != expected_source_id:
            raise ExplorationSchedulerFactsScopeError(
                "Action after Capture 的 Work source 必须是动作前 Capture"
            )
        return
    if capture_role == "before_action":
        if work_source_id != capture_id:
            raise ExplorationSchedulerFactsScopeError(
                "Action before Capture 的 Work source 必须是自身 Capture"
            )
        return
    parent_id = _row_text(row, "verified_capture_parent_capture_id")
    if capture_role in {"bootstrap", "observation", "supplemental"} and work_source_id in {
        capture_id,
        parent_id,
    }:
        return
    raise ExplorationSchedulerFactsScopeError(
        "Exploration Work source Capture 与 Entry 的 parent/action 谱系不一致"
    )


def _row_text(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _epoch_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _app_index_payload(
    row: Mapping[str, Any],
    entries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "source": "qc_app_evidence_index",
        "index_id": str(row["index_id"]),
        "task_id": str(row["task_id"]),
        "agent_run_id": str(row["agent_run_id"]),
        "build_id": str(row["build_id"]),
        "surface_session_id": str(row["surface_session_id"]),
        "protocol_version": str(row["protocol_version"]),
        "status": str(row["status"]),
        "result_context_sha256": row["result_context_sha256"],
        "outcome": row["outcome"],
        "terminal_reason_code": row["terminal_reason_code"],
        "content_sha256": row["content_sha256"],
        "evidence_gaps": _json_value(row["evidence_gaps"]),
        "sealed_at": _datetime_value(row["sealed_at"]),
        "created_at": _datetime_value(row["created_at"]),
        "updated_at": _datetime_value(row["updated_at"]),
        "entries": [dict(entry) for entry in entries],
    }


def _app_index_entry_payload(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "source": "qc_app_evidence_index_entry",
        "sequence_no": int(row["sequence_no"]),
        "capture_id": str(row["capture_id"]),
        "evidence_manifest_id": str(row["evidence_manifest_id"]),
        "evidence_manifest_version": int(row["evidence_manifest_version"]),
        "semantic_role": str(row["semantic_role"]),
        "surface_phase": row["surface_phase"],
        "exploration_work_id": row["exploration_work_id"],
        "action_execution_id": row["action_execution_id"],
        "candidate_id": row["candidate_id"],
        "page_identity": row["page_identity"],
        "captured_at": _datetime_value(row["captured_at"]),
        "metadata": _json_value(row["metadata"]),
    }


def _rule_request_payload(row: Mapping[str, Any]) -> dict[str, object]:
    request_payload = _json_value(row["request_payload"])
    if not isinstance(request_payload, Mapping):
        request_payload = {}
    return {
        "source": "qc_evidence_request",
        "request_id": str(row["request_id"]),
        "task_id": str(row["task_id"]),
        "rule_id": str(row["rule_id"]),
        "rule_code": str(row["rule_code"]),
        "target_capture_id": str(row["target_capture_id"]),
        "session_id": str(row["surface_session_id"]),
        "status": str(row["status"]),
        "finalization_epoch": int(row["finalization_epoch"]),
        "request_payload": dict(request_payload),
        "created_at": _datetime_value(row["created_at"]),
        "updated_at": _datetime_value(row["updated_at"]),
        "evidence_gap": {
            "source": "qc_evidence_request",
            "request_id": str(row["request_id"]),
            "reason_code": _request_reason_code(request_payload),
            "target_capture_id": str(row["target_capture_id"]),
            "page_types": _request_page_types(request_payload),
            "status": str(row["status"]),
        },
    }


def _h5_rule_request_fact(request: Mapping[str, object]) -> dict[str, object]:
    """投影 H5 调度所需的最小补采事实，不复制规则请求正文。"""

    return {
        key: request[key]
        for key in (
            "source",
            "request_id",
            "task_id",
            "rule_id",
            "rule_code",
            "target_capture_id",
            "session_id",
            "status",
            "finalization_epoch",
            "created_at",
            "updated_at",
            "evidence_gap",
        )
    }


def _request_reason_code(payload: Mapping[str, Any]) -> str:
    for key in ("reason_code", "reason", "evidence_goal"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "evidence_request_pending"


def _request_page_types(payload: Mapping[str, Any]) -> list[str]:
    found: list[str] = []

    def visit(value: object, *, in_target: bool = False, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).strip().casefold()
                if normalized in {
                    "page_type",
                    "target_page_type",
                    "expected_page_type",
                    "page_types",
                    "target_page_types",
                }:
                    visit(item, in_target=True, depth=depth + 1)
                else:
                    visit(item, in_target=in_target, depth=depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item, in_target=in_target, depth=depth + 1)
        elif in_target and isinstance(value, str) and value.strip():
            normalized = value.strip().casefold()
            if normalized not in found:
                found.append(normalized)

    visit(payload)
    return found


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _datetime_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


__all__ = [
    "ExplorationSchedulerFactsScopeError",
    "PostgresExplorationSchedulerFactsReader",
]
