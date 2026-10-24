"""Web 阶段事务仓储；运行归属锁、租约 fencing 与 row_version CAS。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebStageCursor,
    WebStageStatus,
)
from services.api.app.domain.exploration.web_runtime_contract import CURRENT_WEB_RUNTIME_VERSION
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_stage_checkpoint_row import (
    parse_web_run_stage_checkpoint_row,
    web_run_stage_checkpoint_values,
)

_ACTIVE = """
    run.status = 'running' AND task.status = 'running' AND task.target_platform = 'web'
    AND task.finalization_state <> 'sealed'
    AND COALESCE(run.output_context->>'cancel_requested', 'false') = 'false'
    AND run.input_context->'task_config'->>'web_runtime_version' = $2
"""
_DUE = """
    (checkpoint.stage_status = 'pending'
     OR (checkpoint.stage_status = 'retry_wait' AND checkpoint.next_attempt_at <= $1)
     OR (checkpoint.stage_status = 'running' AND checkpoint.lease_expires_at <= $1)
     OR (checkpoint.stage_status IN ('succeeded', 'gapped')
         AND checkpoint.current_stage NOT IN ('session_close', 'ad_closeout')))
"""


class WebStageCheckpointConflict(RuntimeError):
    """游标版本、领取租约或运行归属已失效。"""


class PostgresWebRunStageCheckpointRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None):
        self._database_url = database_url
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_postgres_pool(self._database_url)

    async def _lock_run(self, connection, run_id: str, epoch: int | None = None):
        # 与取消/终态更新互斥，但兼容证据 INSERT 的外键 KEY SHARE，避免互相死锁。
        row = await connection.fetchrow(
            f"""SELECT task.finalization_epoch FROM qc_agent_run AS run
                JOIN qc_task AS task ON task.id=run.task_id
                WHERE run.id=$1::uuid AND {_ACTIVE} FOR NO KEY UPDATE OF run, task""",
            run_id,
            CURRENT_WEB_RUNTIME_VERSION,
        )
        if row is None or (epoch is not None and row["finalization_epoch"] != epoch):
            raise WebStageCheckpointConflict("Web Run 已取消、终结、属于历史合同或 epoch 失效")

    async def create(self, cursor: WebStageCursor) -> WebStageCursor:
        """同 Run/逻辑页只创建一次；重复初始化只复用相同初始身份。"""
        if (
            cursor.status is not WebStageStatus.PENDING
            or cursor.row_version
            or cursor.attempt_count
        ):
            raise ValueError("只允许创建尚未领取的初始游标")
        identity = sha256(
            json.dumps(
                {
                    "stage": cursor.current_stage.value,
                    "generation": cursor.stage_generation,
                    "payload": dict(cursor.cursor_payload),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        values = web_run_stage_checkpoint_values(cursor)
        values["initial_identity"] = identity
        keys = tuple(values)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._lock_run(
                connection, cursor.run_id, cursor.cursor_payload.get("finalization_epoch")
            )
            # 唯一键以逻辑页为边界；相同广告在不同业务页仍需各自留证。
            await connection.execute(
                f"INSERT INTO qc_web_run_stage_checkpoint ({','.join(keys)}) "
                f"VALUES ({','.join('$' + str(i) for i in range(1, len(keys) + 1))}) "
                "ON CONFLICT (run_id,logical_page_key) DO NOTHING",
                *values.values(),
            )
            row = await self._read(connection, cursor.run_id, cursor.logical_page_key)
            if row["initial_identity"] != identity:
                raise WebStageCheckpointConflict("逻辑页面初始恢复身份不一致")
            return parse_web_run_stage_checkpoint_row(row)

    async def _read(self, connection, run_id, logical_page_key, *, lock=False):
        row = await connection.fetchrow(
            "SELECT * FROM qc_web_run_stage_checkpoint "
            "WHERE run_id=$1::uuid AND logical_page_key=$2" + (" FOR UPDATE" if lock else ""),
            run_id,
            logical_page_key,
        )
        if row is None:
            raise WebStageCheckpointConflict("checkpoint 不存在")
        return row

    async def get(self, *, run_id: str, logical_page_key: str) -> WebStageCursor:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return parse_web_run_stage_checkpoint_row(
                await self._read(connection, run_id, logical_page_key)
            )

    async def list_for_run(self, *, run_id: str) -> tuple[WebStageCursor, ...]:
        """恢复调度同时需要未来重试与已完成子页面；实际领取仍走 claim 门禁。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM qc_web_run_stage_checkpoint WHERE run_id=$1::uuid "
                "ORDER BY created_at,logical_page_key",
                run_id,
            )
        return tuple(parse_web_run_stage_checkpoint_row(row) for row in rows)

    async def _persist(self, connection, before, after, now, *, claiming=False, renewing=False):
        values = web_run_stage_checkpoint_values(
            replace(after, row_version=before.row_version + (0 if renewing else 1), updated_at=now)
        )
        for key in ("run_id", "logical_page_key", "created_at"):
            values.pop(key)
        assignment = ",".join(f"{key}=${i}" for i, key in enumerate(values, start=5))
        if claiming:
            fence = (
                " AND (stage_status <> 'running' OR lease_expires_at <= clock_timestamp())"
                " AND (stage_status <> 'retry_wait' OR next_attempt_at <= clock_timestamp())"
            )
            if after.status is WebStageStatus.RUNNING:
                lease_parameter = tuple(values).index("lease_expires_at") + 5
                fence += f" AND ${lease_parameter}::timestamptz > clock_timestamp()"
        else:
            fence = " AND stage_status='running' AND lease_expires_at > clock_timestamp()"
            if renewing:
                lease_parameter = tuple(values).index("lease_expires_at") + 5
                fence += f" AND ${lease_parameter}::timestamptz > clock_timestamp()"
        row = await connection.fetchrow(
            f"UPDATE qc_web_run_stage_checkpoint SET {assignment} "
            "WHERE run_id=$1::uuid AND logical_page_key=$2 AND row_version=$3 "
            "AND claim_token IS NOT DISTINCT FROM $4::uuid" + fence + " RETURNING *",
            before.run_id,
            before.logical_page_key,
            before.row_version,
            before.claim_token,
            *values.values(),
        )
        if row is None:
            raise WebStageCheckpointConflict("checkpoint CAS 失败")
        return parse_web_run_stage_checkpoint_row(row)

    async def claim(
        self,
        *,
        run_id: str,
        logical_page_key: str,
        expected_row_version: int,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 600,
    ) -> WebStageCursor:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._lock_run(connection, run_id)
            current = parse_web_run_stage_checkpoint_row(
                await self._read(connection, run_id, logical_page_key, lock=True)
            )
            await self._lock_run(
                connection, run_id, current.cursor_payload.get("finalization_epoch")
            )
            if current.row_version != expected_row_version:
                raise WebStageCheckpointConflict("checkpoint 领取版本已失效")
            # 入参 now 只为兼容旧调用；领取授权只使用全部行锁取得后的数据库时钟。
            now = await connection.fetchval("SELECT clock_timestamp()")
            try:
                after = current.claim(worker_id=worker_id, now=now, lease_seconds=lease_seconds)
            except ValueError as exc:
                raise WebStageCheckpointConflict(str(exc)) from exc
            return await self._persist(connection, current, after, now, claiming=True)

    async def _settle(self, cursor, transition, *, renewing=False):
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._lock_run(connection, cursor.run_id)
            current = parse_web_run_stage_checkpoint_row(
                await self._read(connection, cursor.run_id, cursor.logical_page_key, lock=True)
            )
            await self._lock_run(
                connection, cursor.run_id, current.cursor_payload.get("finalization_epoch")
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                current.row_version != cursor.row_version
                or current.claim_token != cursor.claim_token
                or current.claimed_worker_id != cursor.claimed_worker_id
                or current.status is not WebStageStatus.RUNNING
                or current.lease_expires_at is None
                or current.lease_expires_at <= now
            ):
                raise WebStageCheckpointConflict("阶段回写租约或版本已失效")
            return await self._persist(
                connection, current, transition(current, now), now, renewing=renewing
            )

    async def renew_lease(self, cursor: WebStageCursor) -> WebStageCursor:
        """延续同一执行授权，不改变阶段版本；旧 token、过期和取消仍执行完整 CAS 门禁。"""
        return await self._settle(
            cursor,
            lambda current, now: replace(
                current, lease_expires_at=now + timedelta(seconds=600)
            ),
            renewing=True,
        )

    async def mark_succeeded(
        self,
        cursor: WebStageCursor,
        *,
        artifact_ids: tuple[str, ...] = (),
        cursor_payload: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> WebStageCursor:
        def transition(current, _now):
            result = current.mark_succeeded(artifact_ids=artifact_ids)
            return replace(
                result, cursor_payload={**result.cursor_payload, **(cursor_payload or {})}
            )

        return await self._settle(cursor, transition)

    async def schedule_retry(
        self,
        cursor: WebStageCursor,
        *,
        error_code: str,
        transient: bool = True,
        detail: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> WebStageCursor:
        return await self._settle(
            cursor,
            lambda current, database_now: current.schedule_retry(
                error_code=error_code, transient=transient, detail=detail, now=database_now
            ),
        )

    async def record_gap_and_advance(
        self,
        cursor: WebStageCursor,
        *,
        error_code: str,
        gap_code: str | None = None,
        detail: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> WebStageCursor:
        return await self._settle(
            cursor,
            lambda current, _now: current.record_gap_and_advance(
                error_code=error_code, gap_code=gap_code, detail=detail
            ),
        )

    async def list_due(
        self, *, now: datetime | None = None, run_id: str | None = None, limit: int = 100
    ) -> tuple[WebStageCursor, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("领取批次必须为 1..1000")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT checkpoint.* FROM qc_web_run_stage_checkpoint AS checkpoint
                    JOIN qc_agent_run AS run ON run.id=checkpoint.run_id
                    JOIN qc_task AS task ON task.id=run.task_id
                    WHERE {_ACTIVE} AND {_DUE}
                      AND ($3::uuid IS NULL OR checkpoint.run_id=$3::uuid)
                      AND (checkpoint.cursor_payload->>'finalization_epoch' IS NULL
                           OR (checkpoint.cursor_payload->>'finalization_epoch')::int
                               =task.finalization_epoch)
                    ORDER BY COALESCE(checkpoint.next_attempt_at, checkpoint.updated_at),
                             checkpoint.run_id, checkpoint.logical_page_key LIMIT $4""",
                now or datetime.now(UTC),
                CURRENT_WEB_RUNTIME_VERSION,
                run_id,
                limit,
            )
        return tuple(parse_web_run_stage_checkpoint_row(row) for row in rows)
