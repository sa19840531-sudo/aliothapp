"""探索级 Evidence Index 的 PostgreSQL 仓储；封存不发 BundleReady。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from services.api.app.application.evidence_indexes import EvidenceInventory
from services.api.app.domain.evidence.index import (
    EvidenceActionLogItem,
    EvidenceArtifactIndexItem,
    EvidenceCaptureLineage,
    EvidenceGapCode,
    EvidenceGapItem,
    EvidenceIndexReference,
)
from services.api.app.domain.evidence.vocabulary import TERMINAL_CAPTURE_ROLES
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool

_ACTION_TERMINAL = (
    "completed",
    "evidence_failed",
    "blocked",
    "failed",
    "timed_out",
)
_EXPLORATION_WORK_TERMINAL = (
    "succeeded",
    "bounded",
    "budget_exhausted",
    "blocked",
    "no_progress",
    "failed",
    "cancelled",
    "superseded",
)
_IN_FLIGHT_WORK = ("leased", "running")
_ACTION_EXECUTED_TERMINAL = (
    "completed",
    "evidence_failed",
    "blocked",
    "failed",
    "timed_out",
)


class EvidenceIndexPersistenceError(RuntimeError):
    """Evidence Index 封存冲突或盘点归属无效。"""


class PostgresEvidenceIndexRepository:
    """盘点会话 Capture/Manifest/动作配对并幂等写入 qc_evidence_index。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def inventory(self, *, task_id: str, session_id: str) -> EvidenceInventory:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            captures = await connection.fetch(
                """
                SELECT
                    capture.id::text AS capture_id,
                    capture.capture_role AS role,
                    capture.parent_capture_id::text AS parent_capture_id,
                    capture.action_execution_id
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                ORDER BY capture.created_at, capture.id
                """,
                task_id,
                session_id,
            )
            pending_manifests = await connection.fetch(
                """
                SELECT latest.manifest_id, latest.capture_id
                FROM (
                    SELECT DISTINCT ON (manifest.capture_id)
                        manifest.manifest_id,
                        manifest.capture_id::text AS capture_id,
                        manifest.quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = $1::uuid
                      AND manifest.session_id = $2
                    ORDER BY manifest.capture_id, manifest.manifest_version DESC
                ) AS latest
                WHERE latest.quality_status = 'pending'
                """,
                task_id,
                session_id,
            )
            latest_pass = await connection.fetch(
                """
                SELECT DISTINCT ON (manifest.capture_id)
                    manifest.capture_id::text AS capture_id,
                    manifest.manifest_id,
                    manifest.quality_status
                FROM qc_evidence_manifest AS manifest
                WHERE manifest.task_id = $1::uuid
                  AND manifest.session_id = $2
                ORDER BY manifest.capture_id, manifest.manifest_version DESC
                """,
                task_id,
                session_id,
            )
            unbound = await connection.fetch(
                """
                SELECT artifact.id::text AS artifact_id,
                       artifact.page_capture_id::text AS capture_id
                FROM qc_evidence_artifact AS artifact
                INNER JOIN qc_capture AS capture
                    ON capture.id = artifact.page_capture_id
                LEFT JOIN LATERAL (
                    SELECT quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS latest ON TRUE
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND artifact.source = 'app_runtime'
                  AND capture.capture_role <> ALL($3::text[])
                  AND (
                      latest.quality_status IS NULL
                      OR latest.quality_status <> 'pass'
                  )
                """,
                task_id,
                session_id,
                list(TERMINAL_CAPTURE_ROLES),
            )
            pending_actions = await connection.fetch(
                """
                SELECT action.action_execution_id
                FROM qc_action_execution AS action
                WHERE action.task_id = $1::uuid
                  AND action.session_id = $2
                  AND action.status <> ALL($3::text[])
                """,
                task_id,
                session_id,
                list(_ACTION_TERMINAL),
            )
            unpaired = await connection.fetch(
                """
                SELECT action.action_execution_id
                FROM qc_action_execution AS action
                INNER JOIN qc_capture AS before_cap
                    ON before_cap.action_execution_id = action.action_execution_id
                   AND before_cap.capture_role = 'before_action'
                   AND before_cap.capture_status = 'captured'
                LEFT JOIN qc_capture AS after_cap
                    ON after_cap.action_execution_id = action.action_execution_id
                   AND after_cap.capture_role = 'after_action'
                   AND after_cap.capture_status = 'captured'
                WHERE action.task_id = $1::uuid
                  AND action.session_id = $2
                  AND after_cap.id IS NULL
                """,
                task_id,
                session_id,
            )
            executed_unpaired = await connection.fetch(
                """
                SELECT
                    action.action_execution_id,
                    (before_cap.id IS NULL) AS missing_before,
                    (after_cap.id IS NULL) AS missing_after
                FROM qc_action_execution AS action
                LEFT JOIN qc_capture AS before_cap
                    ON before_cap.action_execution_id = action.action_execution_id
                   AND before_cap.capture_role = 'before_action'
                   AND before_cap.capture_status = 'captured'
                LEFT JOIN qc_capture AS after_cap
                    ON after_cap.action_execution_id = action.action_execution_id
                   AND after_cap.capture_role = 'after_action'
                   AND after_cap.capture_status = 'captured'
                WHERE action.task_id = $1::uuid
                  AND action.session_id = $2
                  AND action.status = ANY($3::text[])
                  AND (before_cap.id IS NULL OR after_cap.id IS NULL)
                """,
                task_id,
                session_id,
                list(_ACTION_EXECUTED_TERMINAL),
            )
            missing_screenshots = await connection.fetch(
                """
                SELECT capture.id::text AS capture_id
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_evidence_artifact AS artifact
                      WHERE artifact.page_capture_id = capture.id
                        AND artifact.artifact_type = 'screenshot'
                  )
                """,
                task_id,
                session_id,
            )
            nonterminal_works = await connection.fetch(
                """
                SELECT work_item.work_id, work_item.status
                FROM qc_v2_work_item AS work_item
                WHERE work_item.task_id = $1::uuid
                  AND work_item.session_id = $2
                  AND work_item.work_type = 'exploration'
                  AND work_item.status <> ALL($3::text[])
                """,
                task_id,
                session_id,
                list(_EXPLORATION_WORK_TERMINAL),
            )
            missing_ui_trees = await connection.fetch(
                """
                SELECT capture.id::text AS capture_id
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_evidence_artifact AS artifact
                      WHERE artifact.page_capture_id = capture.id
                        AND artifact.artifact_type IN ('ui_tree', 'ui_snapshot')
                  )
                """,
                task_id,
                session_id,
            )
            missing_action_logs = await connection.fetch(
                """
                SELECT capture.id::text AS capture_id
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                  AND capture.capture_role = 'after_action'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_evidence_artifact AS artifact
                      WHERE artifact.page_capture_id = capture.id
                        AND artifact.artifact_type = 'action_trace'
                  )
                """,
                task_id,
                session_id,
            )
            artifact_rows = await connection.fetch(
                """
                SELECT
                    artifact.id::text AS artifact_id,
                    artifact.artifact_type,
                    artifact.checksum,
                    artifact.content_type,
                    artifact.size_bytes,
                    artifact.task_id::text AS artifact_task_id,
                    artifact.page_capture_id::text AS capture_id,
                    capture.task_id::text AS capture_task_id
                FROM qc_evidence_artifact AS artifact
                INNER JOIN qc_capture AS capture
                    ON capture.id = artifact.page_capture_id
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                  AND artifact.source = 'app_runtime'
                ORDER BY capture.created_at, artifact.artifact_type, artifact.id
                """,
                task_id,
                session_id,
            )
            cross_task_parents = await connection.fetch(
                """
                SELECT capture.id::text AS capture_id,
                       parent.task_id::text AS parent_task_id
                FROM qc_capture AS capture
                INNER JOIN qc_capture AS parent
                    ON parent.id = capture.parent_capture_id
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND parent.task_id IS DISTINCT FROM capture.task_id
                """,
                task_id,
                session_id,
            )
            drain_row = await connection.fetchrow(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM qc_action_execution AS action
                        WHERE action.task_id = $1::uuid
                          AND action.session_id = $2
                          AND action.status IN ('planned', 'executing', 'evidence_pending')
                    )::integer AS pending_actions,
                    (
                        SELECT count(*)
                        FROM qc_action_execution AS action
                        WHERE action.task_id = $1::uuid
                          AND action.session_id = $2
                          AND action.status = 'evidence_pending'
                    )::integer AS pending_capture_commits,
                    (
                        SELECT count(*)
                        FROM qc_restoration_attempt AS restoration
                        WHERE restoration.task_id = $1::uuid
                          AND restoration.target_session_id = $2
                          AND restoration.status = 'running'
                    )::integer AS running_restorations
                """,
                task_id,
                session_id,
            )
            action_log_rows = await connection.fetch(
                """
                SELECT
                    action.action_execution_id,
                    artifact.id::text AS artifact_id,
                    artifact.checksum,
                    capture.id::text AS capture_id
                FROM qc_action_execution AS action
                INNER JOIN qc_capture AS capture
                    ON capture.action_execution_id = action.action_execution_id
                   AND capture.capture_role = 'after_action'
                   AND capture.capture_status = 'captured'
                INNER JOIN qc_evidence_artifact AS artifact
                    ON artifact.page_capture_id = capture.id
                   AND artifact.artifact_type = 'action_trace'
                WHERE action.task_id = $1::uuid
                  AND action.session_id = $2
                ORDER BY action.action_execution_id, artifact.id
                """,
                task_id,
                session_id,
            )
            timeline_rows = await connection.fetch(
                """
                SELECT
                    visual.capture_id::text AS capture_id,
                    visual.visual_page_type,
                    visual.visual_summary,
                    visual.created_at
                FROM qc_app_visual_observation AS visual
                WHERE visual.task_id = $1::uuid
                  AND visual.session_id = $2
                ORDER BY visual.created_at, visual.capture_id
                """,
                task_id,
                session_id,
            )

        capture_ids = tuple(str(row["capture_id"]) for row in captures)
        role_by_capture = {
            str(row["capture_id"]): str(row["role"] or "") for row in captures
        }
        pass_by_capture = {
            str(row["capture_id"]): str(row["manifest_id"])
            for row in latest_pass
            if str(row["quality_status"]) == "pass"
        }
        manifest_ids = tuple(pass_by_capture[cid] for cid in capture_ids if cid in pass_by_capture)
        gaps: list[EvidenceGapItem] = []
        if not capture_ids:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.NO_CAPTURES,
                    subject_id=session_id,
                    detail="会话没有任何已提交 Capture",
                )
            )
        for row in pending_manifests:
            if role_by_capture.get(str(row["capture_id"])) in TERMINAL_CAPTURE_ROLES:
                continue
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.PENDING_MANIFEST,
                    subject_id=str(row["manifest_id"]),
                    detail=f"Capture {row['capture_id']} 的 Manifest 仍为 pending",
                )
            )
        latest_status = {
            str(row["capture_id"]): str(row["quality_status"]) for row in latest_pass
        }
        for capture_id in capture_ids:
            if role_by_capture.get(capture_id) in TERMINAL_CAPTURE_ROLES:
                continue
            status = latest_status.get(capture_id)
            if status == "pass":
                continue
            if status == "fail":
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.WRITE_TIMEOUT,
                        subject_id=capture_id,
                        detail="Capture Manifest 已固化 fail，不得再 pending",
                    )
                )
                continue
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.PENDING_MANIFEST,
                    subject_id=capture_id,
                    detail="已提交 Capture 没有 pass Manifest",
                )
            )
        for row in unbound:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.UNBOUND_ARTIFACT,
                    subject_id=str(row["artifact_id"]),
                    detail=f"制品未绑定 pass Manifest（capture={row['capture_id']}）",
                )
            )
        for row in pending_actions:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.PENDING_ACTION,
                    subject_id=str(row["action_execution_id"]),
                    detail="动作仍非终态或停留 evidence_pending",
                )
            )
        for row in unpaired:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.UNPAIRED_BEFORE_AFTER,
                    subject_id=str(row["action_execution_id"]),
                    detail="已有 before_action 但缺少 after_action",
                )
            )
        for row in executed_unpaired:
            action_id = str(row["action_execution_id"])
            if bool(row["missing_before"]):
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.MISSING_BEFORE_CAPTURE,
                        subject_id=action_id,
                        detail="已执行动作缺少 before_action Capture",
                    )
                )
            if bool(row["missing_after"]):
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.MISSING_AFTER_CAPTURE,
                        subject_id=action_id,
                        detail="已执行动作缺少 after_action Capture",
                    )
                )
        for row in missing_screenshots:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_SCREENSHOT,
                    subject_id=str(row["capture_id"]),
                    detail="已提交 Capture 缺少必需截图",
                )
            )
        for row in nonterminal_works:
            status = str(row["status"])
            if status in _IN_FLIGHT_WORK:
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.IN_FLIGHT_WORK,
                        subject_id=str(row["work_id"]),
                        detail=f"Exploration Work 仍在途（{status}）",
                    )
                )
            else:
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.NONTERMINAL_WORK,
                        subject_id=str(row["work_id"]),
                        detail=f"Exploration Work 未终结（{status}）",
                    )
                )
        for row in missing_ui_trees:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_UI_TREE,
                    subject_id=str(row["capture_id"]),
                    detail="已提交 Capture 缺少 UI Tree／ui_snapshot",
                )
            )
        for row in missing_action_logs:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_ACTION_LOG,
                    subject_id=str(row["capture_id"]),
                    detail="after_action Capture 缺少动作日志 action_trace",
                )
            )
        for row in artifact_rows:
            if str(row["artifact_task_id"]) != str(row["capture_task_id"]):
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.CROSS_TASK_BINDING,
                        subject_id=str(row["artifact_id"]),
                        detail=(
                            f"制品 task_id={row['artifact_task_id']} "
                            f"与 Capture task_id={row['capture_task_id']} 不一致"
                        ),
                    )
                )
            reason = _corrupt_artifact_reason(row)
            if reason is not None:
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.CORRUPT_ARTIFACT,
                        subject_id=str(row["artifact_id"]),
                        detail=reason,
                    )
                )
        for row in cross_task_parents:
            gaps.append(
                EvidenceGapItem(
                    code=EvidenceGapCode.CROSS_TASK_BINDING,
                    subject_id=str(row["capture_id"]),
                    detail=f"父 Capture 属于其他任务 {row['parent_task_id']}",
                )
            )
        if drain_row is not None:
            if int(drain_row["pending_actions"] or 0) > 0 or int(
                drain_row["running_restorations"] or 0
            ) > 0:
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.IN_FLIGHT_COMMAND,
                        subject_id=session_id,
                        detail=(
                            "在途设备命令或恢复未排空"
                            f"（actions={drain_row['pending_actions']},"
                            f" restorations={drain_row['running_restorations']}）"
                        ),
                    )
                )
            if int(drain_row["pending_capture_commits"] or 0) > 0:
                gaps.append(
                    EvidenceGapItem(
                        code=EvidenceGapCode.PENDING_OBJECT_WRITE,
                        subject_id=session_id,
                        detail=(
                            "对象存储／数据库绑定仍在途"
                            f"（pending_capture_commits={drain_row['pending_capture_commits']}）"
                        ),
                    )
                )
        page_timeline = tuple(_page_timeline_entry(row) for row in timeline_rows)
        capture_lineage = tuple(
            EvidenceCaptureLineage(
                capture_id=str(row["capture_id"]),
                role=str(row["role"] or "unknown"),
                parent_capture_id=(
                    str(row["parent_capture_id"])
                    if row["parent_capture_id"] is not None
                    else None
                ),
                action_execution_id=(
                    str(row["action_execution_id"])
                    if row["action_execution_id"] is not None
                    else None
                ),
            )
            for row in captures
        )
        artifacts = tuple(
            EvidenceArtifactIndexItem(
                artifact_id=str(row["artifact_id"]),
                artifact_type=str(row["artifact_type"]),
                checksum=str(row["checksum"]),
                capture_id=str(row["capture_id"]),
            )
            for row in artifact_rows
        )
        action_logs = tuple(
            EvidenceActionLogItem(
                action_execution_id=str(row["action_execution_id"]),
                artifact_id=str(row["artifact_id"]),
                checksum=str(row["checksum"]),
                capture_id=str(row["capture_id"]),
            )
            for row in action_log_rows
        )
        # 去重 subject+code
        unique: list[EvidenceGapItem] = []
        seen: set[tuple[str, str]] = set()
        for item in gaps:
            key = (item.code.value, item.subject_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return EvidenceInventory(
            complete=not unique,
            capture_ids=capture_ids,
            manifest_ids=manifest_ids,
            gaps=tuple(unique),
            page_timeline=page_timeline,
            capture_lineage=capture_lineage,
            artifacts=artifacts,
            action_logs=action_logs,
        )

    async def supersede_unstarted_exploration_works(
        self, *, task_id: str, session_id: str
    ) -> int:
        """截止时停止未领取的后继 Exploration Work，不算在途重试。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                UPDATE qc_v2_work_item
                SET
                    status = 'superseded',
                    finished_at = now(),
                    row_version = row_version + 1,
                    last_error_code = 'exploration_cutoff',
                    last_error_message = '探索截止：停止领取新 Exploration Work',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND work_type = 'exploration'
                  AND status IN ('pending', 'retry_wait')
                RETURNING work_id
                """,
                task_id,
                session_id,
            )
        return len(rows)

    async def record_terminal_observation(
        self,
        *,
        task_id: str,
        session_id: str,
        role: str,
    ) -> str | None:
        """把最后一帧复制为收口 Role Capture，不发新命令、不写 BundleReady。"""
        if role not in TERMINAL_CAPTURE_ROLES:
            raise ValueError("收口观察 role 不受支持")
        pool = await self._get_pool()
        new_capture_id = str(uuid4())
        async with pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchval(
                """
                SELECT capture.id::text
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                  AND capture.capture_role = $3
                ORDER BY capture.created_at DESC, capture.id DESC
                LIMIT 1
                """,
                task_id,
                session_id,
                role,
            )
            if existing is not None:
                return str(existing)
            parent = await connection.fetchrow(
                """
                SELECT
                    capture.id,
                    capture.agent_run_id,
                    capture.surface_kind,
                    capture.metadata
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.capture_status = 'captured'
                  AND capture.capture_role <> ALL($3::text[])
                ORDER BY capture.created_at DESC, capture.id DESC
                LIMIT 1
                """,
                task_id,
                session_id,
                list(TERMINAL_CAPTURE_ROLES),
            )
            if parent is None:
                return None
            inserted = await connection.fetchval(
                """
                INSERT INTO qc_capture (
                    id, task_id, agent_run_id, surface_kind,
                    surface_session_id, parent_capture_id,
                    capture_role, capture_status, captured_at, metadata
                )
                SELECT
                    $1::uuid, task.id, $3::uuid, parent.surface_kind,
                    session.session_id, parent.id, $6, 'captured', now(),
                    COALESCE(parent.metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            'state_type', $6,
                            'terminal_observation', true
                        )
                FROM qc_task AS task
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                   AND session.session_id = $5
                   AND session.status IN ('active', 'draining')
                INNER JOIN qc_capture AS parent
                    ON parent.id = $4::uuid
                   AND parent.task_id = task.id
                WHERE task.id = $2::uuid
                  AND task.target_platform = 'app'
                  AND task.status = 'running'
                RETURNING id
                """,
                new_capture_id,
                task_id,
                parent["agent_run_id"],
                parent["id"],
                session_id,
                role,
            )
            if inserted is None:
                return None
            artifacts = await connection.fetch(
                """
                SELECT
                    artifact_type, source, storage_path, content_type,
                    checksum, size_bytes, metadata
                FROM qc_evidence_artifact
                WHERE page_capture_id = $1::uuid
                  AND source = 'app_runtime'
                """,
                parent["id"],
            )
            for row in artifacts:
                await connection.execute(
                    """
                    INSERT INTO qc_evidence_artifact (
                        id, task_id, page_capture_id, artifact_type, source,
                        storage_path, content_type, checksum, size_bytes, metadata
                    )
                    VALUES (
                        $1::uuid, $2::uuid, $3::uuid, $4, $5,
                        $6, $7, $8, $9, $10::jsonb
                    )
                    """,
                    str(uuid4()),
                    task_id,
                    new_capture_id,
                    row["artifact_type"],
                    row["source"],
                    row["storage_path"],
                    row["content_type"],
                    row["checksum"],
                    row["size_bytes"],
                    row["metadata"],
                )
            app_row = await connection.fetchrow(
                """
                SELECT
                    screenshot_artifact_id, package_name, activity,
                    width, height, orientation, image_signature, device_snapshot
                FROM qc_app_capture
                WHERE capture_id = $1::uuid
                """,
                parent["id"],
            )
            if app_row is not None:
                screenshot_id = await connection.fetchval(
                    """
                    SELECT id
                    FROM qc_evidence_artifact
                    WHERE page_capture_id = $1::uuid
                      AND artifact_type = 'screenshot'
                    ORDER BY id
                    LIMIT 1
                    """,
                    new_capture_id,
                )
                if screenshot_id is not None:
                    await connection.execute(
                        """
                        INSERT INTO qc_app_capture (
                            capture_id, screenshot_artifact_id,
                            package_name, activity, width, height,
                            orientation, image_signature, device_snapshot
                        )
                        VALUES (
                            $1::uuid, $2::uuid, $3, $4, $5, $6,
                            $7, $8, $9::jsonb
                        )
                        """,
                        new_capture_id,
                        screenshot_id,
                        app_row["package_name"],
                        app_row["activity"],
                        app_row["width"],
                        app_row["height"],
                        app_row["orientation"],
                        app_row["image_signature"],
                        app_row["device_snapshot"],
                    )
        return new_capture_id

    async def retry_original_writes(self, *, task_id: str, session_id: str) -> None:
        """重试已落库 Capture 的原 Manifest 晋级，禁止新开 Probe／Inspection。"""
        from types import SimpleNamespace

        from services.api.app.application.app_evidence_manifest import (
            promote_app_capture_manifest_pass,
        )
        from services.api.app.infrastructure.db.evidence_manifest_repository import (
            PostgresEvidenceManifestRepository,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            pending = await connection.fetch(
                """
                SELECT DISTINCT ON (manifest.capture_id)
                    manifest.capture_id::text AS capture_id,
                    manifest.manifest_version
                FROM qc_evidence_manifest AS manifest
                WHERE manifest.task_id = $1::uuid
                  AND manifest.session_id = $2
                  AND manifest.quality_status = 'pending'
                ORDER BY manifest.capture_id, manifest.manifest_version DESC
                """,
                task_id,
                session_id,
            )
        store = PostgresEvidenceManifestRepository(
            database_url=self._database_url,
            pool=self._pool,
        )
        for row in pending:
            capture_id = str(row["capture_id"])
            artifacts = await self._artifacts_for_capture(capture_id=capture_id)
            if not artifacts:
                continue
            persisted = SimpleNamespace(
                capture=SimpleNamespace(
                    task_id=task_id,
                    session_id=session_id,
                    capture_id=capture_id,
                    role="bootstrap",
                )
            )
            await promote_app_capture_manifest_pass(
                persisted=persisted,  # type: ignore[arg-type]
                artifacts=artifacts,
                repository=store,
                previous_manifest_version=int(row["manifest_version"]),
            )

    async def fail_pending_manifests(self, *, task_id: str, session_id: str) -> None:
        """将仍 pending 的 Manifest 固化为 fail，禁止永久 pending。"""
        from types import SimpleNamespace

        from services.api.app.application.app_evidence_manifest import (
            fail_app_capture_manifest,
        )
        from services.api.app.infrastructure.db.evidence_manifest_repository import (
            PostgresEvidenceManifestRepository,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            pending = await connection.fetch(
                """
                SELECT DISTINCT ON (manifest.capture_id)
                    manifest.capture_id::text AS capture_id,
                    manifest.manifest_version
                FROM qc_evidence_manifest AS manifest
                WHERE manifest.task_id = $1::uuid
                  AND manifest.session_id = $2
                  AND manifest.quality_status = 'pending'
                ORDER BY manifest.capture_id, manifest.manifest_version DESC
                """,
                task_id,
                session_id,
            )
        store = PostgresEvidenceManifestRepository(
            database_url=self._database_url,
            pool=self._pool,
        )
        for row in pending:
            capture_id = str(row["capture_id"])
            artifacts = await self._artifacts_for_capture(capture_id=capture_id)
            if not artifacts:
                continue
            persisted = SimpleNamespace(
                capture=SimpleNamespace(
                    task_id=task_id,
                    session_id=session_id,
                    capture_id=capture_id,
                    role="bootstrap",
                )
            )
            await fail_app_capture_manifest(
                persisted=persisted,  # type: ignore[arg-type]
                artifacts=artifacts,
                repository=store,
                previous_manifest_version=int(row["manifest_version"]),
                quality_reason="write_timeout",
            )

    async def _artifacts_for_capture(self, *, capture_id: str) -> tuple[Any, ...]:
        from types import SimpleNamespace

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    id::text AS artifact_id,
                    artifact_type,
                    storage_path,
                    checksum,
                    size_bytes,
                    content_type
                FROM qc_evidence_artifact
                WHERE page_capture_id = $1::uuid
                  AND source = 'app_runtime'
                ORDER BY artifact_type, id
                """,
                capture_id,
            )
        return tuple(
            SimpleNamespace(
                artifact_id=str(row["artifact_id"]),
                artifact_type=str(row["artifact_type"]),
                storage_path=str(row["storage_path"]),
                checksum=str(row["checksum"]),
                size_bytes=int(row["size_bytes"]),
                content_type=str(row["content_type"]),
            )
            for row in rows
        )

    async def seal(
        self,
        *,
        index: EvidenceIndexReference,
        agent_run_id: str | None = None,
    ) -> EvidenceIndexReference:
        if not isinstance(index, EvidenceIndexReference):
            raise TypeError("index 必须为 EvidenceIndexReference")
        pool = await self._get_pool()
        payload = index.to_dict()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{index.task_id}",
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_evidence_index AS existing (
                    index_id, task_id, session_id, status,
                    protocol_version, index_hash, payload_hash,
                    index_payload, sealed_at
                )
                SELECT
                    $1, task.id, session.session_id, $4,
                    $5, $6, $7, $8::jsonb, $9
                FROM qc_task AS task
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                   AND session.session_id = $3
                WHERE task.id = $2::uuid
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                ON CONFLICT (task_id, session_id) DO UPDATE
                SET index_id = existing.index_id
                WHERE existing.index_hash = EXCLUDED.index_hash
                  AND existing.payload_hash = EXCLUDED.payload_hash
                RETURNING existing.index_payload, (xmax = 0) AS created
                """,
                index.index_id,
                index.task_id,
                index.session_id,
                index.status.value,
                index.protocol_version,
                index.index_hash,
                stable_protocol_hash(payload),
                _json_dumps(payload),
                index.sealed_at,
            )
            if row is None:
                raise EvidenceIndexPersistenceError(
                    "Evidence Index 已被其他内容占用或任务/会话归属无效"
                )
            stored = EvidenceIndexReference.from_dict(
                _as_mapping(row["index_payload"], "index_payload")
            )
            if stored.index_id != index.index_id:
                raise EvidenceIndexPersistenceError("Evidence Index 幂等回读身份不一致")
            if agent_run_id:
                await connection.execute(
                    """
                    UPDATE qc_agent_run
                    SET
                        output_context = COALESCE(output_context, '{}'::jsonb)
                            || $2::jsonb,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND task_id = $3::uuid
                    """,
                    agent_run_id,
                    _json_dumps(
                        {
                            "evidence_index_id": stored.index_id,
                            "exploration_status": stored.status.value,
                            "evidence_gap": [
                                item.detail for item in stored.evidence_gap
                            ],
                            "page_timeline": list(stored.page_timeline),
                        }
                    ),
                    index.task_id,
                )
            return stored

    async def get_latest_for_task(self, *, task_id: str) -> EvidenceIndexReference | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT index_payload
                FROM qc_evidence_index
                WHERE task_id = $1::uuid
                ORDER BY sealed_at DESC
                LIMIT 1
                """,
                task_id,
            )
        if payload is None:
            return None
        return EvidenceIndexReference.from_dict(_as_mapping(payload, "index_payload"))

    async def get(
        self, *, task_id: str, session_id: str
    ) -> EvidenceIndexReference | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT index_payload
                FROM qc_evidence_index
                WHERE task_id = $1::uuid
                  AND session_id = $2
                """,
                task_id,
                session_id,
            )
        if payload is None:
            return None
        return EvidenceIndexReference.from_dict(_as_mapping(payload, "index_payload"))

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceIndexPersistenceError(f"{field_name} 必须为对象")
    return value


_SHA256_CHECKSUM = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_UI_TREE_MIME = frozenset(
    {
        "application/xml",
        "text/xml",
        "application/json",
        "text/json",
        "text/plain",
    }
)


def _corrupt_artifact_reason(row: Mapping[str, Any]) -> str | None:
    checksum = str(row.get("checksum") or "").strip()
    if not checksum or _SHA256_CHECKSUM.fullmatch(checksum) is None:
        return f"制品 checksum 无效：{checksum or 'empty'}"
    try:
        size_bytes = int(row.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if size_bytes <= 0:
        return f"制品 size_bytes 无效：{row.get('size_bytes')}"
    artifact_type = str(row.get("artifact_type") or "").strip().casefold()
    mime = str(row.get("content_type") or "").split(";", 1)[0].strip().casefold()
    if artifact_type == "screenshot" and mime != "image/png":
        return f"截图 MIME 不是 image/png：{mime or 'empty'}"
    if artifact_type in {"ui_tree", "ui_snapshot"} and mime not in _UI_TREE_MIME:
        return f"UI Tree MIME 不受支持：{mime or 'empty'}"
    return None


def _page_timeline_entry(row: Mapping[str, Any]) -> str:
    observed_at = row["created_at"]
    stamp = observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at)
    page_type = str(row["visual_page_type"] or "unknown").strip() or "unknown"
    summary = str(row["visual_summary"] or "").strip()
    capture_id = str(row["capture_id"])
    if summary:
        return f"{stamp}|{capture_id}|{page_type}|{summary}"
    return f"{stamp}|{capture_id}|{page_type}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
