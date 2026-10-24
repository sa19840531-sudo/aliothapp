"""页面状态图投影仓储。

Capture 和 Action Execution 仍是执行事实的唯一来源。本仓储只在相同事务
中写入可重建的图投影，并提供任务详情页和恢复路线的只读查询。投影缺失
或迁移未执行时抛出明确异常，调用方不能把它伪装成空图。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from services.api.app.application.page_graph_contracts import PageGraphUnavailableError
from services.api.app.domain.exploration.page_graph import (
    PAGE_GRAPH_PROTOCOL_VERSION,
    WEB_URL_AFTER_PROTOCOL_VERSION,
    PageStateSnapshot,
    build_app_visual_after_proof,
    build_page_state_snapshot,
    build_transition_decision,
    canonical_url_identity,
)
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.page_graph_progress_repository import (
    read_page_graph_progress,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_ENTRY_RUN_MARKER_PATTERN = (
    r"^(?:h5-entry|agent-entry):[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_ENTRY_RUN_MARKER_PREFIX_PATTERN = r"^(?:h5-entry|agent-entry):"


class PageGraphProjectionError(RuntimeError):
    """页面图投影与权威事实不一致。"""


@dataclass(frozen=True, slots=True)
class PageGraphRebuildResult:
    """一次 Run 重建结果。"""

    task_id: str
    agent_run_id: str
    finalization_epoch: int | None
    state_count: int
    transition_count: int
    status: str
    unprojected_capture_count: int = 0
    unprojected_action_count: int = 0


class PostgresPageGraphRepository:
    """页面状态、转场和实际探索树的 PostgreSQL 投影。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def project_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        agent_run_id: str | None,
        surface_kind: str,
        session_id: str | None,
        finalization_epoch: int | None,
        command_sequence: int | None,
        capture_role: str,
        metadata: Mapping[str, Any] | None = None,
        page_type: str | None = None,
        page_state: str | None = None,
        captured_at: Any | None = None,
    ) -> None:
        """为单个已提交 Capture 写入状态投影。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self.project_capture_on_connection(
                connection,
                task_id=task_id,
                capture_id=capture_id,
                agent_run_id=agent_run_id,
                surface_kind=surface_kind,
                session_id=session_id,
                finalization_epoch=finalization_epoch,
                command_sequence=command_sequence,
                capture_role=capture_role,
                metadata=metadata,
                page_type=page_type,
                page_state=page_state,
                captured_at=captured_at,
            )

    async def project_capture_on_connection(
        self,
        connection: Any,
        *,
        task_id: str,
        capture_id: str,
        agent_run_id: str | None,
        surface_kind: str,
        session_id: str | None,
        finalization_epoch: int | None,
        command_sequence: int | None,
        capture_role: str,
        metadata: Mapping[str, Any] | None = None,
        page_type: str | None = None,
        page_state: str | None = None,
        captured_at: Any | None = None,
    ) -> None:
        from services.api.app.infrastructure.db.web_page_graph_projection import (
            read_web_page_graph_facts,
        )

        web_facts = await read_web_page_graph_facts(
            connection, task_id=task_id, capture_id=capture_id, agent_run_id=agent_run_id,
            session_id=session_id, surface_kind=surface_kind,
        )
        snapshot = build_page_state_snapshot(
            capture_id=capture_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            surface_kind=surface_kind,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
            command_sequence=command_sequence,
            capture_role=capture_role,
            metadata=metadata,
            page_type=page_type,
            page_state=page_state,
            captured_at=captured_at,
            web_facts=web_facts,
        )
        await _insert_state_on_connection(connection, snapshot)

    async def project_capture_from_database_on_connection(
        self,
        connection: Any,
        *,
        capture_id: str,
    ) -> None:
        """从已提交的公共 Capture 读取完整 Session/Epoch 后投影。"""

        row = await connection.fetchrow(
            f"""
            SELECT
                capture.id::text AS capture_id,
                capture.task_id::text AS task_id,
                capture.agent_run_id::text AS agent_run_id,
                capture.surface_kind,
                capture.surface_session_id,
                capture.capture_role,
                capture.session_command_sequence,
                capture.metadata,
                capture.captured_at,
                page.url AS page_url,
                page.page_type,
                page.page_state,
                app_capture.package_name AS app_package_name,
                app_capture.activity AS app_activity,
                app_capture.image_signature AS app_image_signature,
                app_capture.screenshot_artifact_id::text AS app_screenshot_artifact_id,
                session.session_id AS session_record_id,
                session.finalization_epoch,
                {capture_epoch_sql()} AS effective_finalization_epoch
            FROM qc_capture AS capture
            LEFT JOIN qc_page_capture AS page ON page.id = capture.id
            LEFT JOIN qc_app_capture AS app_capture ON app_capture.capture_id = capture.id
            LEFT JOIN qc_surface_session AS session
              ON session.task_id = capture.task_id
             AND session.session_id = capture.surface_session_id
            WHERE capture.id = $1::uuid
              AND capture.capture_status = 'captured'
            """,
            capture_id,
        )
        if row is None:
            raise PageGraphProjectionError("Capture 不存在，无法写入页面状态")
        finalization_epoch = _capture_finalization_epoch(row)
        metadata = _mapping(row.get("metadata"))
        if row.get("page_url") is not None:
            metadata.setdefault("url", str(row["page_url"]))
        if row.get("app_screenshot_artifact_id") is not None:
            metadata["package_name"] = (
                str(row["app_package_name"]) if row.get("app_package_name") is not None else None
            )
            metadata["activity"] = (
                str(row["app_activity"]) if row.get("app_activity") is not None else None
            )
            metadata["image_signature"] = str(row["app_image_signature"])
            metadata["screenshot_artifact_id"] = str(row["app_screenshot_artifact_id"])
        await self.project_capture_on_connection(
            connection,
            task_id=str(row["task_id"]),
            capture_id=str(row["capture_id"]),
            agent_run_id=row.get("agent_run_id"),
            surface_kind=str(row["surface_kind"]),
            session_id=row.get("surface_session_id"),
            finalization_epoch=finalization_epoch,
            command_sequence=_int_or_none(row.get("session_command_sequence")),
            capture_role=str(row["capture_role"]),
            metadata=metadata,
            page_type=row.get("page_type"),
            page_state=row.get("page_state"),
            captured_at=row.get("captured_at"),
        )

    async def update_detection_on_connection(
        self,
        connection: Any,
        *,
        task_id: str,
        agent_run_id: str,
        capture_id: str,
        page_type: str,
        page_state: str,
        detection_status: str,
        confidence: float,
        logical_surface: Mapping[str, Any] | None = None,
    ) -> None:
        """同步 H5 识别结果，但不把识别类型当成页面身份。"""

        row = await connection.fetchrow(
            f"""
            SELECT
                capture.id::text AS capture_id,
                capture.task_id::text AS task_id,
                capture.agent_run_id::text AS agent_run_id,
                capture.surface_kind,
                capture.surface_session_id,
                capture.capture_role,
                capture.session_command_sequence,
                capture.metadata,
                capture.captured_at,
                page.url AS page_url,
                page.page_type,
                page.page_state,
                app_capture.package_name AS app_package_name,
                app_capture.activity AS app_activity,
                app_capture.image_signature AS app_image_signature,
                app_capture.screenshot_artifact_id::text AS app_screenshot_artifact_id,
                session.session_id AS session_record_id,
                session.finalization_epoch,
                {capture_epoch_sql()} AS effective_finalization_epoch
            FROM qc_capture AS capture
            LEFT JOIN qc_page_capture AS page ON page.id = capture.id
            LEFT JOIN qc_app_capture AS app_capture ON app_capture.capture_id = capture.id
            LEFT JOIN qc_surface_session AS session
              ON session.task_id = capture.task_id
             AND session.session_id = capture.surface_session_id
            WHERE capture.id = $1::uuid
              AND capture.task_id = $2::uuid
              AND capture.agent_run_id = $3::uuid
              AND capture.capture_status = 'captured'
            """,
            capture_id,
            task_id,
            agent_run_id,
        )
        if row is None:
            raise PageGraphProjectionError("识别结果绑定的 Capture 不存在")
        finalization_epoch = _capture_finalization_epoch(row)
        metadata = _mapping(row.get("metadata"))
        if row.get("page_url") is not None:
            metadata.setdefault("url", str(row["page_url"]))
        if row.get("app_screenshot_artifact_id") is not None:
            metadata["package_name"] = (
                str(row["app_package_name"]) if row.get("app_package_name") is not None else None
            )
            metadata["activity"] = (
                str(row["app_activity"]) if row.get("app_activity") is not None else None
            )
            metadata["image_signature"] = str(row["app_image_signature"])
            metadata["screenshot_artifact_id"] = str(row["app_screenshot_artifact_id"])
        metadata["detection_status"] = detection_status
        metadata["detection_confidence"] = confidence
        metadata["page_detection"] = {
            **_mapping(metadata.get("page_detection")),
            "detection_status": detection_status,
            "confidence": confidence,
        }
        if logical_surface is not None:
            metadata["logical_surface"] = _mapping(logical_surface)
        snapshot = build_page_state_snapshot(
            capture_id=str(row["capture_id"]),
            task_id=str(row["task_id"]),
            agent_run_id=str(row["agent_run_id"]),
            surface_kind=str(row["surface_kind"]),
            session_id=row.get("surface_session_id"),
            finalization_epoch=finalization_epoch,
            command_sequence=_int_or_none(row.get("session_command_sequence")),
            capture_role=str(row["capture_role"]),
            metadata=metadata,
            page_type=page_type or row.get("page_type"),
            page_state=page_state or row.get("page_state"),
            captured_at=row.get("captured_at"),
        )
        await _insert_state_on_connection(connection, snapshot)

        # 页型识别可能晚于动作留证；识别提交后重新核对实际关联的边，
        # 不能等到手工重建才让普通 H5 的可信到达可见。
        await self._refresh_capture_actions(connection, capture_id=capture_id)

    async def _refresh_capture_actions(self, connection: Any, *, capture_id: str) -> None:
        rows = await connection.fetch(
            """
            SELECT action.action_execution_id
            FROM qc_action_execution AS action
            JOIN qc_capture AS capture ON capture.task_id = action.task_id
            WHERE capture.id = $1::uuid
              AND (action.source_capture_id = capture.id OR action.result_capture_id = capture.id)
            ORDER BY action.action_execution_id
            """,
            capture_id,
        )
        for row in rows:
            await self.project_action_on_connection(
                connection, action_execution_id=str(row["action_execution_id"])
            )

    async def project_visual_observation_on_connection(
        self,
        connection: Any,
        *,
        capture_id: str,
        visual_page_type: str | None,
        page_state: str | None,
        overlay_signature: str | None,
        surface_facts: Mapping[str, Any] | None,
    ) -> None:
        """把 App 不可变视觉事实合并到对应状态版本。"""

        row = await connection.fetchrow(
            f"""
            SELECT
                capture.id::text AS capture_id,
                capture.task_id::text AS task_id,
                capture.agent_run_id::text AS agent_run_id,
                capture.surface_kind,
                capture.surface_session_id,
                capture.parent_capture_id::text AS parent_capture_id,
                capture.action_execution_id,
                capture.capture_role,
                capture.session_command_sequence,
                capture.metadata,
                session.session_id AS session_record_id,
                session.finalization_epoch,
                {capture_epoch_sql()} AS effective_finalization_epoch,
                capture.captured_at,
                app_capture.package_name AS app_package_name,
                app_capture.activity AS app_activity,
                app_capture.image_signature AS app_image_signature,
                app_capture.screenshot_artifact_id::text AS app_screenshot_artifact_id
            FROM qc_capture AS capture
            LEFT JOIN qc_app_capture AS app_capture
              ON app_capture.capture_id = capture.id
            LEFT JOIN qc_surface_session AS session
              ON session.task_id = capture.task_id
             AND session.session_id = capture.surface_session_id
            WHERE capture.id = $1::uuid
              AND capture.capture_status = 'captured'
            """,
            capture_id,
        )
        if row is None:
            raise PageGraphProjectionError("视觉事实绑定的 Capture 不存在")
        finalization_epoch = _capture_finalization_epoch(row)
        metadata = _mapping(row.get("metadata"))
        if row.get("app_screenshot_artifact_id") is not None:
            metadata["package_name"] = (
                str(row["app_package_name"]) if row.get("app_package_name") is not None else None
            )
            metadata["activity"] = (
                str(row["app_activity"]) if row.get("app_activity") is not None else None
            )
            metadata["image_signature"] = str(row["app_image_signature"])
            metadata["screenshot_artifact_id"] = str(row["app_screenshot_artifact_id"])
        facts = _mapping(surface_facts)
        metadata.update(
            {
                "visual_page_type": visual_page_type,
                "page_state": page_state,
                "overlay_signature": overlay_signature,
                "surface_facts": facts,
                "package_name": metadata.get("package_name"),
                "activity": metadata.get("activity"),
            }
        )
        metadata["app_visual_after"] = build_app_visual_after_proof(
            capture_id=str(row["capture_id"]),
            source_capture_id=_text(row.get("parent_capture_id")),
            capture_role=str(row["capture_role"]),
            page_type=visual_page_type,
            page_state=page_state,
            metadata={
                **metadata,
                "surface_kind": str(row["surface_kind"]),
            },
        )
        snapshot = build_page_state_snapshot(
            capture_id=str(row["capture_id"]),
            task_id=str(row["task_id"]),
            agent_run_id=row.get("agent_run_id"),
            surface_kind=str(row["surface_kind"]),
            session_id=row.get("surface_session_id"),
            finalization_epoch=finalization_epoch,
            command_sequence=row.get("session_command_sequence"),
            capture_role=str(row["capture_role"]),
            metadata=metadata,
            page_type=visual_page_type,
            page_state=page_state,
            captured_at=row.get("captured_at"),
        )
        await _insert_state_on_connection(connection, snapshot)
        action_execution_id = _text(row.get("action_execution_id"))
        if action_execution_id is not None:
            # 视觉事实可能在 Action Projection 之后提交；同事务重算该边，
            # 让 App 的真实 After 合同即时反映到达可信度。
            await self.project_action_on_connection(
                connection,
                action_execution_id=action_execution_id,
            )

    async def project_action(self, *, action_execution_id: str) -> None:
        """为已提交 Action Execution 写入或更新实际转场边。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self.project_action_on_connection(
                connection,
                action_execution_id=action_execution_id,
            )

    async def project_action_on_connection(
        self,
        connection: Any,
        *,
        action_execution_id: str,
    ) -> None:
        row = await connection.fetchrow(
            f"""
            SELECT
                action.action_execution_id,
                action.task_id::text AS task_id,
                task.target_platform,
                action.session_id,
                action.command_sequence,
                action.finalization_epoch,
                action.status,
                action.intent_payload,
                action.result_payload,
                action.error_code,
                action.error_message,
                action.requested_source_capture_id::text AS requested_source_capture_id,
                action.source_capture_id::text AS source_capture_id,
                action.result_capture_id::text AS target_capture_id,
                action_session.finalization_epoch AS action_session_finalization_epoch,
                action.started_at,
                action.action_finished_at,
                action.evidence_finished_at,
                source.agent_run_id::text AS source_agent_run_id,
                source.surface_kind AS source_surface_kind,
                source.surface_session_id AS source_surface_session_id,
                source_session.session_id AS source_session_record_id,
                source_session.finalization_epoch AS source_session_finalization_epoch,
                source.capture_status AS source_capture_status,
                source.capture_role AS source_capture_role,
                source.parent_capture_id::text AS source_parent_capture_id,
                source.metadata AS source_metadata,
                {capture_epoch_sql("source")} AS source_capture_epoch,
                source_page.url AS source_url,
                source_page.page_type AS source_page_type,
                source_page.page_state AS source_page_state,
                source.captured_at AS source_captured_at,
                requested_page.url AS requested_source_url,
                requested_state.identity_status AS requested_identity_status,
                requested_state.identity_source AS requested_identity_source,
                requested_state.state_payload AS requested_state_payload,
                source_app.package_name AS source_app_package_name,
                source_app.activity AS source_app_activity,
                source_app.image_signature AS source_app_image_signature,
                source_app.screenshot_artifact_id::text AS source_app_screenshot_artifact_id,
                target.agent_run_id::text AS target_agent_run_id,
                target.surface_kind AS target_surface_kind,
                target.surface_session_id AS target_surface_session_id,
                target_session.session_id AS target_session_record_id,
                target_session.finalization_epoch AS target_session_finalization_epoch,
                target.capture_status AS target_capture_status,
                target.capture_role AS target_capture_role,
                target.metadata AS target_metadata,
                {capture_epoch_sql("target")} AS target_capture_epoch,
                target_page.url AS target_url,
                target_page.page_type AS target_page_type,
                target_page.page_state AS target_page_state,
                target.captured_at AS target_captured_at,
                target_app.package_name AS target_app_package_name,
                target_app.activity AS target_app_activity,
                target_app.image_signature AS target_app_image_signature,
                target_app.screenshot_artifact_id::text AS target_app_screenshot_artifact_id
            FROM qc_action_execution AS action
            JOIN qc_task AS task ON task.id = action.task_id
            JOIN qc_surface_session AS action_session
              ON action_session.task_id = action.task_id
             AND action_session.session_id = action.session_id
            LEFT JOIN qc_capture AS source
              ON source.id = action.source_capture_id
             AND source.task_id = action.task_id
            LEFT JOIN qc_surface_session AS source_session
              ON source_session.task_id = source.task_id
             AND source_session.session_id = source.surface_session_id
            LEFT JOIN qc_page_capture AS source_page
              ON source_page.id = source.id
             AND source_page.task_id = action.task_id
            LEFT JOIN qc_page_capture AS requested_page
              ON requested_page.id = action.requested_source_capture_id
             AND requested_page.task_id = action.task_id
            LEFT JOIN qc_page_graph_state AS requested_state
              ON requested_state.state_id = action.requested_source_capture_id
             AND requested_state.task_id = action.task_id
             AND requested_state.agent_run_id = source.agent_run_id
             AND requested_state.surface_session_id
                 IS NOT DISTINCT FROM source.surface_session_id
             AND requested_state.finalization_epoch
                 IS NOT DISTINCT FROM action.finalization_epoch
            LEFT JOIN qc_app_capture AS source_app
              ON source_app.capture_id = source.id
            LEFT JOIN qc_capture AS target
              ON target.id = action.result_capture_id
             AND target.task_id = action.task_id
            LEFT JOIN qc_surface_session AS target_session
              ON target_session.task_id = target.task_id
             AND target_session.session_id = target.surface_session_id
            LEFT JOIN qc_page_capture AS target_page
              ON target_page.id = target.id
             AND target_page.task_id = action.task_id
            LEFT JOIN qc_app_capture AS target_app
              ON target_app.capture_id = target.id
            WHERE action.action_execution_id = $1
            """,
            action_execution_id,
        )
        if row is None:
            raise PageGraphProjectionError("Action Execution 不存在，无法写入页面转场")
        scope_issues = _action_epoch_scope_issues(row)

        intent_payload = _mapping(row.get("intent_payload"))
        result_payload = _mapping(row.get("result_payload"))
        if scope_issues:
            result_payload = {
                **result_payload,
                "page_graph_scope_issues": list(scope_issues),
            }
        url_markers = _verified_url_markers(row, result_payload)
        source_metadata_override = {
            **url_markers.get("source", {}),
            **_trusted_h5_source_identity_override(row, intent_payload),
        }
        source = await self._ensure_page_capture_state(
            connection,
            row,
            prefix="source",
            metadata_override=source_metadata_override or None,
            scope_valid="source_capture_scope_invalid" not in scope_issues,
        )
        target = await self._ensure_page_capture_state(
            connection,
            row,
            prefix="target",
            metadata_override=url_markers.get("target"),
            scope_valid="target_capture_scope_invalid" not in scope_issues,
        )
        # 动作归属由权威 Capture 的原始 Run 决定，不能因某一端 Capture
        # 不能投影而改用另一端状态快照的 Run。入口哨兵只在两端都没有
        # 真实 Capture 引用时兜底。
        action_agent_run_id = _action_owner_run_id(row, intent_payload)
        decision = build_transition_decision(
            action_status=str(row.get("status") or "unknown"),
            source=source,
            target=target,
            result_payload=result_payload,
            error_code=row.get("error_code"),
            error_message=row.get("error_message"),
        )
        risk_level = _text(
            intent_payload.get("risk_level")
            or intent_payload.get("risk")
            or _mapping(intent_payload.get("parameters")).get("risk_level")
        )
        if risk_level not in {"low", "medium", "high"}:
            risk_level = "unknown"
        replay_eligible = bool(decision.replay_eligible and risk_level == "low")
        action_kind = (
            _text(
                intent_payload.get("action_type")
                or intent_payload.get("operation")
                or intent_payload.get("kind")
                or _mapping(intent_payload.get("parameters")).get("action_type")
            )
            or "unknown"
        )
        semantic_target = _text(
            intent_payload.get("semantic_target")
            or intent_payload.get("target")
            or _mapping(intent_payload.get("parameters")).get("semantic_target")
        )
        parent_transition_id, route_path, depth = await _load_route_parent(
            connection,
            task_id=str(row["task_id"]),
            agent_run_id=action_agent_run_id,
            session_id=_text(row.get("session_id")),
            finalization_epoch=_int_or_none(row.get("finalization_epoch")),
            source_state_id=decision.source_capture_id,
            action_execution_id=str(row["action_execution_id"]),
            target_state_id=decision.target_capture_id,
        )
        await connection.execute(
            """
            INSERT INTO qc_page_graph_transition (
                transition_id, task_id, agent_run_id, surface_kind,
                surface_session_id, finalization_epoch, command_sequence,
                action_execution_id, source_capture_id, target_capture_id,
                source_state_id, target_state_id, action_kind, semantic_target,
                risk_level, status, terminal, proof,
                counts_as_business_arrival, replay_eligible, reason_codes,
                outcome_payload, parent_transition_id, route_path,
                navigation_depth, started_at, finished_at
            )
            VALUES (
                $1, $2::uuid, $3::uuid, $4, $5, $6, $7,
                $1, $8::uuid, $9::uuid, $8::uuid, $9::uuid,
                $10, $11, $12, $13, $14, $15,
                $16, $17, $18::jsonb, $19::jsonb, $20, $21::jsonb,
                $22, $23, $24
            )
            ON CONFLICT (transition_id) DO UPDATE SET
                agent_run_id = EXCLUDED.agent_run_id,
                surface_kind = EXCLUDED.surface_kind,
                surface_session_id = EXCLUDED.surface_session_id,
                finalization_epoch = EXCLUDED.finalization_epoch,
                command_sequence = EXCLUDED.command_sequence,
                source_capture_id = EXCLUDED.source_capture_id,
                target_capture_id = EXCLUDED.target_capture_id,
                source_state_id = EXCLUDED.source_state_id,
                target_state_id = EXCLUDED.target_state_id,
                action_kind = EXCLUDED.action_kind,
                semantic_target = EXCLUDED.semantic_target,
                risk_level = EXCLUDED.risk_level,
                status = EXCLUDED.status,
                terminal = EXCLUDED.terminal,
                proof = EXCLUDED.proof,
                counts_as_business_arrival = EXCLUDED.counts_as_business_arrival,
                replay_eligible = EXCLUDED.replay_eligible,
                reason_codes = EXCLUDED.reason_codes,
                outcome_payload = EXCLUDED.outcome_payload,
                parent_transition_id = EXCLUDED.parent_transition_id,
                route_path = EXCLUDED.route_path,
                navigation_depth = EXCLUDED.navigation_depth,
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at,
                updated_at = now()
            """,
            str(row["action_execution_id"]),
            str(row["task_id"]),
            action_agent_run_id,
            _text(
                intent_payload.get("surface_kind")
                or row.get("source_surface_kind")
                or row.get("target_surface_kind")
            )
            or "web",
            _text(row.get("session_id")),
            _int_or_none(row.get("finalization_epoch")),
            _int_or_none(row.get("command_sequence")),
            decision.source_capture_id,
            decision.target_capture_id,
            action_kind,
            semantic_target,
            risk_level,
            decision.status,
            decision.terminal,
            decision.proof,
            decision.counts_as_business_arrival,
            replay_eligible,
            _json_dumps(list(decision.reason_codes)),
            _json_dumps(result_payload),
            parent_transition_id,
            _json_dumps(route_path),
            depth,
            row.get("started_at"),
            row.get("evidence_finished_at") or row.get("action_finished_at"),
        )

    async def rebuild_run(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int | None = None,
    ) -> PageGraphRebuildResult:
        """按 Capture/Action 权威事实重建一个 Run 的图投影。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            capture_rows = await connection.fetch(
                f"""
                SELECT
                    capture.id::text AS capture_id,
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.surface_kind,
                    capture.surface_session_id,
                    capture.parent_capture_id::text AS parent_capture_id,
                    capture.capture_role,
                    capture.session_command_sequence,
                    capture.metadata,
                    capture.captured_at,
                    page.url AS page_url,
                    page.page_type,
                    page.page_state,
                    app_capture.package_name AS app_package_name,
                    app_capture.activity AS app_activity,
                    app_capture.image_signature AS app_image_signature,
                    app_capture.screenshot_artifact_id::text AS app_screenshot_artifact_id,
                    visual.visual_page_type AS visual_page_type,
                    visual.page_state AS visual_page_state,
                    visual.overlay_signature AS visual_overlay_signature,
                    visual.surface_facts AS visual_surface_facts,
                    session.session_id AS session_record_id,
                    session.finalization_epoch,
                    {capture_epoch_sql()} AS effective_finalization_epoch
                FROM qc_capture AS capture
                LEFT JOIN qc_page_capture AS page ON page.id = capture.id
                LEFT JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = capture.id
                LEFT JOIN qc_app_visual_observation AS visual
                  ON visual.capture_id = capture.id
                LEFT JOIN qc_surface_session AS session
                  ON session.task_id = capture.task_id
                 AND session.session_id = capture.surface_session_id
                WHERE capture.task_id = $1::uuid
                  AND capture.agent_run_id = $2::uuid
                  AND capture.capture_status = 'captured'
                  AND (capture.surface_session_id IS NULL OR session.session_id IS NOT NULL)
                  AND (
                      {capture_epoch_sql()}
                  ) IS NOT DISTINCT FROM $3::integer
                ORDER BY capture.session_command_sequence NULLS FIRST,
                    capture.captured_at, capture.id
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
            for row in capture_rows:
                metadata = _mapping(row.get("metadata"))
                if row.get("page_url") is not None:
                    metadata.setdefault("url", str(row["page_url"]))
                if row.get("app_screenshot_artifact_id") is not None:
                    metadata["package_name"] = (
                        str(row["app_package_name"])
                        if row.get("app_package_name") is not None
                        else None
                    )
                    metadata["activity"] = (
                        str(row["app_activity"]) if row.get("app_activity") is not None else None
                    )
                    metadata["image_signature"] = str(row["app_image_signature"])
                    metadata["screenshot_artifact_id"] = str(row["app_screenshot_artifact_id"])
                if row.get("visual_surface_facts") is not None:
                    metadata["surface_facts"] = _mapping(row["visual_surface_facts"])
                    metadata["app_visual_after"] = build_app_visual_after_proof(
                        capture_id=str(row["capture_id"]),
                        source_capture_id=_text(row.get("parent_capture_id")),
                        capture_role=str(row["capture_role"]),
                        page_type=row.get("visual_page_type"),
                        page_state=row.get("visual_page_state"),
                        metadata={
                            **metadata,
                            "surface_kind": str(row["surface_kind"]),
                        },
                    )
                await self.project_capture_on_connection(
                    connection,
                    task_id=str(row["task_id"]),
                    capture_id=str(row["capture_id"]),
                    agent_run_id=row.get("agent_run_id"),
                    surface_kind=str(row["surface_kind"]),
                    session_id=row.get("surface_session_id"),
                    finalization_epoch=_int_or_none(row.get("effective_finalization_epoch")),
                    command_sequence=_int_or_none(row.get("session_command_sequence")),
                    capture_role=str(row["capture_role"]),
                    metadata=metadata,
                    page_type=row.get("visual_page_type") or row.get("page_type"),
                    page_state=row.get("visual_page_state") or row.get("page_state"),
                    captured_at=row.get("captured_at"),
                )
            action_rows = await connection.fetch(
                """
                SELECT action.action_execution_id
                FROM qc_action_execution AS action
                JOIN qc_surface_session AS session
                  ON session.task_id = action.task_id
                 AND session.session_id = action.session_id
                LEFT JOIN qc_capture AS source
                  ON source.id = action.source_capture_id
                 AND source.task_id = action.task_id
                LEFT JOIN qc_capture AS target
                  ON target.id = action.result_capture_id
                 AND target.task_id = action.task_id
                WHERE action.task_id = $1::uuid
                  AND (
                      -- Capture 归属按 source 优先；跨 Run pair 仍保留在
                      -- source Run 的诊断图，由动作投影记录作用域问题。
                      source.agent_run_id = $2::uuid
                      OR (
                          source.agent_run_id IS NULL
                          AND target.agent_run_id = $2::uuid
                      )
                      OR (
                          -- 入口动作在任何 Capture 之前可能只有入口哨兵；
                          -- 只有两端都没有真实 Capture 引用时才允许 marker 兜底。
                          source.agent_run_id IS NULL
                          AND target.agent_run_id IS NULL
                          AND action.source_capture_id IS NULL
                          AND action.result_capture_id IS NULL
                          AND action.intent_payload->>'source_capture_id' IN (
                              'h5-entry:' || $2::text,
                              'agent-entry:' || $2::text
                          )
                      )
                  )
                  AND (
                      action.finalization_epoch IS NOT DISTINCT FROM $3::integer
                  )
                ORDER BY action.command_sequence, action.action_execution_id
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
            for row in action_rows:
                await self.project_action_on_connection(
                    connection,
                    action_execution_id=str(row["action_execution_id"]),
                )
            counts = await _projection_counts(
                connection,
                task_id=task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            )
        unprojected_capture_count = max(counts[0] - counts[2], 0)
        unprojected_action_count = max(counts[1] - counts[3], 0)
        status = (
            "unavailable"
            if finalization_epoch is None
            else (
                "available"
                if not (unprojected_capture_count or unprojected_action_count)
                else "legacy_unprojected"
            )
        )
        return PageGraphRebuildResult(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            state_count=counts[2],
            transition_count=counts[3],
            status=status,
            unprojected_capture_count=unprojected_capture_count,
            unprojected_action_count=unprojected_action_count,
        )

    async def rebuild_task(self, *, task_id: str) -> tuple[PageGraphRebuildResult, ...]:
        """收口或补偿时按每个 Run/Epoch 重建，禁止把历史事实搬到当前阶段。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH capture_scopes AS (
                    SELECT
                        capture.agent_run_id AS run_id,
                        {capture_epoch_sql()} AS effective_epoch
                    FROM qc_capture AS capture
                    LEFT JOIN qc_surface_session AS session
                      ON session.task_id = capture.task_id
                     AND session.session_id = capture.surface_session_id
                    WHERE capture.task_id = $1::uuid
                      AND capture.agent_run_id IS NOT NULL
                      AND capture.capture_status = 'captured'
                      AND (capture.surface_session_id IS NULL OR session.session_id IS NOT NULL)
                ), action_scopes AS (
                    SELECT
                        CASE
                            WHEN source.agent_run_id IS NOT NULL THEN source.agent_run_id
                            WHEN target.agent_run_id IS NOT NULL THEN target.agent_run_id
                            WHEN (
                                action.source_capture_id IS NULL
                                AND action.result_capture_id IS NULL
                                AND source.agent_run_id IS NULL
                                AND target.agent_run_id IS NULL
                                AND action.intent_payload->>'source_capture_id'
                                    ~ '{_ENTRY_RUN_MARKER_PATTERN}'
                            ) THEN (
                                CASE
                                    WHEN action.intent_payload->>'source_capture_id'
                                        ~ '{_ENTRY_RUN_MARKER_PATTERN}'
                                    THEN regexp_replace(
                                        action.intent_payload->>'source_capture_id',
                                        '{_ENTRY_RUN_MARKER_PREFIX_PATTERN}',
                                        ''
                                    )
                                    ELSE NULL
                                END
                            )::uuid
                            ELSE NULL
                        END AS run_id,
                        action.finalization_epoch AS effective_epoch
                    FROM qc_action_execution AS action
                    JOIN qc_surface_session AS session
                      ON session.task_id = action.task_id
                     AND session.session_id = action.session_id
                    LEFT JOIN qc_capture AS source
                      ON source.id = action.source_capture_id
                     AND source.task_id = action.task_id
                    LEFT JOIN qc_capture AS target
                      ON target.id = action.result_capture_id
                     AND target.task_id = action.task_id
                    WHERE action.task_id = $1::uuid
                      AND (
                          source.agent_run_id IS NOT NULL
                          OR (
                              source.agent_run_id IS NULL
                              AND target.agent_run_id IS NOT NULL
                          )
                          OR (
                              source.agent_run_id IS NULL
                              AND target.agent_run_id IS NULL
                              AND action.source_capture_id IS NULL
                              AND action.result_capture_id IS NULL
                              AND action.intent_payload->>'source_capture_id'
                                  ~ '{_ENTRY_RUN_MARKER_PATTERN}'
                          )
                      )
                )
                SELECT DISTINCT run_id::text AS agent_run_id, effective_epoch AS finalization_epoch
                FROM (
                    SELECT run_id, effective_epoch FROM capture_scopes
                    UNION ALL
                    SELECT run_id, effective_epoch FROM action_scopes
                ) AS scopes
                WHERE run_id IS NOT NULL
                ORDER BY agent_run_id, finalization_epoch NULLS LAST
                """,
                task_id,
            )
        results: list[PageGraphRebuildResult] = []
        for row in rows:
            results.append(
                await self.rebuild_run(
                    task_id=task_id,
                    agent_run_id=str(row["agent_run_id"]),
                    finalization_epoch=_int_or_none(row.get("finalization_epoch")),
                )
            )
        return tuple(results)

    async def get_graph(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        finalization_epoch: int | None,
    ) -> dict[str, Any]:
        """查询一个 Run/Epoch 的页面状态图和实际路线。"""

        if not agent_run_id:
            return _unavailable_graph(task_id, "authoritative_run_missing")
        pool = await self._get_pool()
        try:
            async with (
                pool.acquire() as connection,
                connection.transaction(isolation="repeatable_read", readonly=True),
            ):
                task_scope = await connection.fetchrow(
                    "SELECT target_platform, "
                    "lower(task_config->>'workflow_version')='v2' AS workflow_v2 "
                    "FROM qc_task WHERE id=$1::uuid",
                    task_id,
                )
                counts = await _projection_counts(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    finalization_epoch=finalization_epoch,
                )
                state_rows = await connection.fetch(
                    _state_query(), task_id, agent_run_id, finalization_epoch
                )
                transition_rows = await connection.fetch(
                    _transition_query(), task_id, agent_run_id, finalization_epoch
                )
                progress = await read_page_graph_progress(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    finalization_epoch=finalization_epoch,
                )
        except Exception as exc:  # noqa: BLE001 - 将数据库缺表显式转换为不可用
            if _looks_like_missing_graph_relation(exc):
                raise PageGraphUnavailableError("页面图迁移尚未执行") from exc
            raise
        state_count, action_count, projected_state_count, projected_transition_count = counts
        missing_captures = max(state_count - projected_state_count, 0)
        missing_actions = max(action_count - projected_transition_count, 0)
        if missing_captures or missing_actions:
            projection_status = "legacy_unprojected"
        else:
            projection_status = "available"
        states = [_row_dict(row) for row in state_rows]
        transitions = [_row_dict(row) for row in transition_rows]
        target_platform = _text(task_scope.get("target_platform")) if task_scope else None
        web_v2 = bool(task_scope and task_scope.get("workflow_v2"))
        if target_platform == "web" and web_v2:
            from services.api.app.infrastructure.db.web_page_graph_projection import (
                decode_web_graph_payloads,
            )

            states, transitions = decode_web_graph_payloads(states, transitions)
        return {
            "protocol_version": PAGE_GRAPH_PROTOCOL_VERSION,
            "status": projection_status,
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "finalization_epoch": finalization_epoch,
            "target_platform": target_platform,
            "source": "capture_action_projection",
            "counts": {
                "source_captures": state_count,
                "source_actions": action_count,
                "projected_states": projected_state_count,
                "projected_transitions": projected_transition_count,
                "unprojected_captures": missing_captures,
                "unprojected_actions": missing_actions,
            },
            "states": states,
            "transitions": transitions,
            **progress,
            "recovery_policy": {
                "eligible_only": "completed_low_risk_trusted",
                "requires_stepwise_reverification": True,
                "counts_external_or_overlay_as_business": False,
            },
        }

    async def _ensure_page_capture_state(
        self,
        connection: Any,
        row: Mapping[str, Any],
        *,
        prefix: str,
        metadata_override: Mapping[str, Any] | None = None,
        scope_valid: bool = True,
    ) -> PageStateSnapshot | None:
        if not scope_valid:
            # 保留 Action 的终态边，但不把跨 Session/Epoch 的 Capture
            # 伪装成当前阶段的节点。
            return None
        capture_id = _text(row.get(f"{prefix}_capture_id"))
        if capture_id is None:
            return None
        # 失败、跳过或仍 pending 的 Capture 不是可回放页面事实。Action 的
        # 终态边仍要保留，但不能因为边上挂着一个非 captured ID 就补造状态节点。
        if row.get(f"{prefix}_capture_status") != "captured":
            return None
        agent_run_id = _text(row.get(f"{prefix}_agent_run_id"))
        if agent_run_id is None:
            return None
        state_row = await connection.fetchrow(
            """
            SELECT state_id::text AS state_id, task_id::text AS task_id,
                agent_run_id::text AS agent_run_id, surface_kind,
                surface_session_id, finalization_epoch, command_sequence,
                capture_role, logical_page_key, identity_status,
                identity_source, page_type, page_state, state_fingerprint,
                is_overlay, is_external, counts_as_business_page,
                state_payload, captured_at
            FROM qc_page_graph_state
            WHERE state_id = $1::uuid
              AND task_id = $2::uuid
              AND agent_run_id = $3::uuid
              AND surface_session_id IS NOT DISTINCT FROM $5::text
              AND finalization_epoch IS NOT DISTINCT FROM $4::integer
            """,
            capture_id,
            str(row["task_id"]),
            agent_run_id,
            _int_or_none(row.get("finalization_epoch")),
            _text(row.get(f"{prefix}_surface_session_id")),
        )
        metadata = _mapping(row.get(f"{prefix}_metadata"))
        app_screenshot_artifact_id = row.get(f"{prefix}_app_screenshot_artifact_id")
        if app_screenshot_artifact_id is not None:
            app_package_name = row.get(f"{prefix}_app_package_name")
            app_activity = row.get(f"{prefix}_app_activity")
            metadata["package_name"] = (
                str(app_package_name) if app_package_name is not None else None
            )
            metadata["activity"] = str(app_activity) if app_activity is not None else None
            metadata["image_signature"] = str(row[f"{prefix}_app_image_signature"])
            metadata["screenshot_artifact_id"] = str(app_screenshot_artifact_id)
        page_url = _text(row.get(f"{prefix}_url"))
        if page_url is not None:
            metadata.setdefault("url", page_url)
        if metadata_override:
            metadata.update(_mapping(metadata_override))
        from services.api.app.infrastructure.db.web_page_graph_projection import (
            read_web_page_graph_facts,
        )

        web_facts = await read_web_page_graph_facts(
            connection, task_id=str(row["task_id"]), capture_id=capture_id,
            agent_run_id=agent_run_id, session_id=_text(row.get(f"{prefix}_surface_session_id")),
            surface_kind=str(row.get(f"{prefix}_surface_kind") or "web"),
        )
        if (state_row is None or metadata_override or app_screenshot_artifact_id is not None
                or web_facts is not None):
            snapshot = build_page_state_snapshot(
                capture_id=capture_id,
                task_id=str(row["task_id"]),
                agent_run_id=agent_run_id,
                surface_kind=str(row.get(f"{prefix}_surface_kind") or "web"),
                session_id=_text(row.get(f"{prefix}_surface_session_id")),
                finalization_epoch=_int_or_none(row.get("finalization_epoch")),
                command_sequence=_int_or_none(row.get("command_sequence")),
                capture_role=str(row.get(f"{prefix}_capture_role") or "observation"),
                metadata=metadata,
                web_facts=web_facts,
                page_type=(row.get(f"{prefix}_page_type") or state_row.get("page_type"))
                if state_row is not None
                else row.get(f"{prefix}_page_type"),
                page_state=(row.get(f"{prefix}_page_state") or state_row.get("page_state"))
                if state_row is not None
                else row.get(f"{prefix}_page_state"),
                captured_at=(state_row.get("captured_at") or row.get(f"{prefix}_captured_at"))
                if state_row is not None
                else row.get(f"{prefix}_captured_at"),
            )
            await _insert_state_on_connection(connection, snapshot)
            return snapshot
        return _snapshot_from_row(state_row)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def _insert_state_on_connection(
    connection: Any,
    snapshot: PageStateSnapshot,
) -> None:
    # state_id 与 Capture 一一绑定。Session 重开后，历史 Capture 的旧
    # epoch 不能被 ON CONFLICT 更新成当前 epoch；身份或证据可以补全，
    # 但任务、Run、端、Session 和阶段作用域必须保持不可变。
    existing = await connection.fetchrow(
        """
        SELECT
            task_id::text AS task_id,
            agent_run_id::text AS agent_run_id,
            surface_kind,
            surface_session_id,
            finalization_epoch
        FROM qc_page_graph_state
        WHERE state_id = $1::uuid
        """,
        snapshot.capture_id,
    )
    if existing is not None:
        existing_scope = (
            _text(existing.get("task_id")),
            _text(existing.get("agent_run_id")),
            _text(existing.get("surface_kind")),
            _text(existing.get("surface_session_id")),
            _int_or_none(existing.get("finalization_epoch")),
        )
        snapshot_scope = (
            snapshot.task_id,
            snapshot.agent_run_id,
            snapshot.surface_kind,
            snapshot.session_id,
            snapshot.finalization_epoch,
        )
        if existing_scope != snapshot_scope:
            raise PageGraphProjectionError(
                "页面状态已属于其他 Task/Run/Session/Epoch，拒绝迁移历史 Capture"
            )
    await connection.execute(
        """
        INSERT INTO qc_page_graph_state (
            state_id, task_id, agent_run_id, surface_kind,
            surface_session_id, finalization_epoch, command_sequence,
            capture_role, logical_page_key, identity_status, identity_source,
            page_type, page_state, state_fingerprint, is_overlay, is_external,
            counts_as_business_page, state_payload, captured_at
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10,
            $11, $12, $13, $14, $15, $16, $17, $18::jsonb, $19
        )
        ON CONFLICT (state_id) DO UPDATE SET
            task_id = EXCLUDED.task_id,
            agent_run_id = EXCLUDED.agent_run_id,
            surface_kind = EXCLUDED.surface_kind,
            surface_session_id = EXCLUDED.surface_session_id,
            finalization_epoch = EXCLUDED.finalization_epoch,
            command_sequence = EXCLUDED.command_sequence,
            capture_role = EXCLUDED.capture_role,
            logical_page_key = EXCLUDED.logical_page_key,
            identity_status = EXCLUDED.identity_status,
            identity_source = EXCLUDED.identity_source,
            page_type = COALESCE(EXCLUDED.page_type, qc_page_graph_state.page_type),
            page_state = COALESCE(EXCLUDED.page_state, qc_page_graph_state.page_state),
            state_fingerprint = EXCLUDED.state_fingerprint,
            is_overlay = EXCLUDED.is_overlay,
            is_external = EXCLUDED.is_external,
            counts_as_business_page = EXCLUDED.counts_as_business_page,
            state_payload = EXCLUDED.state_payload,
            captured_at = COALESCE(EXCLUDED.captured_at, qc_page_graph_state.captured_at),
            updated_at = now()
        """,
        snapshot.capture_id,
        snapshot.task_id,
        snapshot.agent_run_id,
        snapshot.surface_kind,
        snapshot.session_id,
        snapshot.finalization_epoch,
        snapshot.command_sequence,
        snapshot.capture_role,
        snapshot.identity.key,
        snapshot.identity.status,
        snapshot.identity.source,
        snapshot.page_type,
        snapshot.page_state,
        snapshot.state_fingerprint,
        snapshot.is_overlay,
        snapshot.is_external,
        snapshot.counts_as_business_page,
        _json_dumps(snapshot.state_payload),
        snapshot.captured_at,
    )


async def _load_route_parent(
    connection: Any,
    *,
    task_id: str,
    agent_run_id: str | None,
    session_id: str | None,
    finalization_epoch: int | None,
    source_state_id: str | None,
    action_execution_id: str,
    target_state_id: str | None,
) -> tuple[str | None, list[str], int]:
    if source_state_id is None or agent_run_id is None:
        return None, [item for item in (source_state_id, target_state_id) if item], 0
    row = await connection.fetchrow(
        """
        SELECT transition_id, route_path, navigation_depth
        FROM qc_page_graph_transition
        WHERE task_id = $1::uuid
          AND agent_run_id = $2::uuid
          AND surface_session_id IS NOT DISTINCT FROM $3::text
          AND finalization_epoch IS NOT DISTINCT FROM $4::integer
          AND target_state_id = $5::uuid
          AND status = 'completed'
          AND proof = 'trusted'
          AND counts_as_business_arrival
          AND replay_eligible
          AND transition_id <> $6
        ORDER BY command_sequence DESC NULLS LAST, created_at DESC, transition_id DESC
        LIMIT 1
        """,
        task_id,
        agent_run_id,
        session_id,
        finalization_epoch,
        source_state_id,
        action_execution_id,
    )
    if row is None:
        return None, [item for item in (source_state_id, target_state_id) if item], 0
    path = _json_list(row.get("route_path"))
    if target_state_id is not None:
        path.append(target_state_id)
    return (
        _text(row.get("transition_id")),
        path,
        int(row.get("navigation_depth") or 0) + 1,
    )


async def _projection_counts(
    connection: Any,
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int | None,
) -> tuple[int, int, int, int]:
    """按同一 Run/Epoch 对账 Capture/Action 与其逐条图投影。

    ``projected_*_count`` 不能直接统计图表总行数：重复边、历史边或别的
    Capture 的投影都可能把总数相减伪装成“已全部投影”。这里先固定权威
    来源集合，再用来源 ID 的 ``EXISTS`` 逐条核对。Epoch 也只做精确的
    ``IS NOT DISTINCT FROM`` 匹配；``NULL`` 表示未知归属，不是全阶段通配。
    """
    row = await connection.fetchrow(
        f"""
        WITH capture_candidates AS (
            SELECT
                capture.id AS capture_id,
                capture.task_id,
                capture.agent_run_id,
                {capture_epoch_sql()} AS effective_epoch
            FROM qc_capture AS capture
            LEFT JOIN qc_surface_session AS session
              ON session.task_id = capture.task_id
             AND session.session_id = capture.surface_session_id
            WHERE capture.task_id = $1::uuid
              AND capture.agent_run_id = $2::uuid
              AND capture.capture_status = 'captured'
              AND (capture.surface_session_id IS NULL OR session.session_id IS NOT NULL)
        ), scoped_captures AS (
            SELECT *
            FROM capture_candidates
            WHERE effective_epoch IS NOT DISTINCT FROM $3::integer
        ), action_candidates AS (
            SELECT
                action.action_execution_id,
                action.task_id,
                action.finalization_epoch AS effective_epoch
            FROM qc_action_execution AS action
            LEFT JOIN qc_surface_session AS session
              ON session.task_id = action.task_id
             AND session.session_id = action.session_id
            LEFT JOIN qc_capture AS source
              ON source.id = action.source_capture_id
             AND source.task_id = action.task_id
            LEFT JOIN qc_capture AS target
              ON target.id = action.result_capture_id
             AND target.task_id = action.task_id
            WHERE action.task_id = $1::uuid
              AND (
                  source.agent_run_id = $2::uuid
                  OR (
                      source.agent_run_id IS NULL
                      AND target.agent_run_id = $2::uuid
                  )
                  OR (
                      -- 与 rebuild_run 保持同一窄范围：入口哨兵只对尚未绑定
                      -- 任何 Capture 的 bootstrap 动作兜底，不能覆盖真实归属。
                      source.agent_run_id IS NULL
                      AND target.agent_run_id IS NULL
                      AND action.source_capture_id IS NULL
                      AND action.result_capture_id IS NULL
                      AND action.intent_payload->>'source_capture_id' IN (
                          'h5-entry:' || $2::text,
                          'agent-entry:' || $2::text
                      )
                  )
              )
              AND session.session_id IS NOT NULL
        ), scoped_actions AS (
            SELECT *
            FROM action_candidates
            WHERE effective_epoch IS NOT DISTINCT FROM $3::integer
        )
        SELECT
            (SELECT count(*) FROM scoped_captures) AS source_capture_count,
            (SELECT count(*) FROM scoped_actions) AS source_action_count,
            (
                SELECT count(*)
                FROM scoped_captures AS source
                WHERE EXISTS (
                    SELECT 1
                    FROM qc_page_graph_state AS state
                    WHERE state.state_id = source.capture_id
                      AND state.task_id = source.task_id
                      AND state.agent_run_id = source.agent_run_id
                      AND state.finalization_epoch IS NOT DISTINCT FROM source.effective_epoch
                )
            ) AS projected_state_count,
            (
                SELECT count(*)
                FROM scoped_actions AS source
                WHERE EXISTS (
                    SELECT 1
                    FROM qc_page_graph_transition AS edge
                    WHERE edge.task_id = source.task_id
                      AND edge.action_execution_id = source.action_execution_id
                      AND edge.agent_run_id = $2::uuid
                      AND edge.finalization_epoch IS NOT DISTINCT FROM source.effective_epoch
                )
            ) AS projected_transition_count
        """,
        task_id,
        agent_run_id,
        finalization_epoch,
    )
    if row is None:
        raise PageGraphProjectionError("页面图计数查询没有返回结果")
    return tuple(
        int(row[key] or 0)
        for key in (
            "source_capture_count",
            "source_action_count",
            "projected_state_count",
            "projected_transition_count",
        )
    )  # type: ignore[return-value]


def _state_query() -> str:
    return """
        SELECT
            state.state_id::text AS state_id,
            state.task_id::text AS task_id,
            state.agent_run_id::text AS agent_run_id,
            state.surface_kind,
            state.surface_session_id,
            state.finalization_epoch,
            state.command_sequence,
            state.capture_role,
            state.logical_page_key,
            state.identity_status,
            state.identity_source,
            state.page_type,
            state.page_state,
            state.state_fingerprint,
            state.is_overlay,
            state.is_external,
            state.counts_as_business_page,
            state.state_payload,
            state.captured_at,
            capture.parent_capture_id::text AS parent_capture_id
        FROM qc_page_graph_state AS state
        JOIN qc_capture AS capture ON capture.id = state.state_id
          AND capture.task_id = state.task_id AND capture.agent_run_id = state.agent_run_id
        WHERE state.task_id = $1::uuid
          AND state.agent_run_id = $2::uuid
          AND state.finalization_epoch IS NOT DISTINCT FROM $3::integer
        ORDER BY state.command_sequence NULLS FIRST, state.captured_at, state.state_id
    """


def _transition_query() -> str:
    return """
        SELECT
            edge.transition_id,
            edge.task_id::text AS task_id,
            edge.agent_run_id::text AS agent_run_id,
            edge.surface_kind,
            edge.surface_session_id,
            edge.finalization_epoch,
            edge.command_sequence,
            edge.action_execution_id,
            edge.source_state_id::text AS source_state_id,
            edge.target_state_id::text AS target_state_id,
            edge.action_kind,
            edge.semantic_target,
            edge.risk_level,
            edge.status,
            edge.terminal,
            edge.proof,
            edge.counts_as_business_arrival,
            edge.replay_eligible,
            edge.reason_codes,
            edge.outcome_payload,
            edge.parent_transition_id,
            edge.route_path,
            edge.navigation_depth,
            edge.started_at,
            edge.finished_at
        FROM qc_page_graph_transition AS edge
        WHERE edge.task_id = $1::uuid
          AND edge.agent_run_id = $2::uuid
          AND edge.finalization_epoch IS NOT DISTINCT FROM $3::integer
        ORDER BY edge.command_sequence NULLS LAST, edge.created_at, edge.transition_id
    """


def _snapshot_from_row(row: Mapping[str, Any]) -> PageStateSnapshot:
    from services.api.app.domain.exploration.page_graph import PageIdentity

    return PageStateSnapshot(
        capture_id=str(row["state_id"]),
        task_id=str(row["task_id"]),
        agent_run_id=_text(row.get("agent_run_id")),
        surface_kind=str(row["surface_kind"]),
        session_id=_text(row.get("surface_session_id")),
        finalization_epoch=_int_or_none(row.get("finalization_epoch")),
        command_sequence=_int_or_none(row.get("command_sequence")),
        capture_role=str(row["capture_role"]),
        identity=PageIdentity(
            key=str(row["logical_page_key"]),
            status=str(row["identity_status"]),
            source=str(row["identity_source"]),
        ),
        page_type=_text(row.get("page_type")),
        page_state=_text(row.get("page_state")),
        state_fingerprint=str(row["state_fingerprint"]),
        is_overlay=bool(row.get("is_overlay")),
        is_external=bool(row.get("is_external")),
        state_payload=_mapping(row.get("state_payload")),
        captured_at=row.get("captured_at"),
    )


def _action_owner_run_id(
    row: Mapping[str, Any], intent_payload: Mapping[str, Any]
) -> str | None:
    """按真实 Capture 归属选择动作 Run，不受状态投影可用性影响。"""

    source_run_id = _text(row.get("source_agent_run_id"))
    if source_run_id is not None:
        return source_run_id
    target_run_id = _text(row.get("target_agent_run_id"))
    if target_run_id is not None:
        return target_run_id
    # 只有动作两端都没有真实 Capture 引用时，入口哨兵才是合法来源。
    if (
        _text(row.get("source_capture_id")) is None
        and _text(row.get("target_capture_id")) is None
    ):
        return _run_id_from_intent(intent_payload)
    return None


def _run_id_from_intent(intent_payload: Mapping[str, Any]) -> str | None:
    """入口动作没有 Capture 时，从受信入口哨兵恢复 Run 归属。"""

    source_capture_id = _text(intent_payload.get("source_capture_id"))
    if source_capture_id is None:
        return None
    if not re.fullmatch(_ENTRY_RUN_MARKER_PATTERN, source_capture_id):
        return None
    for prefix in ("h5-entry:", "agent-entry:"):
        if not source_capture_id.startswith(prefix):
            continue
        candidate = source_capture_id.removeprefix(prefix)
        try:
            return str(UUID(candidate))
        except (ValueError, AttributeError):
            return None
    return None


def _verified_url_markers(
    row: Mapping[str, Any],
    result_payload: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """仅在普通 H5 动作实际产生 URL 变化时签发 URL 身份标记。"""

    surface_kind = _text(row.get("source_surface_kind") or row.get("target_surface_kind"))
    if (
        surface_kind != "web"
        or row.get("status") != "completed"
        or row.get("error_code")
        or row.get("error_message")
        or result_payload.get("page_graph_scope_issues")
        or row.get("target_capture_role") != "after_action"
    ):
        return {}
    for prefix in ("source", "target"):
        metadata = _mapping(row.get(f"{prefix}_metadata"))
        detection = _mapping(metadata.get("page_detection"))
        confidence = detection.get("confidence")
        if (
            row.get(f"{prefix}_capture_status") != "captured"
            or row.get(f"{prefix}_page_state")
            not in {"stable", "normal", "loaded", "content_available"}
            or metadata.get("reachable") is not True
            or detection.get("detection_status") != "recognized"
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.70 <= confidence <= 1.0
        ):
            return {}
    state_diff = _mapping(result_payload.get("state_diff"))
    if state_diff.get("location_changed") is not True:
        return {}
    source_capture_id = _text(row.get("source_capture_id"))
    target_capture_id = _text(row.get("target_capture_id"))
    source_url = canonical_url_identity(row.get("source_url"))
    target_url = canonical_url_identity(row.get("target_url"))
    if (
        source_capture_id is None
        or target_capture_id is None
        or source_url is None
        or target_url is None
        or source_url == target_url
    ):
        return {}
    external = _url_origin(source_url) != _url_origin(target_url)
    common = {
        "protocol_version": WEB_URL_AFTER_PROTOCOL_VERSION,
        "verified": True,
        "verified_by": "action_after_capture",
    }
    return {
        "source": {
            "url": source_url,
            "trusted_url_identity": {
                **common,
                "capture_id": source_capture_id,
                "source_capture_id": source_capture_id,
            },
        },
        "target": {
            "url": target_url,
            "trusted_url_identity": {
                **common,
                "capture_id": target_capture_id,
                "source_capture_id": source_capture_id,
            },
            "is_external": external,
            "external_navigation": external,
            "external_url": target_url if external else None,
        },
    }


def _trusted_h5_source_identity_override(
    row: Mapping[str, Any],
    intent_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """用已签名父状态恢复 H5 动作前快照身份，不降低图的可信门槛。"""

    requested_capture_id = _text(row.get("requested_source_capture_id"))
    source_capture_id = _text(row.get("source_capture_id"))
    if (
        _text(row.get("target_platform")) != "h5"
        or _text(row.get("source_surface_kind")) != "web"
        or row.get("source_capture_role") not in {"before_action", "probe_before"}
        or requested_capture_id is None
        or source_capture_id is None
        or requested_capture_id == source_capture_id
        or _text(row.get("source_parent_capture_id")) != requested_capture_id
        or row.get("requested_identity_status") != "confirmed"
        or row.get("requested_identity_source") != "logical_surface"
    ):
        return {}
    source_url = canonical_url_identity(row.get("source_url"))
    requested_url = canonical_url_identity(row.get("requested_source_url"))
    if source_url is None or source_url != requested_url:
        return {}

    consistency = _mapping(
        _mapping(_mapping(row.get("source_metadata")).get("pipeline_metrics")).get(
            "capture_consistency"
        )
    )
    if consistency.get("status") not in {
        "consistent",
        "consistent_with_capability_gaps",
    } or consistency.get("changed_signals") != []:
        return {}

    logical_surface = _mapping(
        _mapping(row.get("requested_state_payload")).get("logical_surface")
    )
    logical_target = _mapping(
        _mapping(intent_payload.get("parameters")).get("logical_target")
    )
    if (
        logical_target.get("protocol_version") != "logical_target.v1"
        or logical_surface.get("protocol_version") != "logical_surface.v1"
        or logical_target.get("source_identity_fingerprint")
        != logical_surface.get("identity_fingerprint")
        or logical_target.get("source_state_fingerprint")
        != logical_surface.get("state_fingerprint")
    ):
        return {}
    return {
        "logical_surface": logical_surface,
        "page_graph_identity_lineage": {
            "protocol_version": "h5-action-source-lineage.v1",
            "verified": True,
            "requested_source_capture_id": requested_capture_id,
            "source_capture_id": source_capture_id,
            "verified_by": "parent_capture_consistency_and_logical_target",
        },
    }


def _url_origin(value: str) -> tuple[str, str, int | None]:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port


def _unavailable_graph(task_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "protocol_version": PAGE_GRAPH_PROTOCOL_VERSION,
        "status": "unavailable",
        "task_id": task_id,
        "reason_code": reason_code,
        "states": [],
        "transitions": [],
        "recovery_policy": {
            "eligible_only": "completed_low_risk_trusted",
            "requires_stepwise_reverification": True,
        },
    }


def _looks_like_missing_graph_relation(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return "qc_page_graph_" in text and (
        "does not exist" in text or "undefinedtable" in text or "undefined relation" in text
    )


def _row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if 'reason_codes' in result:
        reasons = result['reason_codes']
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except json.JSONDecodeError:
                reasons = None
        result['reason_codes'] = (
            reasons if isinstance(reasons, list) and all(isinstance(x, str) for x in reasons)
            else ['graph_reason_codes_invalid']
        )
    return result


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata_finalization_epoch(metadata: Mapping[str, Any] | None) -> int | None:
    """读取 Capture 自带的 epoch 标记；非法值保持未知而不猜测归属。"""
    value = _mapping(metadata).get("finalization_epoch")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        return None
    return _int_or_none(value)


def _capture_finalization_epoch(row: Mapping[str, Any]) -> int | None:
    """读取 SQL 已核对的采集/动作阶段；会话重开不改变历史归属。"""
    surface_session_id = _text(row.get("surface_session_id"))
    session_record_id = _text(row.get("session_record_id"))
    if surface_session_id is not None and session_record_id is None:
        raise PageGraphProjectionError("Capture 的 Surface Session 不存在，无法确认 epoch")
    metadata_epoch = _metadata_finalization_epoch(_mapping(row.get("metadata")))
    effective_epoch = _int_or_none(row.get("effective_finalization_epoch"))
    if metadata_epoch is not None and effective_epoch != metadata_epoch:
        raise PageGraphProjectionError("Capture 的有效 epoch 无法由权威事实证明")
    return effective_epoch


def _action_epoch_scope_issues(row: Mapping[str, Any]) -> tuple[str, ...]:
    """列出 Action/Session/Capture 作用域问题，但仍保留未知终态边。"""
    issues: list[str] = []
    action_epoch = _int_or_none(row.get("finalization_epoch"))
    if action_epoch is None:
        issues.append("action_epoch_missing")
    source_run_id = _text(row.get("source_agent_run_id"))
    target_run_id = _text(row.get("target_agent_run_id"))
    if source_run_id is not None and target_run_id is not None and source_run_id != target_run_id:
        issues.extend(("cross_run_capture_pair", "source_capture_scope_invalid"))
        issues.append("target_capture_scope_invalid")
    for prefix in ("source", "target"):
        capture_id = _text(row.get(f"{prefix}_capture_id"))
        if capture_id is None:
            continue
        if row.get(f"{prefix}_capture_status") != "captured":
            issues.append(f"{prefix}_capture_not_captured")
            continue
        session_id = _text(row.get(f"{prefix}_surface_session_id"))
        session_record_id = _text(row.get(f"{prefix}_session_record_id"))
        if (
            session_id is None
            or session_record_id is None
            or session_id != _text(row.get("session_id"))
        ):
            issues.append(f"{prefix}_capture_scope_invalid")
            continue
        capture_epoch = _int_or_none(row.get(f"{prefix}_capture_epoch"))
        if action_epoch is None or capture_epoch != action_epoch:
            issues.append(f"{prefix}_capture_scope_invalid")
    return tuple(dict.fromkeys(issues))


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
