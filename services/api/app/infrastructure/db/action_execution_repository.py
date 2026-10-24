"""Action Execution 可恢复状态机的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.api.app.application.evidence_exit_gate import (
    is_trusted_initial_surface_action,
    trusted_h5_browser_recovery_identity,
)
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    ACTION_EXECUTION_PROTOCOL_VERSION,
    ActionExecutionSnapshot,
    ActionExecutionStatus,
    ActionIntent,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
    build_action_execution_id,
    build_action_intent_hash,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_PAGE_GRAPH_UNSET = object()


class ActionExecutionPersistenceError(RuntimeError):
    """动作身份、Session 租约、状态或 Capture 归属已变化。"""


# 普通动作：父采集必须是本 Session 已落库的 Capture，用 INNER JOIN 强制。
_PLAN_QUERY = """
    INSERT INTO qc_action_execution AS existing (
        action_execution_id, task_id, session_id, intent_id,
        idempotency_key, intent_hash, intent_protocol_version,
        intent_payload, command_sequence, finalization_epoch,
        requested_source_capture_id, status
    )
    SELECT
        $1, session.task_id, session.session_id, $4, $5, $6, $7,
        $8::jsonb, session.command_sequence,
        session.finalization_epoch, source.id, 'planned'
    FROM qc_surface_session AS session
    INNER JOIN qc_capture AS source
        ON source.id = $11::uuid
       AND source.task_id = session.task_id
       AND (
            source.surface_session_id = session.session_id
            OR (
                -- H5 浏览器崩溃恢复：新 Session 允许引用已 lost 的旧 Session 采集，
                -- 但必须同 Run，且旧 Session 归属符合工作流或恢复器命名。
                $15::text IS NOT NULL
                AND source.surface_session_id = $15
                AND source.agent_run_id = $17::uuid
                AND session.session_owner = $16
                AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS lost_source
                    WHERE lost_source.task_id = session.task_id
                      AND lost_source.session_id = $15
                      AND lost_source.surface_kind = 'web'
                      AND lost_source.status = 'lost'
                      AND (
                        (
                          $15 = 'h5:' || $17::text
                          AND lost_source.session_owner LIKE '%h5-workflow'
                          AND lost_source.session_owner NOT LIKE 'h5-browser-recovery:%'
                        )
                        OR (
                          $15 <> 'h5:' || $17::text
                          AND lost_source.session_owner = $16
                        )
                      )
                )
            )
       )
       AND source.surface_kind = session.surface_kind
       AND source.capture_status = 'captured'
    WHERE session.task_id = $2::uuid
      AND session.session_id = $3
      AND session.surface_kind = $9
      AND session.lease_token = $10::uuid
      AND session.lease_owner = $12
      AND session.lease_expires_at > now()
      AND session.command_sequence = $13
      AND session.finalization_epoch = $14
      AND session.status IN ('active', 'draining')
    ON CONFLICT (action_execution_id) DO UPDATE
    SET action_execution_id = existing.action_execution_id
    WHERE existing.task_id = EXCLUDED.task_id
      AND existing.session_id = EXCLUDED.session_id
      AND existing.intent_id = EXCLUDED.intent_id
      AND existing.idempotency_key = EXCLUDED.idempotency_key
      AND existing.intent_hash = EXCLUDED.intent_hash
      AND existing.requested_source_capture_id
            IS NOT DISTINCT FROM EXCLUDED.requested_source_capture_id
    RETURNING existing.*
"""

# 入口动作：此前没有任何采集可引用，$11 恒为 NULL，因此没有可 JOIN 的父采集。
# 其余归属、租约与幂等条件与普通动作完全一致，不放宽。
_PLAN_ENTRY_QUERY = """
    INSERT INTO qc_action_execution AS existing (
        action_execution_id, task_id, session_id, intent_id,
        idempotency_key, intent_hash, intent_protocol_version,
        intent_payload, command_sequence, finalization_epoch,
        requested_source_capture_id, status
    )
    SELECT
        $1, session.task_id, session.session_id, $4, $5, $6, $7,
        $8::jsonb, session.command_sequence,
        session.finalization_epoch, $11::uuid, 'planned'
    FROM qc_surface_session AS session
    WHERE session.task_id = $2::uuid
      AND session.session_id = $3
      AND session.surface_kind = $9
      AND session.lease_token = $10::uuid
      AND session.lease_owner = $12
      AND session.lease_expires_at > now()
      AND session.command_sequence = $13
      AND session.finalization_epoch = $14
      AND session.status IN ('active', 'draining')
    ON CONFLICT (action_execution_id) DO UPDATE
    SET action_execution_id = existing.action_execution_id
    WHERE existing.task_id = EXCLUDED.task_id
      AND existing.session_id = EXCLUDED.session_id
      AND existing.intent_id = EXCLUDED.intent_id
      AND existing.idempotency_key = EXCLUDED.idempotency_key
      AND existing.intent_hash = EXCLUDED.intent_hash
      AND existing.requested_source_capture_id
            IS NOT DISTINCT FROM EXCLUDED.requested_source_capture_id
    RETURNING existing.*
