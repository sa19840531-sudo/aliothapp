"""Web 策略只写一次；归属、历史缺口和并发可见性在同一事务检查。"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.domain.exploration.web_run_policy_snapshot import (
    WebRunPolicyError,
    WebRunPolicySnapshot,
    canonical_policy_json,
)
from services.api.app.domain.exploration.web_runtime_contract import (
    CURRENT_WEB_RUNTIME_VERSION,
)
from services.api.app.domain.page.web_page_catalog import CURRENT_WEB_CATALOG_VERSION
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWebRunPolicySnapshotRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None):
        self._database_url = database_url
        self._pool = pool

    async def read(self, *, task_id: str, agent_run_id: str) -> WebRunPolicySnapshot | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            await _require_owner(connection, task_id, agent_run_id)
            result = await read_web_policy_on_connection(connection, task_id, agent_run_id)
            if result is None:
                await _require_no_history(connection, agent_run_id)
            return result

    async def freeze_or_read(self, snapshot: WebRunPolicySnapshot) -> WebRunPolicySnapshot:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            # 锁定真实 Run；竞争者等待提交后用下一条语句读取已提交正文。
            await _require_owner(connection, snapshot.task_id, snapshot.agent_run_id, lock=True)
            existing = await read_web_policy_on_connection(
                connection, snapshot.task_id, snapshot.agent_run_id
            )
            if existing is not None:
                return existing
            # 仅已存在的冻结记录可恢复旧协议；首次插入必须满足当前五类/v3 合同。
            materialize_web_policy(snapshot)
            await _require_no_history(connection, snapshot.agent_run_id)
            await connection.execute(
                """
                INSERT INTO qc_web_run_policy_snapshot (
                    task_id, agent_run_id, payload, payload_sha256
                )
                VALUES ($1::uuid, $2::uuid, $3::jsonb, $4)
                ON CONFLICT (agent_run_id) DO NOTHING
            """,
                snapshot.task_id,
                snapshot.agent_run_id,
                canonical_policy_json(snapshot.payload),
                snapshot.payload_sha256,
            )
            result = await read_web_policy_on_connection(
                connection, snapshot.task_id, snapshot.agent_run_id
            )
            if result is None:
                raise WebRunPolicyError("WEB_POLICY_FREEZE_FAILED: 未取得冻结正文")
            return result


async def _require_owner(connection, task_id: str, agent_run_id: str, *, lock=False):
    row = await connection.fetchrow(
        """
        SELECT run.task_id FROM qc_agent_run run
        JOIN qc_task task ON task.id=run.task_id
        WHERE run.id=$1::uuid AND run.task_id=$2::uuid
          AND run.agent_type='main' AND task.target_platform='web'
    """
        + (" FOR UPDATE OF run" if lock else ""),
        agent_run_id,
        task_id,
    )
    if row is None:
        raise WebRunPolicyError("WEB_POLICY_OWNERSHIP_INVALID: 不属于真实 Web Task/main Run")


async def _require_no_history(connection, agent_run_id: str):
    has_history = await connection.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM qc_capture WHERE agent_run_id=$1::uuid)
            OR EXISTS (SELECT 1 FROM qc_page_capture WHERE agent_run_id=$1::uuid)
            OR EXISTS (SELECT 1 FROM qc_action_execution WHERE session_id='web:' || $1::text)
            OR EXISTS (SELECT 1 FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid)
    """,
        agent_run_id,
    )
    if has_history:
        raise WebRunPolicyError("WEB_POLICY_HISTORY_MISSING: 已有动作或证据，无法恢复当时完整策略")


async def read_web_policy_on_connection(connection, task_id: str, agent_run_id: str):
    """复用当前入库事务连接，避免单连接池中嵌套获取连接。"""
    try:
        row = await connection.fetchrow(
            """
            SELECT task_id, agent_run_id, payload, payload_sha256
            FROM qc_web_run_policy_snapshot WHERE agent_run_id=$1::uuid
        """,
            agent_run_id,
        )
    except asyncpg.UndefinedTableError as exc:
        raise WebRunPolicyError("WEB_POLICY_STORAGE_UNAVAILABLE: Web 策略迁移尚未部署") from exc
    if row is None:
        return None
    if str(row["task_id"]) != task_id:
        raise WebRunPolicyError("WEB_POLICY_OWNERSHIP_INVALID: 冻结行归属不符")
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    result = WebRunPolicySnapshot(
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        payload=payload,
        payload_sha256=row["payload_sha256"],
    )
    # 当前协议在信任边界完整复验；历史协议仍交给只读适配器，不得反向变成可执行运行时。
    if (
        result.payload.get("runtime_version") == CURRENT_WEB_RUNTIME_VERSION
        and result.payload.get("guide_version") == CURRENT_WEB_CATALOG_VERSION
    ):
        materialize_web_policy(result)
    return result