"""


class PostgresActionExecutionRepository:
    """旧租约和旧 Finalization Epoch 都不能推进动作账本。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        page_graph_repository: Any = _PAGE_GRAPH_UNSET,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._page_graph_repository = (
            page_graph_repository
            if page_graph_repository is not _PAGE_GRAPH_UNSET
            else PostgresPageGraphRepository(database_url=database_url, pool=pool)
        )

    async def plan(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
    ) -> ActionExecutionSnapshot:
        _validate_permit_for_intent(permit, intent)
        action_execution_id = build_action_execution_id(intent)
        intent_hash = build_action_intent_hash(intent)
        # 入口动作发生在任何采集之前，父采集是不指向已落库 Capture 的哨兵：
        # 台账按「无父采集」留痕，与采集侧首跳 parent_capture_id 为空同义。
        # 入口动作没有可引用的旧采集，因此不走崩溃恢复分支，也不传恢复身份参数。
        entry_action = is_trusted_initial_surface_action(intent)
        recovery_source_session_id, recovery_owner, recovery_run_id = (
            (None, None, None)
            if entry_action
            else _h5_recovery_initial_identity(intent)
        )
        args: list[Any] = [
            action_execution_id,
            intent.task_id,
            intent.session_id,
            intent.intent_id,
            intent.idempotency_key,
            intent_hash,
            intent.protocol_version,
            _json_dumps(intent.to_dict()),
            intent.surface_kind.value,
            permit.lease_token,
            None if entry_action else intent.source_capture_id,
            permit.lease_owner,
            permit.command_sequence,
            permit.finalization_epoch,
        ]
        if not entry_action:
            args.extend(
                (recovery_source_session_id, recovery_owner, recovery_run_id)
            )
        pool = await self._get_pool()
        if self._page_graph_repository is None:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    _PLAN_ENTRY_QUERY if entry_action else _PLAN_QUERY,
                    *args,
                )
        else:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    _PLAN_ENTRY_QUERY if entry_action else _PLAN_QUERY,
                    *args,
                )
                if row is not None:
                    await self._page_graph_repository.project_action_on_connection(
                        connection,
                        action_execution_id=str(row["action_execution_id"]),
                    )
        if row is None:
            raise ActionExecutionPersistenceError(
                "Action Execution 归属无效、租约失效或幂等身份漂移"
            )
        persisted = _parse_action(row)
        if intent.surface_kind is SurfaceKind.WEB and (
            persisted.command_sequence != permit.command_sequence
            or persisted.finalization_epoch != permit.finalization_epoch
        ):
            raise ActionExecutionPersistenceError(
                "相同 Web 动作已绑定旧命令序列或 Finalization Epoch，禁止再次执行"
            )
        return persisted

    async def find_intent_for_work(
        self,
        *,
        task_id: str,
        session_id: str,
        exploration_work_id: str,
    ) -> ActionIntent | None:
        """按 Work 身份读取已落库意图，避免恢复时重新生成另一动作。"""

        for field_name, value in (
            ("task_id", task_id),
            ("session_id", session_id),
            ("exploration_work_id", exploration_work_id),
        ):
            _require_text(value, field_name)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT intent_payload
                FROM qc_action_execution
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND intent_payload->>'surface_kind' IN ('android', 'ios')
                  AND intent_payload #>> '{parameters,exploration_work_id}' = $3
                ORDER BY created_at, action_execution_id
                LIMIT 2
                """,
                task_id,
                session_id,
                exploration_work_id,
            )
        if not rows:
            return None
        if len(rows) != 1:
            raise ActionExecutionPersistenceError(
                "同一 App Exploration Work 绑定了多个动作意图"
            )
        try:
            intent = ActionIntent.from_dict(
                _as_mapping(rows[0]["intent_payload"], field_name="intent_payload")
            )
        except (TypeError, ValueError) as exc:
            raise ActionExecutionPersistenceError("恢复动作意图合同无效") from exc
        if (
            intent.task_id != task_id
            or intent.session_id != session_id
            or intent.surface_kind is SurfaceKind.WEB
            or intent.parameters.get("exploration_work_id") != exploration_work_id
        ):
            raise ActionExecutionPersistenceError("恢复动作意图与 Work 归属不一致")
        return intent

    async def recover(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        """恢复旧动作账本；仅未开始的 Before Capture 失败可安全重试一次。"""

        _require_text(action_execution_id, "action_execution_id")
        if (
            isinstance(expected_row_version, bool)
            or not isinstance(expected_row_version, int)
            or expected_row_version < 0
        ):
            raise ValueError("expected_row_version 必须为非负整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT action.*
                FROM qc_action_execution AS action
                INNER JOIN qc_surface_session AS session
                  ON session.task_id = action.task_id
                 AND session.session_id = action.session_id
                 AND session.surface_kind = $4
                 AND session.status IN ('active', 'draining')
                 AND session.lease_token = $5::uuid
                 AND session.lease_owner = $6
                 AND session.lease_expires_at > now()
                 AND session.command_sequence = $7
                 AND session.finalization_epoch = $8
                WHERE action.action_execution_id = $1
                  AND action.task_id = $2::uuid
                  AND action.session_id = $3
                  AND action.row_version = $9
                FOR UPDATE OF action, session
                """,
                action_execution_id,
                permit.task_id,
                permit.session_id,
                permit.surface_kind.value,
                permit.lease_token,
                permit.lease_owner,
                permit.command_sequence,
                permit.finalization_epoch,
                expected_row_version,
            )
            if row is None:
                raise ActionExecutionPersistenceError(
                    "恢复动作时归属、版本或当前 Session 租约已变化"
                )
            persisted = _parse_action(row)
            if persisted.finalization_epoch != permit.finalization_epoch:
                raise ActionExecutionPersistenceError(
                    "动作所属 Finalization Epoch 已过期，禁止恢复"
                )
            if (
                persisted.status is ActionExecutionStatus.FAILED
                and persisted.source_capture_id is None
                and persisted.started_at is None
                and persisted.result_capture_id is None
                and persisted.evidence_finished_at is None
                and persisted.error_code == "before_capture_failed"
                and _before_capture_retry_count(persisted.result_payload) < 1
            ):
                retried = await connection.fetchrow(
                    """
                    UPDATE qc_action_execution AS action
                    SET status = 'planned',
                        command_sequence = $2,
                        result_payload = jsonb_set(
                            COALESCE(action.result_payload, '{}'::jsonb),
                            '{before_capture_retry_count}',
                            to_jsonb(
                                COALESCE(
                                    (action.result_payload->>'before_capture_retry_count')::integer,
                                    0
                                ) + 1
                            ),
                            true
                        ),
                        error_code = NULL,
                        error_message = NULL,
                        action_finished_at = NULL,
                        result_capture_id = NULL,
                        evidence_finished_at = NULL,
                        row_version = action.row_version + 1,
                        updated_at = now()
                    WHERE action.action_execution_id = $1
                      AND action.status = 'failed'
                      AND action.row_version = $4
                      AND action.source_capture_id IS NULL
                      AND action.started_at IS NULL
                      AND action.result_capture_id IS NULL
                      AND action.evidence_finished_at IS NULL
                      AND action.error_code = 'before_capture_failed'
                      AND COALESCE(
                            (action.result_payload->>'before_capture_retry_count')::integer,
                            0
                          ) < 1
                      AND action.finalization_epoch = $3
                      AND EXISTS (
                          SELECT 1 FROM qc_surface_session AS session
                          WHERE session.task_id = action.task_id
                            AND session.session_id = action.session_id
                            AND session.surface_kind = $5
                            AND session.lease_token = $6::uuid
                            AND session.lease_owner = $7
                            AND session.lease_expires_at > now()
                            AND session.command_sequence = $2
                            AND session.finalization_epoch = $3
                      )
                    RETURNING action.*
                    """,
                    action_execution_id,
                    permit.command_sequence,
                    permit.finalization_epoch,
                    expected_row_version,
                    permit.surface_kind.value,
                    permit.lease_token,
                    permit.lease_owner,
                )
                if retried is None:
                    raise ActionExecutionPersistenceError(
                        "动作前 Capture 重试绑定失败"
                    )
                return await self._project_snapshot_on_connection(
                    connection, _parse_action(retried)
                )
            if persisted.status is ActionExecutionStatus.PLANNED:
                rebound = await connection.fetchrow(
                    """
                    UPDATE qc_action_execution AS action
                    SET command_sequence = $2,
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE action.action_execution_id = $1
                      AND action.status = 'planned'
                      AND action.row_version = $4
                      AND action.finalization_epoch = $3
                      AND EXISTS (
                          SELECT 1 FROM qc_surface_session AS session
                          WHERE session.task_id = action.task_id
                            AND session.session_id = action.session_id
                            AND session.surface_kind = $5
                            AND session.lease_token = $6::uuid
                            AND session.lease_owner = $7
                            AND session.lease_expires_at > now()
                            AND session.command_sequence = $2
                            AND session.finalization_epoch = $3
                      )
                    RETURNING action.*
                    """,
                    action_execution_id,
                    permit.command_sequence,
                    permit.finalization_epoch,
                    expected_row_version,
                    permit.surface_kind.value,
                    permit.lease_token,
                    permit.lease_owner,
                )
                if rebound is None:
                    raise ActionExecutionPersistenceError("planned 动作恢复绑定失败")
                return await self._project_snapshot_on_connection(
                    connection, _parse_action(rebound)
                )
            if persisted.status is ActionExecutionStatus.EXECUTING:
                blocked = await connection.fetchrow(
                    """
                    UPDATE qc_action_execution AS action
                    SET status = 'failed',
                        error_code = 'APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH',
                        error_message = '进程中断时动作可能已经送达设备，禁止重复执行',
                        action_finished_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE action.action_execution_id = $1
                      AND action.status = 'executing'
                      AND action.row_version = $2
                      AND EXISTS (
                          SELECT 1 FROM qc_surface_session AS session
                          WHERE session.task_id = action.task_id
                            AND session.session_id = action.session_id
                            AND session.surface_kind = $3
                            AND session.lease_token = $4::uuid
                            AND session.lease_owner = $5
                            AND session.lease_expires_at > now()
                            AND session.command_sequence = $6
                            AND session.finalization_epoch = $7
                      )
                    RETURNING action.*
                    """,
                    action_execution_id,
                    expected_row_version,
                    permit.surface_kind.value,
                    permit.lease_token,
                    permit.lease_owner,
                    permit.command_sequence,
                    permit.finalization_epoch,
                )
                if blocked is None:
                    raise ActionExecutionPersistenceError("executing 动作阻断写回失败")
                return await self._project_snapshot_on_connection(
                    connection, _parse_action(blocked)
                )
            if persisted.status is ActionExecutionStatus.EVIDENCE_PENDING:
                try:
                    pending = InteractionResult.from_dict(persisted.result_payload)
                except (TypeError, ValueError) as exc:
                    raise ActionExecutionPersistenceError(
                        "待证据动作的结果合同无效"
                    ) from exc
                _validate_pending_action_result(persisted, pending)
                after_capture_id = await _load_recovery_after_capture_id(
                    connection,
                    action_execution_id,
                    permit=permit,
                    action=persisted,
                )
                if after_capture_id is not None:
                    interaction = pending.complete_with_capture(after_capture_id)
                    recovered = await connection.fetchrow(
                        """
                        UPDATE qc_action_execution AS action
                        SET status = 'completed',
                            result_capture_id = $2::uuid,
                            result_payload = $3::jsonb,
                            evidence_finished_at = now(),
                            row_version = row_version + 1,
                            updated_at = now()
                        WHERE action.action_execution_id = $1
                          AND action.status = 'evidence_pending'
                          AND action.row_version = $4
                          AND EXISTS (
                              SELECT 1 FROM qc_surface_session AS session
                              WHERE session.task_id = action.task_id
                                AND session.session_id = action.session_id
                                AND session.surface_kind = $5
                                AND session.lease_token = $6::uuid
                                AND session.lease_owner = $7
                                AND session.lease_expires_at > now()
                                AND session.command_sequence = $8
                                AND session.finalization_epoch = $9
                          )
                        RETURNING action.*
                        """,
                        action_execution_id,
                        interaction.result_capture_id,
                        _json_dumps(interaction.to_dict()),
                        expected_row_version,
                        permit.surface_kind.value,
                        permit.lease_token,
                        permit.lease_owner,
                        permit.command_sequence,
                        permit.finalization_epoch,
                    )
                else:
                    interaction = pending.mark_evidence_failed(
                        error_code="APP_AFTER_CAPTURE_MISSING_AFTER_CRASH",
                        error_message="动作已经完成，但进程中断前未提交 After Capture",
                        retryable=False,
                    )
                    recovered = await connection.fetchrow(
                        """
                        UPDATE qc_action_execution AS action
                        SET status = 'evidence_failed',
                            result_payload = $2::jsonb,
                            error_code = $3,
                            error_message = $4,
                            evidence_finished_at = now(),
                            row_version = row_version + 1,
                            updated_at = now()
                        WHERE action.action_execution_id = $1
                          AND action.status = 'evidence_pending'
                          AND action.row_version = $5
                          AND EXISTS (
                              SELECT 1 FROM qc_surface_session AS session
                              WHERE session.task_id = action.task_id
                                AND session.session_id = action.session_id
                                AND session.surface_kind = $6
                                AND session.lease_token = $7::uuid
                                AND session.lease_owner = $8
                                AND session.lease_expires_at > now()
                                AND session.command_sequence = $9
                                AND session.finalization_epoch = $10
                          )
                        RETURNING action.*
                        """,
                        action_execution_id,
                        _json_dumps(interaction.to_dict()),
                        interaction.error_code,
                        interaction.error_message,
                        expected_row_version,
                        permit.surface_kind.value,
                        permit.lease_token,
                        permit.lease_owner,
                        permit.command_sequence,
                        permit.finalization_epoch,
                    )
                if recovered is None:
                    raise ActionExecutionPersistenceError("待证据动作恢复写回失败")
                restored = _parse_action(recovered)
                if restored.status is ActionExecutionStatus.COMPLETED:
                    _validate_recovered_action_result(restored)
                    await _advance_recovered_session_cursor(
                        connection,
                        permit=permit,
                        action_execution_id=restored.action_execution_id,
                        result_capture_id=restored.result_capture_id,
                    )
                return await self._project_snapshot_on_connection(connection, restored)
            if (
                persisted.status
                in {
                    ActionExecutionStatus.BLOCKED,
                    ActionExecutionStatus.FAILED,
                    ActionExecutionStatus.TIMED_OUT,
                }
                and persisted.result_capture_id is None
                and persisted.source_capture_id is not None
            ):
                after_capture_id = await _load_recovery_after_capture_id(
                    connection,
                    action_execution_id,
                    permit=permit,
                    action=persisted,
                )
                if after_capture_id is not None:
                    try:
                        terminal = InteractionResult.from_dict(persisted.result_payload)
                        expected_result_status = {
                            ActionExecutionStatus.BLOCKED: InteractionResultStatus.BLOCKED,
                            ActionExecutionStatus.FAILED: InteractionResultStatus.FAILED,
                            ActionExecutionStatus.TIMED_OUT: InteractionResultStatus.TIMED_OUT,
                        }[persisted.status]
                        if terminal.status is not expected_result_status:
                            raise ValueError("动作终态与结果合同不一致")
                        interaction = terminal.attach_capture_preserving_execution(
                            after_capture_id
                        )
                    except (TypeError, ValueError) as exc:
                        raise ActionExecutionPersistenceError(
                            "终态动作的恢复结果合同无效"
                        ) from exc
                    attached = await connection.fetchrow(
                        """
                        UPDATE qc_action_execution AS action
                        SET result_capture_id = $2::uuid,
                            result_payload = $3::jsonb,
                            evidence_finished_at = now(),
                            row_version = row_version + 1,
                            updated_at = now()
                        WHERE action.action_execution_id = $1
                          AND action.status = $4
                          AND action.row_version = $5
                          AND action.result_capture_id IS NULL
                          AND EXISTS (
                              SELECT 1 FROM qc_surface_session AS session
                              WHERE session.task_id = action.task_id
                                AND session.session_id = action.session_id
                                AND session.surface_kind = $6
                                AND session.lease_token = $7::uuid
                                AND session.lease_owner = $8
                                AND session.lease_expires_at > now()
                                AND session.command_sequence = $9
                                AND session.finalization_epoch = $10
                          )
                        RETURNING action.*
                        """,
                        action_execution_id,
                        after_capture_id,
                        _json_dumps(interaction.to_dict()),
                        persisted.status.value,
                        expected_row_version,
                        permit.surface_kind.value,
                        permit.lease_token,
                        permit.lease_owner,
                        permit.command_sequence,
                        permit.finalization_epoch,
                    )
                    if attached is None:
                        raise ActionExecutionPersistenceError(
                            "终态动作 After Capture 恢复写回失败"
                        )
                    persisted = _parse_action(attached)
            if (
                persisted.status
                in {
                    ActionExecutionStatus.COMPLETED,
                    ActionExecutionStatus.BLOCKED,
                    ActionExecutionStatus.FAILED,
                    ActionExecutionStatus.TIMED_OUT,
                }
                and persisted.result_capture_id is not None
                and persisted.evidence_finished_at is not None
            ):
                _validate_recovered_action_result(persisted)
                await _advance_recovered_session_cursor(
                    connection,
                    permit=permit,
                    action_execution_id=persisted.action_execution_id,
                    result_capture_id=persisted.result_capture_id,
                )
            return await self._project_snapshot_on_connection(connection, persisted)

    async def mark_executing(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        source_capture_id: str,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        row = await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'executing',
                    source_capture_id = capture.id,
                    started_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.status = 'planned'
                  AND action.row_version = $2
                  AND capture.id = $3::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.surface_kind = $6
                  AND capture.capture_role = 'before_action'
                  AND capture.capture_status = 'captured'
                  -- 入口动作两侧同为 NULL：before Capture 是根证据
                  AND capture.parent_capture_id
                        IS NOT DISTINCT FROM action.requested_source_capture_id
                  AND capture.session_command_sequence = action.command_sequence
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = $6
                      AND session.lease_token = $4::uuid
                      AND session.lease_owner = $5
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                action_execution_id,
                expected_row_version,
                source_capture_id,
                permit.lease_token,
                permit.lease_owner,
                permit.surface_kind.value,
            ),
            error="动作前 Capture、状态、版本或 Session 租约已变化",
        )
        return row

    async def resume_planned_with_capture(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        source_capture_id: str,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        """复用崩溃前已提交的 Before Capture，不重新截图也不重复设备动作。"""

        _require_text(action_execution_id, "action_execution_id")
        _require_text(source_capture_id, "source_capture_id")
        row = await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'executing',
                    command_sequence = $4,
                    source_capture_id = capture.id,
                    started_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.status = 'planned'
                  AND action.row_version = $2
                  AND capture.id = $3::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.surface_kind = $6
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.capture_role = 'before_action'
                  AND capture.capture_status = 'captured'
                  -- 入口动作两侧同为 NULL：before Capture 是根证据
                  AND capture.parent_capture_id
                        IS NOT DISTINCT FROM action.requested_source_capture_id
                  AND action.finalization_epoch = $5
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = $6
                      AND session.status IN ('active', 'draining')
                      AND session.lease_token = $7::uuid
                      AND session.lease_owner = $8
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = $4
                      AND session.finalization_epoch = $5
                  )
                RETURNING action.*
            """,
            args=(
                action_execution_id,
                expected_row_version,
                source_capture_id,
                permit.command_sequence,
                permit.finalization_epoch,
                permit.surface_kind.value,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="动作前 Capture、planned 状态、版本或当前 Session 租约已变化",
        )
        return row

    async def record_interaction(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_status: ActionExecutionStatus,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        if expected_status is not ActionExecutionStatus.EXECUTING:
            raise ValueError("Interaction 结果只能从 executing 推进")
        status_map = {
            InteractionResultStatus.EVIDENCE_PENDING: ActionExecutionStatus.EVIDENCE_PENDING,
            InteractionResultStatus.BLOCKED: ActionExecutionStatus.BLOCKED,
            InteractionResultStatus.FAILED: ActionExecutionStatus.FAILED,
            InteractionResultStatus.TIMED_OUT: ActionExecutionStatus.TIMED_OUT,
        }
        target_status = status_map.get(interaction.status)
        if target_status is None:
            raise ValueError("Interaction Executor 返回了不允许的阶段状态")
        row = await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = $3,
                    result_payload = $4::jsonb,
                    error_code = $5,
                    error_message = $6,
                    action_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                WHERE action.action_execution_id = $1
                  AND action.status = $2
                  AND action.row_version = $7
                  AND action.task_id = $8::uuid
                  AND action.session_id = $9
                  AND action.intent_id = $10
                  AND action.source_capture_id = $11::uuid
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.lease_token = $12::uuid
                      AND session.lease_owner = $13
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                interaction.action_execution_id,
                expected_status.value,
                target_status.value,
                _json_dumps(interaction.to_dict()),
                interaction.error_code,
                interaction.error_message,
                expected_row_version,
                interaction.task_id,
                interaction.session_id,
                interaction.intent_id,
                interaction.source_capture_id,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="动作结果身份、状态、版本或 Session 租约已变化",
        )
        return row

    async def complete_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        if (
            interaction.status is not InteractionResultStatus.SUCCEEDED
            or interaction.result_capture_id is None
        ):
            raise ValueError("完成证据必须提供 succeeded InteractionResult")
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'completed',
                    result_capture_id = capture.id,
                    result_payload = $4::jsonb,
                    evidence_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.status = 'evidence_pending'
                  AND action.row_version = $2
                  AND action.task_id = $5::uuid
                  AND action.session_id = $6
                  AND action.intent_id = $7
                  AND action.source_capture_id = $8::uuid
                  AND capture.id = $3::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.surface_kind = $11
                  AND capture.capture_role = 'after_action'
                  AND capture.capture_status = 'captured'
                  AND capture.parent_capture_id = action.source_capture_id
                  AND capture.session_command_sequence = action.command_sequence
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = $11
                      AND session.lease_token = $9::uuid
                      AND session.lease_owner = $10
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                interaction.action_execution_id,
                expected_row_version,
                interaction.result_capture_id,
                _json_dumps(interaction.to_dict()),
                interaction.task_id,
                interaction.session_id,
                interaction.intent_id,
                interaction.source_capture_id,
                permit.lease_token,
                permit.lease_owner,
                permit.surface_kind.value,
            ),
            error="结果 Capture、动作状态、版本或 Session 租约已变化",
        )

    async def attach_terminal_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        """为动作失败终态附加 After Capture，同时保持原执行状态。"""
        status_map = {
            InteractionResultStatus.FAILED: ActionExecutionStatus.FAILED,
            InteractionResultStatus.BLOCKED: ActionExecutionStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT: ActionExecutionStatus.TIMED_OUT,
        }
        expected_status = status_map.get(interaction.status)
        if expected_status is None or interaction.result_capture_id is None:
            raise ValueError("终态证据必须携带失败、阻断或超时结果及 Capture")
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    result_capture_id = capture.id,
                    result_payload = $4::jsonb,
                    evidence_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.row_version = $2
                  AND action.status = $3
                  AND action.task_id = $5::uuid
                  AND action.session_id = $6
                  AND action.intent_id = $7
                  AND action.source_capture_id = $8::uuid
                  AND action.result_capture_id IS NULL
                  AND capture.id = $9::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.surface_kind = $12
                  AND capture.capture_role = 'after_action'
                  AND capture.capture_status = 'captured'
                  AND capture.parent_capture_id = action.source_capture_id
                  AND capture.session_command_sequence = action.command_sequence
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = $12
                      AND session.lease_token = $10::uuid
                      AND session.lease_owner = $11
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                interaction.action_execution_id,
                expected_row_version,
                expected_status.value,
                _json_dumps(interaction.to_dict()),
                interaction.task_id,
                interaction.session_id,
                interaction.intent_id,
                interaction.source_capture_id,
                interaction.result_capture_id,
                permit.lease_token,
                permit.lease_owner,
                permit.surface_kind.value,
            ),
            error="终态动作证据、状态、版本或 Session 租约已变化",
        )

    async def fail_before_action(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        expected_row_version: int,
        error_code: str,
        error_message: str,
    ) -> ActionExecutionSnapshot:
        _require_text(error_code, "error_code")
        _require_text(error_message, "error_message")
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'failed',
                    error_code = $3,
                    error_message = $4,
                    action_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                WHERE action.action_execution_id = $1
                  AND action.status = 'planned'
                  AND action.row_version = $2
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.lease_token = $5::uuid
                      AND session.lease_owner = $6
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                action_execution_id,
                expected_row_version,
                error_code,
                error_message,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="动作前失败写回时状态、版本或 Session 租约已变化",
        )

    async def fail_nonterminal_for_task(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
        h5_only: bool = False,
    ) -> int:
        """在 Worker 终态对账时按任务收束已失去执行者的动作账本。"""

        _require_text(task_id, "task_id")
        _require_text(error_code, "error_code")
        _require_text(error_message, "error_message")
        if not isinstance(h5_only, bool):
            raise ValueError("h5_only 必须为布尔值")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            count = await connection.fetchval(
                """
                WITH failed AS (
                    UPDATE qc_action_execution AS action
                    SET
                        status = CASE
                          WHEN action.status = 'evidence_pending' THEN 'evidence_failed'
                          ELSE 'failed'
                        END,
                        error_code = $2,
                        error_message = $3,
                        action_finished_at = COALESCE(action.action_finished_at, now()),
                        evidence_finished_at = CASE
                          WHEN action.status = 'evidence_pending' THEN now()
                          ELSE action.evidence_finished_at
                        END,
                        row_version = action.row_version + 1,
                        updated_at = now()
                    WHERE action.task_id = $1::uuid
                      AND action.status IN ('planned', 'executing', 'evidence_pending')
                      AND (
                        $4::boolean = FALSE
                        OR EXISTS (
                            SELECT 1
                            FROM qc_surface_session AS session
                            WHERE session.task_id = action.task_id
                              AND session.session_id = action.session_id
                              AND session.surface_kind = 'web'
                        )
                      )
                    RETURNING 1
                )
                SELECT count(*) FROM failed
                """,
                task_id,
                error_code,
                error_message,
                h5_only,
            )
        # 更新动作与重建投影可能分两次提交；首次重建失败后，重试即使已无
        # 非终态动作，也必须按真实账本刷新图，不能把旧 executing 永久留下。
        if self._page_graph_repository is not None:
            await self._page_graph_repository.rebuild_task(task_id=task_id)
        return int(count or 0)

    async def fail_nonterminal_for_finalization(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        error_code: str,
        error_message: str,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> int:
        """仅在权威 Run 的 Session Draining 阶段收束遗留动作账本。"""

        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        _require_text(error_code, "error_code")
        _require_text(error_message, "error_message")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            if recovery_lease_token is not None:
                await assert_finalization_recovery_fence(
                    connection,
                    task_id=task_id,
                    recovery_lease_token=recovery_lease_token,
                    recovery_generation=recovery_generation,
                    recovery_lease_expires_at=recovery_lease_expires_at,
                )
            count = await connection.fetchval(
                """
                WITH failed AS (
                    UPDATE qc_action_execution AS action
                    SET
                        status = CASE
                          WHEN action.status = 'evidence_pending' THEN 'evidence_failed'
                          ELSE 'failed'
                        END,
                        error_code = COALESCE(action.error_code, $4),
                        error_message = COALESCE(action.error_message, $5),
                        action_finished_at = COALESCE(action.action_finished_at, now()),
                        evidence_finished_at = CASE
                          WHEN action.status = 'evidence_pending' THEN now()
                          ELSE action.evidence_finished_at
                        END,
                        row_version = action.row_version + 1,
                        updated_at = now()
                    WHERE action.task_id = $1::uuid
                      AND action.status IN ('planned', 'executing', 'evidence_pending')
                      AND EXISTS (
                        SELECT 1
                        FROM qc_task AS task
                        WHERE task.id = action.task_id
                          AND task.finalization_state = 'session_draining'
                          AND task.finalization_agent_run_id = $2::uuid
                          AND task.finalization_epoch = $3
                      )
                    RETURNING 1
                )
                SELECT count(*) FROM failed
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
                error_code,
                error_message,
            )
        if self._page_graph_repository is not None and int(count or 0) > 0:
            await self._page_graph_repository.rebuild_run(
                task_id=task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            )
        return int(count or 0)

    async def fail_h5_browser_loss_unknown(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        action_execution_id: str,
        source_capture_id: str,
        failure_stage: str,
    ) -> ActionExecutionSnapshot:
        """旧 H5 Session 已 LOST 后确认超时原终态，或幂等终结未知 Action。"""
        if failure_stage not in {"action", "after_capture"}:
            raise ValueError("failure_stage 只允许 action 或 after_capture")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH transitioned AS (
                    UPDATE qc_action_execution AS action
                    SET status = CASE
                          WHEN $6 = 'after_capture' THEN 'evidence_failed'
                          ELSE 'failed'
                        END,
                        error_code = CASE
                          WHEN $6 = 'after_capture'
                            THEN 'h5_browser_loss_after_capture_unknown'
                          ELSE 'h5_browser_loss_action_unknown'
                        END,
                        error_message = 'H5 浏览器丢失，动作结果未知',
                        result_payload = $7::jsonb,
                        action_finished_at = now(),
                        evidence_finished_at = CASE
                          WHEN $6 = 'after_capture' THEN now()
                          ELSE action.evidence_finished_at
                        END,
                        row_version = action.row_version + 1,
                        updated_at = now()
                    WHERE action.task_id = $1::uuid
                      AND action.session_id = $3
                      AND action.action_execution_id = $4
                      AND action.source_capture_id = $5::uuid
                      AND (
                        ($6 = 'action' AND action.status = 'executing')
                        OR (
                          $6 = 'after_capture'
                          AND action.status = 'evidence_pending'
                        )
                      )
                      AND EXISTS (
                        SELECT 1
                        FROM qc_surface_session AS source_session
                        INNER JOIN qc_task AS task
                          ON task.id = source_session.task_id
                         AND lower(task.target_platform) = 'h5'
                         AND task.finalization_epoch = source_session.finalization_epoch
                         AND task.finalization_epoch = action.finalization_epoch
                        INNER JOIN qc_capture AS source
                          ON source.id = $5::uuid
                         AND source.task_id = source_session.task_id
                         AND source.surface_session_id = source_session.session_id
                         AND source.agent_run_id = $2::uuid
                        WHERE source_session.task_id = action.task_id
                          AND source_session.session_id = action.session_id
                          AND source_session.surface_kind = 'web'
                          AND source_session.status = 'lost'
                          AND (
                            (
                              $3 = 'h5:' || $2::text
                              AND source_session.session_owner LIKE '%h5-workflow'
                              AND source_session.session_owner
                                    NOT LIKE 'h5-browser-recovery:%'
                            )
                            OR (
                              $3 <> 'h5:' || $2::text
                              AND source_session.session_owner
                                    = 'h5-browser-recovery:' || $2::text
                            )
                          )
                      )
                    RETURNING action.*
                )
                SELECT * FROM transitioned
                UNION ALL
                SELECT action.* FROM qc_action_execution AS action
                WHERE NOT EXISTS (SELECT 1 FROM transitioned)
                  AND action.task_id = $1::uuid
                  AND action.session_id = $3
                  AND action.action_execution_id = $4
                  AND action.source_capture_id = $5::uuid
                  AND (
                    (
                      $6 = 'action'
                      AND action.status = 'timed_out'
                      AND action.error_code = 'ACTION_TIMED_OUT'
                      AND action.result_capture_id IS NULL
                    )
                    OR
                    (
                      action.status = 'failed'
                      AND action.error_code = 'h5_browser_loss_action_unknown'
                      AND action.result_payload @> $7::jsonb
                    )
                    OR (
                      $6 = 'after_capture'
                      AND action.status = 'evidence_failed'
                      AND action.error_code IN (
                        'after_capture_failed',
                        'h5_browser_loss_after_capture_unknown'
                      )
                      AND (
                        action.error_code = 'after_capture_failed'
                        OR action.result_payload @> $7::jsonb
                      )
                    )
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM qc_surface_session AS source_session
                    INNER JOIN qc_task AS task
                      ON task.id = source_session.task_id
                     AND lower(task.target_platform) = 'h5'
                     AND task.finalization_epoch = source_session.finalization_epoch
                     AND task.finalization_epoch = action.finalization_epoch
                    INNER JOIN qc_capture AS source
                      ON source.id = $5::uuid
                     AND source.task_id = source_session.task_id
                     AND source.surface_session_id = source_session.session_id
                     AND source.agent_run_id = $2::uuid
                    WHERE source_session.task_id = action.task_id
                      AND source_session.session_id = action.session_id
                      AND source_session.surface_kind = 'web'
                      AND source_session.status = 'lost'
                      AND (
                        (
                          $3 = 'h5:' || $2::text
                          AND source_session.session_owner LIKE '%h5-workflow'
                          AND source_session.session_owner
                                NOT LIKE 'h5-browser-recovery:%'
                        )
                        OR (
                          $3 <> 'h5:' || $2::text
                          AND source_session.session_owner
                                = 'h5-browser-recovery:' || $2::text
                        )
                      )
                  )
                LIMIT 1
                """,
                task_id,
                agent_run_id,
                session_id,
                action_execution_id,
                source_capture_id,
                failure_stage,
                _json_dumps(
                    {
                        "task_id": task_id,
                        "agent_run_id": agent_run_id,
                        "session_id": session_id,
                        "action_execution_id": action_execution_id,
                        "source_capture_id": source_capture_id,
                        "failure_stage": failure_stage,
                        "inventory_status": "insufficient",
                        "original_action_replayed": False,
                    }
                ),
            )
        if row is None:
            raise ActionExecutionPersistenceError(
                "H5 browser loss Action ledger 身份或终态不允许恢复"
            )
        if self._page_graph_repository is not None:
            await self._page_graph_repository.project_action(
                action_execution_id=action_execution_id,
            )
        return _parse_action(row)

    async def fail_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        if interaction.status is not InteractionResultStatus.EVIDENCE_FAILED:
            raise ValueError("证据失败写回必须提供 evidence_failed InteractionResult")
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'evidence_failed',
                    result_payload = $3::jsonb,
                    error_code = $4,
                    error_message = $5,
                    evidence_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                WHERE action.action_execution_id = $1
                  AND action.status = 'evidence_pending'
                  AND action.row_version = $2
                  AND action.task_id = $6::uuid
                  AND action.session_id = $7
                  AND action.intent_id = $8
                  AND action.source_capture_id = $9::uuid
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.lease_token = $10::uuid
                      AND session.lease_owner = $11
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                interaction.action_execution_id,
                expected_row_version,
                _json_dumps(interaction.to_dict()),
                interaction.error_code,
                interaction.error_message,
                interaction.task_id,
                interaction.session_id,
                interaction.intent_id,
                interaction.source_capture_id,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="证据失败写回时动作状态、版本或 Session 租约已变化",
        )

    async def get(self, *, action_execution_id: str) -> ActionExecutionSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_action_execution WHERE action_execution_id = $1",
                action_execution_id,
            )
        return _parse_action(row) if row is not None else None

    async def has_nonterminal(self, *, task_id: str, session_id: str | None = None) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM qc_action_execution
                        WHERE task_id = $1::uuid
                          AND ($2::text IS NULL OR session_id = $2)
                          AND status IN ('planned', 'executing', 'evidence_pending')
                    )
                    """,
                    task_id,
                    session_id,
                )
            )

    async def has_pending_capture_commit(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM qc_action_execution
                        WHERE task_id = $1::uuid
                          AND ($2::text IS NULL OR session_id = $2)
                          AND status = 'evidence_pending'
                    )
                    """,
                    task_id,
                    session_id,
                )
            )

    async def _transition(
        self,
        *,
        query: str,
        args: tuple[Any, ...],
        error: str,
    ) -> ActionExecutionSnapshot:
        pool = await self._get_pool()
        if self._page_graph_repository is None:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(query, *args)
        else:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(query, *args)
                if row is not None:
                    await self._page_graph_repository.project_action_on_connection(
                        connection,
                        action_execution_id=str(row["action_execution_id"]),
                    )
        if row is None:
            raise ActionExecutionPersistenceError(error)
        return _parse_action(row)

    async def _project_snapshot_on_connection(
        self,
        connection: Any,
        snapshot: ActionExecutionSnapshot,
    ) -> ActionExecutionSnapshot:
        if self._page_graph_repository is not None:
            await self._page_graph_repository.project_action_on_connection(
                connection,
                action_execution_id=snapshot.action_execution_id,
            )
        return snapshot

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_action(row: Mapping[str, Any]) -> ActionExecutionSnapshot:
    return ActionExecutionSnapshot(
        action_execution_id=str(row["action_execution_id"]),
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        intent_id=str(row["intent_id"]),
        intent_hash=str(row["intent_hash"]),
        command_sequence=int(row["command_sequence"]),
        finalization_epoch=int(row["finalization_epoch"]),
        requested_source_capture_id=(
            str(row["requested_source_capture_id"])
            if row["requested_source_capture_id"] is not None
            else None
        ),
        source_capture_id=(
            str(row["source_capture_id"]) if row["source_capture_id"] is not None else None
        ),
        result_capture_id=(
            str(row["result_capture_id"]) if row["result_capture_id"] is not None else None
        ),
        status=ActionExecutionStatus(str(row["status"])),
        result_payload=_as_mapping(row["result_payload"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        row_version=int(row["row_version"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        action_finished_at=row["action_finished_at"],
        evidence_finished_at=row["evidence_finished_at"],
        protocol_version=ACTION_EXECUTION_PROTOCOL_VERSION,
    )


def _before_capture_retry_count(payload: Mapping[str, Any]) -> int:
    """读取动作前截图重试次数；畸形历史值按已耗尽处理。"""

    value = payload.get("before_capture_retry_count")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0 if value is None else 1


async def _load_recovery_after_capture_id(
    connection: Any,
    action_execution_id: str,
    *,
    permit: SessionCommandPermit,
    action: ActionExecutionSnapshot,
) -> str | None:
    """只认同一旧命令下完整落库的 App After Capture。"""

    if action.source_capture_id is None:
        raise ActionExecutionPersistenceError("动作账本缺少 Before Capture")
    value = await connection.fetchval(
        """
        SELECT capture.id::text
        FROM qc_capture AS capture
        INNER JOIN qc_app_capture AS app_capture
          ON app_capture.capture_id = capture.id
        INNER JOIN qc_evidence_artifact AS screenshot
          ON screenshot.id = app_capture.screenshot_artifact_id
         AND screenshot.task_id = capture.task_id
         AND screenshot.page_capture_id = capture.id
         AND screenshot.artifact_type = 'screenshot'
        WHERE capture.action_execution_id = $1
          AND capture.task_id = $2::uuid
          AND capture.surface_session_id = $3
          AND capture.surface_kind = $5
          AND capture.capture_role = 'after_action'
          AND capture.capture_status = 'captured'
          AND capture.parent_capture_id = $6::uuid
          AND capture.session_command_sequence = $4
        """,
        action_execution_id,
        permit.task_id,
        permit.session_id,
        action.command_sequence,
        permit.surface_kind.value,
        action.source_capture_id,
    )
    return str(value) if value is not None else None


def _validate_recovered_action_result(
    action: ActionExecutionSnapshot,
) -> InteractionResult:
    """恢复游标前先验证账本终态、结果和 Capture 绑定完全一致。"""

    try:
        interaction = InteractionResult.from_dict(action.result_payload)
    except (TypeError, ValueError) as exc:
        raise ActionExecutionPersistenceError("恢复动作的结果合同无效") from exc
    expected_status = {
        ActionExecutionStatus.COMPLETED: InteractionResultStatus.SUCCEEDED,
        ActionExecutionStatus.BLOCKED: InteractionResultStatus.BLOCKED,
        ActionExecutionStatus.FAILED: InteractionResultStatus.FAILED,
        ActionExecutionStatus.TIMED_OUT: InteractionResultStatus.TIMED_OUT,
    }.get(action.status)
    if expected_status is None or (
        interaction.task_id != action.task_id
        or interaction.session_id != action.session_id
        or interaction.intent_id != action.intent_id
        or interaction.action_execution_id != action.action_execution_id
        or interaction.source_capture_id != action.source_capture_id
        or interaction.result_capture_id != action.result_capture_id
        or interaction.status is not expected_status
    ):
        raise ActionExecutionPersistenceError(
            "恢复动作的账本终态、结果或 Capture 归属不一致"
        )
    return interaction


def _validate_pending_action_result(
    action: ActionExecutionSnapshot,
    interaction: InteractionResult,
) -> None:
    """恢复待证据动作前校验旧账本载荷，禁止污染事实推进状态。"""

    if (
        action.status is not ActionExecutionStatus.EVIDENCE_PENDING
        or action.result_capture_id is not None
        or interaction.task_id != action.task_id
        or interaction.session_id != action.session_id
        or interaction.intent_id != action.intent_id
        or interaction.action_execution_id != action.action_execution_id
        or interaction.source_capture_id != action.source_capture_id
        or interaction.result_capture_id is not None
        or interaction.status is not InteractionResultStatus.EVIDENCE_PENDING
    ):
        raise ActionExecutionPersistenceError(
            "待证据动作的账本状态、结果或 Capture 归属不一致"
        )


async def _advance_recovered_session_cursor(
    connection: Any,
    *,
    permit: SessionCommandPermit,
    action_execution_id: str,
    result_capture_id: str | None,
) -> None:
    """在当前租约事务中把 Session 游标推进到已验证的历史 After Capture。"""

    _require_text(action_execution_id, "action_execution_id")
    _require_text(result_capture_id, "result_capture_id")
    advanced = await connection.fetchval(
        """
        UPDATE qc_surface_session AS session
        SET current_capture_id = capture.id,
            row_version = session.row_version
                + CASE WHEN session.current_capture_id IS DISTINCT FROM capture.id
                       THEN 1 ELSE 0 END,
            updated_at = CASE
                WHEN session.current_capture_id IS DISTINCT FROM capture.id
                THEN now()
                ELSE session.updated_at
            END
        FROM qc_action_execution AS action
        INNER JOIN qc_capture AS capture
          ON capture.id = action.result_capture_id
         AND capture.task_id = action.task_id
         AND capture.surface_session_id = action.session_id
         AND capture.action_execution_id = action.action_execution_id
         AND capture.capture_role = 'after_action'
         AND capture.capture_status = 'captured'
         AND capture.parent_capture_id = action.source_capture_id
         AND capture.session_command_sequence = action.command_sequence
        INNER JOIN qc_app_capture AS app_capture
          ON app_capture.capture_id = capture.id
        INNER JOIN qc_evidence_artifact AS screenshot
          ON screenshot.id = app_capture.screenshot_artifact_id
         AND screenshot.task_id = capture.task_id
         AND screenshot.page_capture_id = capture.id
         AND screenshot.artifact_type = 'screenshot'
        WHERE session.task_id = $1::uuid
          AND session.session_id = $2
          AND session.surface_kind = $3
          AND session.status IN ('active', 'draining')
          AND session.lease_owner = $4
          AND session.lease_token = $5::uuid
          AND session.lease_expires_at > now()
          AND session.command_sequence = $6
          AND session.finalization_epoch = $7
          AND action.action_execution_id = $8
          AND action.task_id = session.task_id
          AND action.session_id = session.session_id
          AND action.status IN ('completed', 'blocked', 'failed', 'timed_out')
          AND action.result_capture_id = $9::uuid
          AND action.evidence_finished_at IS NOT NULL
          AND capture.surface_kind = session.surface_kind
        RETURNING session.session_id
        """,
        permit.task_id,
        permit.session_id,
        permit.surface_kind.value,
        permit.lease_owner,
        permit.lease_token,
        permit.command_sequence,
        permit.finalization_epoch,
        action_execution_id,
        result_capture_id,
    )
    if advanced is None:
        raise ActionExecutionPersistenceError(
            "恢复动作的 After Capture 无法推进当前 Session 游标"
        )


def _validate_permit_for_intent(
    permit: SessionCommandPermit,
    intent: ActionIntent,
) -> None:
    if not isinstance(permit, SessionCommandPermit):
        raise TypeError("permit 必须为 SessionCommandPermit")
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    if (
        permit.task_id != intent.task_id
        or permit.session_id != intent.session_id
        or permit.surface_kind is not intent.surface_kind
    ):
        raise ValueError("Session 命令许可与 ActionIntent 不一致")


def _as_mapping(value: Any, *, field_name: str = "result_payload") -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ActionExecutionPersistenceError(f"{field_name} 必须为对象")
    return value


def _h5_recovery_initial_identity(
    intent: ActionIntent,
) -> tuple[str | None, str | None, str | None]:
    if intent.parameters.get("h5_browser_recovery") is not True:
        return None, None, None
    identity = trusted_h5_browser_recovery_identity(intent)
    if identity is None:
        raise ActionExecutionPersistenceError("H5 recovery-initial Action 身份不合法")
    agent_run_id, _failed_work_id, source_session_id, _stage, _attempt = identity
    return (
        source_session_id,
        f"h5-browser-recovery:{agent_run_id}",
        agent_run_id,
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
