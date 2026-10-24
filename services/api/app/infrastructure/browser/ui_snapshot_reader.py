"""Interaction Runtime 使用的可信 UI Snapshot 读取实现。"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import cv2
import numpy as np

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
)
from services.api.app.application.page_capture_persistence import EvidenceArtifactStore
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store


class PostgresTrustedUiSnapshotReader:
    """从 PostgreSQL 解析归属，再从证据存储读取并校验 UI Snapshot。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        artifact_store: EvidenceArtifactStore | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._artifact_store = artifact_store or build_evidence_artifact_store(
            get_settings().evidence_storage
        )

    async def read_ui_snapshot(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> Mapping[str, Any]:
        task_id = _require_database_uuid(
            task_id,
            message="UI Snapshot 数据库身份无效",
            error_code="UI_SNAPSHOT_SOURCE_STATE_STALE",
        )
        page_capture_id = _require_database_uuid(
            page_capture_id,
            message="UI Snapshot 数据库身份无效",
            error_code="UI_SNAPSHOT_SOURCE_STATE_STALE",
        )
        try:
            pool = self._pool or await get_postgres_pool(self._database_url)
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT
                        evidence.storage_path,
                        evidence.checksum,
                        evidence.size_bytes,
                        (
                            SELECT visual_evidence.checksum
                            FROM qc_evidence_artifact AS visual_evidence
                            WHERE visual_evidence.task_id = evidence.task_id
                              AND visual_evidence.page_capture_id = evidence.page_capture_id
                              AND visual_evidence.artifact_type = 'screenshot'
                              AND visual_evidence.content_type = 'image/png'
                              AND visual_evidence.source IN (
                                  'baseline',
                                  'rule_supplement'
                              )
                            ORDER BY
                                CASE WHEN visual_evidence.metadata->>'purpose' = 'h5_video_primary'
                                     THEN 0 ELSE 1 END,
                                visual_evidence.created_at ASC,
                                visual_evidence.id ASC
                            LIMIT 1
                        ) AS visual_checksum
                    FROM qc_evidence_artifact AS evidence
                    INNER JOIN qc_capture AS capture
                      ON capture.id = evidence.page_capture_id
                     AND capture.task_id = evidence.task_id
                    WHERE evidence.task_id = $1::uuid
                      AND evidence.page_capture_id = $2::uuid
                      AND evidence.artifact_type = 'ui_snapshot'
                      AND evidence.source IN ('baseline', 'rule_supplement')
                      AND capture.capture_status = 'captured'
                    ORDER BY evidence.created_at DESC, evidence.id DESC
                    LIMIT 1
                    """,
                    task_id,
                    page_capture_id,
                )
        except Exception:
            raise SemanticTargetResolutionError(
                "UI Snapshot 暂不可用",
                error_code="UI_SNAPSHOT_UNAVAILABLE",
                retryable=True,
            ) from None
        if row is None:
            raise SemanticTargetResolutionError(
                "当前 Capture 缺少可信 UI Snapshot",
                error_code="UI_SNAPSHOT_MISSING",
                retryable=True,
            )

        try:
            storage_path = _require_owned_evidence_storage_path(
                row["storage_path"],
                task_id=task_id,
                page_capture_id=page_capture_id,
                artifact_id=None,
                extension="json",
                message="UI Snapshot 存储归属不匹配",
                error_code="UI_SNAPSHOT_OWNERSHIP_MISMATCH",
            )
        except (KeyError, TypeError):
            raise SemanticTargetResolutionError(
                "UI Snapshot 暂不可用",
                error_code="UI_SNAPSHOT_UNAVAILABLE",
                retryable=True,
            ) from None
        try:
            payload = await self._artifact_store.read(storage_path)
        except Exception:
            raise SemanticTargetResolutionError(
                "UI Snapshot 暂不可用",
                error_code="UI_SNAPSHOT_UNAVAILABLE",
                retryable=True,
            ) from None
        expected_size = row["size_bytes"]
        if expected_size is not None and len(payload) != int(expected_size):
            raise SemanticTargetResolutionError(
                "UI Snapshot 大小校验失败",
                error_code="UI_SNAPSHOT_SIZE_MISMATCH",
            )
        checksum = str(row["checksum"] or "")
        digest = hashlib.sha256(payload).hexdigest()
        if checksum not in {digest, f"sha256:{digest}"}:
            raise SemanticTargetResolutionError(
                "UI Snapshot 摘要校验失败",
                error_code="UI_SNAPSHOT_CHECKSUM_MISMATCH",
            )
        try:
            snapshot = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SemanticTargetResolutionError(
                "UI Snapshot 不是有效 JSON",
                error_code="UI_SNAPSHOT_INVALID",
            ) from exc
        if not isinstance(snapshot, Mapping):
            raise SemanticTargetResolutionError(
                "UI Snapshot 根节点必须为对象",
                error_code="UI_SNAPSHOT_INVALID",
            )
        visual_checksum = str(row["visual_checksum"] or "").strip()
        trusted_snapshot = dict(snapshot)
        trusted_snapshot.pop("_capture_visual_checksum", None)
        if visual_checksum:
            trusted_snapshot["_capture_visual_checksum"] = visual_checksum
        return trusted_snapshot

    async def read_checkpoint_observation(self, *, task_id: str, page_capture_id: str):
        """精确读取采样附件，并核对每个样本的实际已存 PNG，不信自报摘要。"""
        task_id = _require_database_uuid(
            task_id, message="采样 Task 身份无效", error_code="CHECKPOINT_SAMPLE_SCOPE_INVALID"
        )
        page_capture_id = _require_database_uuid(
            page_capture_id,
            message="采样 Capture 身份无效",
            error_code="CHECKPOINT_SAMPLE_SCOPE_INVALID",
        )
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT evidence.* FROM qc_evidence_artifact AS evidence "
                "JOIN qc_capture AS capture "
                "ON capture.id=evidence.page_capture_id AND capture.task_id=evidence.task_id "
                "WHERE evidence.task_id=$1::uuid AND capture.id=$2::uuid "
                "AND capture.capture_status='captured' "
                "AND evidence.source IN ('baseline','rule_supplement') "
                "AND evidence.lifecycle_status IN ('active','archived') "
                "AND evidence.artifact_type IN ('checkpoint_observation','screenshot')",
                task_id,
                page_capture_id,
            )
        samples = [row for row in rows if row["artifact_type"] == "checkpoint_observation"]
        if not samples:
            return None
        if len(samples) != 1:
            raise ValueError("检查点采样附件身份不唯一")
        row = samples[0]
        path = _require_owned_evidence_storage_path(
            row["storage_path"],
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=str(row["id"]),
            extension="json",
            message="采样存储归属错误",
            error_code="CHECKPOINT_SAMPLE_SCOPE_INVALID",
        )
        content = await self._artifact_store.read(path)
        if (
            type(row["size_bytes"]) is not int
            or row["size_bytes"] > 512 * 1024
            or len(content) != row["size_bytes"]
            or row["content_type"] != "application/json"
            or str(row["checksum"]).removeprefix("sha256:") != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("检查点采样制品完整性失败")
        payload = json.loads(content.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "checkpoint-observation.v1"
            or not isinstance(payload.get("samples"), list)
            or len(payload["samples"]) != 2
        ):
            raise ValueError("检查点采样协议错误")
        pngs = [item for item in rows if item["artifact_type"] == "screenshot"]
        required = [item["screenshot_sha256"] for item in payload["samples"]]
        used = set()
        for digest in required:
            found = [
                item
                for item in pngs
                if str(item["checksum"]).removeprefix("sha256:") == digest
                and str(item["id"]) not in used
            ]
            if not found:
                raise ValueError("检查点样本没有对应的实际 PNG")
            used.add(str(found[0]["id"]))
            actual = await self.read_visual_evidence_image(
                task_id=task_id,
                page_capture_id=page_capture_id,
                artifact_id=str(found[0]["id"]),
                expected_sha256=digest,
            )
            if hashlib.sha256(actual).hexdigest() != digest:
                raise ValueError("检查点样本 PNG 摘要不一致")
            from services.api.app.infrastructure.browser.h5_player_frames import video_region_digest

            sample = payload["samples"][len(used) - 1]
            if sample.get("video_region_sha256") != video_region_digest(actual, sample["snapshot"]):
                raise ValueError("检查点视频区域摘要不对应实际 PNG")
        return payload

    async def read_media_state(
        self, *, task_id: str, page_capture_id: str
    ) -> Mapping[str, Any] | None:
        """按 Capture 归属和摘要读取原生媒体事实，缺项保持未知。"""

        task_id = _require_database_uuid(
            task_id, message="媒体状态 Task 身份无效",
            error_code="WEB_PLAYER_MEDIA_STATE_IDENTITY_INVALID",
        )
        page_capture_id = _require_database_uuid(
            page_capture_id, message="媒体状态 Capture 身份无效",
            error_code="WEB_PLAYER_MEDIA_STATE_IDENTITY_INVALID",
        )
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT evidence.storage_path, evidence.checksum, evidence.size_bytes,
                       evidence.content_type
                FROM qc_evidence_artifact AS evidence
                INNER JOIN qc_capture AS capture
                  ON capture.id = evidence.page_capture_id
                 AND capture.task_id = evidence.task_id
                WHERE evidence.task_id = $1::uuid
                  AND evidence.page_capture_id = $2::uuid
                  AND evidence.artifact_type = 'media_state'
                  AND evidence.source IN ('baseline', 'rule_supplement')
                  AND evidence.lifecycle_status IN ('active', 'archived')
                  AND capture.capture_status = 'captured'
                ORDER BY evidence.created_at DESC, evidence.id DESC
                LIMIT 1
                """,
                task_id, page_capture_id,
            )
        if row is None:
            return None
        path = _require_owned_evidence_storage_path(
            row["storage_path"], task_id=task_id,
            page_capture_id=page_capture_id, artifact_id=None, extension="json",
            message="媒体状态存储归属不匹配",
            error_code="WEB_PLAYER_MEDIA_STATE_OWNERSHIP_MISMATCH",
        )
        if row["content_type"] != "application/json":
            raise SemanticTargetResolutionError(
                "媒体状态附件类型错误", error_code="WEB_PLAYER_MEDIA_STATE_CONTENT_TYPE_INVALID"
            )
        payload = await self._artifact_store.read(path)
        expected_size = row["size_bytes"]
        if (
            type(expected_size) is not int or expected_size > 512 * 1024
            or len(payload) != expected_size
        ):
            raise SemanticTargetResolutionError(
                "媒体状态大小校验失败", error_code="WEB_PLAYER_MEDIA_STATE_SIZE_MISMATCH"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if row["checksum"] not in {digest, f"sha256:{digest}"}:
            raise SemanticTargetResolutionError(
                "媒体状态摘要校验失败", error_code="WEB_PLAYER_MEDIA_STATE_CHECKSUM_MISMATCH"
            )
        try:
            state = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SemanticTargetResolutionError(
                "媒体状态 JSON 无效", error_code="WEB_PLAYER_MEDIA_STATE_INVALID"
            ) from exc
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != "media-state.v1"
            or not isinstance(state.get("items"), list)
        ):
            raise SemanticTargetResolutionError(
                "媒体状态结构无效", error_code="WEB_PLAYER_MEDIA_STATE_INVALID"
            )
        return state

    async def read_web_before_action_binding(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> Mapping[str, Any]:
        """读取 Web 动作前 Capture、动作账本与当前 Session 的正式绑定。"""

        task_id = _require_database_uuid(
            task_id,
            message="Web 动作前 Capture 数据库身份无效",
            error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
        )
        page_capture_id = _require_database_uuid(
            page_capture_id,
            message="Web 动作前 Capture 数据库身份无效",
            error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
        )
        try:
            pool = self._pool or await get_postgres_pool(self._database_url)
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT
                        before_capture.id::text AS capture_id,
                        before_capture.task_id::text AS task_id,
                        before_capture.agent_run_id::text AS agent_run_id,
                        before_capture.surface_kind,
                        before_capture.surface_session_id AS session_id,
                        before_capture.parent_capture_id::text AS parent_capture_id,
                        before_capture.action_execution_id,
                        before_capture.capture_role,
                        before_capture.capture_status,
                        before_capture.session_command_sequence,
                        -- URL 已镜像进公共 Capture metadata；运行时只读公共采集表。
                        before_capture.metadata->>'url' AS final_url,
                        before_capture.metadata->>'requested_url' AS capture_requested_url,
                        parent_capture.agent_run_id::text AS parent_agent_run_id,
                        parent_capture.surface_kind AS parent_surface_kind,
                        parent_capture.surface_session_id AS parent_session_id,
                        parent_capture.capture_status AS parent_capture_status,
                        parent_capture.metadata->>'url' AS parent_final_url,
                        action.task_id::text AS action_task_id,
                        action.session_id AS action_session_id,
                        action.requested_source_capture_id::text
                            AS action_requested_source_capture_id,
                        action.source_capture_id::text AS action_source_capture_id,
                        action.command_sequence AS action_command_sequence,
                        action.finalization_epoch AS action_finalization_epoch,
                        action.status AS action_status,
                        action.intent_payload AS action_intent,
                        action.intent_payload #>> '{parameters,destination_url}'
                            AS action_requested_url,
                        session.current_capture_id::text AS surface_current_capture_id,
                        session.command_sequence AS surface_command_sequence,
                        session.finalization_epoch AS surface_finalization_epoch,
                        session.status AS surface_status
                    FROM qc_capture AS before_capture
                    INNER JOIN qc_capture AS parent_capture
                      ON parent_capture.id = before_capture.parent_capture_id
                     AND parent_capture.task_id = before_capture.task_id
                    INNER JOIN qc_action_execution AS action
                      ON action.action_execution_id = before_capture.action_execution_id
                     AND action.task_id = before_capture.task_id
                     AND action.session_id = before_capture.surface_session_id
                    INNER JOIN qc_surface_session AS session
                      ON session.task_id = before_capture.task_id
                     AND session.session_id = before_capture.surface_session_id
                    WHERE before_capture.task_id = $1::uuid
                      AND before_capture.id = $2::uuid
                    LIMIT 1
                    """,
                    task_id,
                    page_capture_id,
                )
        except SemanticTargetResolutionError:
            raise
        except Exception:
            raise SemanticTargetResolutionError(
                "Web 动作前 Capture 暂不可用",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
                retryable=True,
            ) from None
        if row is None:
            raise SemanticTargetResolutionError(
                "Web 动作前 Capture 绑定不存在",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
                retryable=True,
            )
        binding = dict(row)
        action_intent = binding.get("action_intent")
        if isinstance(action_intent, str):
            try:
                decoded = json.loads(action_intent)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, Mapping):
                binding["action_intent"] = decoded
        return binding

    async def read_visual_evidence_checksum(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        frame_index: int | None,
    ) -> str:
        """按候选绑定的 artifact/frame 读取并校验实际截图摘要。"""
        if frame_index is not None:
            raise SemanticTargetResolutionError(
                "视觉坐标入口只接受独立 PNG 截图证据",
                error_code="VISUAL_SOURCE_FRAME_INVALID",
            )
        payload = await self._read_visual_evidence_payload(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=artifact_id,
        )
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    async def read_visual_evidence_image(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        expected_sha256: str,
    ) -> bytes:
        """返回归属与两层摘要均通过校验的独立 PNG。"""
        payload = await self._read_visual_evidence_payload(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=artifact_id,
        )
        payload_digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 not in {payload_digest, f"sha256:{payload_digest}"}:
            raise SemanticTargetResolutionError(
                "视觉导航来源截图已偏离密封能力",
                error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
                retryable=True,
            )
        return payload

    async def read_visual_evidence_region_checksum(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str | None,
        left_ratio: float,
        top_ratio: float,
        right_ratio: float,
        bottom_ratio: float,
    ) -> str:
        """校验 PNG 证据后，对目标矩形的解码像素生成稳定摘要。"""
        ratios = (left_ratio, top_ratio, right_ratio, bottom_ratio)
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 1
                for value in ratios
            )
            or left_ratio >= right_ratio
            or top_ratio >= bottom_ratio
        ):
            raise SemanticTargetResolutionError(
                "视觉坐标目标区域无效",
                error_code="VISUAL_BOUNDS_INVALID",
            )
        payload = await self._read_visual_evidence_payload(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=artifact_id,
        )
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None or image.ndim not in {2, 3}:
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据无法解码",
                error_code="VISUAL_SOURCE_FRAME_INVALID",
            )
        height, width = image.shape[:2]
        left = max(0, min(width, math.floor(width * left_ratio)))
        top = max(0, min(height, math.floor(height * top_ratio)))
        right = max(0, min(width, math.ceil(width * right_ratio)))
        bottom = max(0, min(height, math.ceil(height * bottom_ratio)))
        if left >= right or top >= bottom:
            raise SemanticTargetResolutionError(
                "视觉坐标目标区域没有有效像素",
                error_code="VISUAL_BOUNDS_INVALID",
            )
        region = np.ascontiguousarray(image[top:bottom, left:right])
        channels = region.shape[2] if region.ndim == 3 else 1
        digest = hashlib.sha256()
        digest.update(
            f"{region.shape[1]}x{region.shape[0]}x{channels}:{region.dtype}\0".encode("ascii")
        )
        digest.update(region.tobytes())
        return f"sha256:{digest.hexdigest()}"

    async def _read_visual_evidence_payload(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str | None,
    ) -> bytes:
        """读取已归属当前 Capture 的独立 PNG，并校验存储摘要。"""
        task_id = _require_database_uuid(
            task_id,
            message="视觉导航来源数据库身份无效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )
        page_capture_id = _require_database_uuid(
            page_capture_id,
            message="视觉导航来源数据库身份无效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )
        if artifact_id is not None:
            artifact_id = _require_database_uuid(
                artifact_id,
                message="视觉导航来源数据库身份无效",
                error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            )
        row = await self._fetch_visual_evidence_row(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=artifact_id,
        )
        if row is None:
            raise SemanticTargetResolutionError(
                "视觉坐标绑定的截图证据不存在",
                error_code="VISUAL_SOURCE_ARTIFACT_MISSING",
                retryable=True,
            )

        try:
            storage_path = _require_owned_evidence_storage_path(
                row["storage_path"],
                task_id=task_id,
                page_capture_id=page_capture_id,
                artifact_id=artifact_id,
                extension="png",
                message="视觉坐标截图证据归属不匹配",
                error_code="VISUAL_SOURCE_ARTIFACT_OWNERSHIP_MISMATCH",
            )
            artifact_type = str(row["artifact_type"] or "").strip().casefold()
            content_type = str(row["content_type"] or "").split(";", 1)[0].strip().casefold()
        except SemanticTargetResolutionError:
            raise
        except Exception:
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据暂不可用",
                error_code="VISUAL_SOURCE_ARTIFACT_UNAVAILABLE",
                retryable=True,
            ) from None
        if artifact_type != "screenshot" or content_type != "image/png":
            raise SemanticTargetResolutionError(
                "视觉坐标入口只接受独立 PNG 截图证据",
                error_code="VISUAL_SOURCE_FRAME_INVALID",
            )
        try:
            payload = await self._artifact_store.read(storage_path)
        except Exception:
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据暂不可用",
                error_code="VISUAL_SOURCE_ARTIFACT_UNAVAILABLE",
                retryable=True,
            ) from None
        if not isinstance(payload, bytes):
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据暂不可用",
                error_code="VISUAL_SOURCE_ARTIFACT_UNAVAILABLE",
                retryable=True,
            )
        expected_size = row["size_bytes"]
        if expected_size is not None and len(payload) != int(expected_size):
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据大小校验失败",
                error_code="VISUAL_SOURCE_ARTIFACT_SIZE_MISMATCH",
            )
        stored_checksum = str(row["checksum"] or "")
        payload_digest = hashlib.sha256(payload).hexdigest()
        if stored_checksum not in {payload_digest, f"sha256:{payload_digest}"}:
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据摘要校验失败",
                error_code="VISUAL_SOURCE_ARTIFACT_CHECKSUM_MISMATCH",
            )

        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SemanticTargetResolutionError(
                "视觉坐标入口只接受独立 PNG 截图证据",
                error_code="VISUAL_SOURCE_FRAME_INVALID",
            )
        return payload

    async def _fetch_visual_evidence_row(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str | None,
    ) -> Any:
        try:
            pool = self._pool or await get_postgres_pool(self._database_url)
            async with pool.acquire() as connection:
                if artifact_id is None:
                    return await connection.fetchrow(
                        """
                        SELECT
                            evidence.storage_path,
                            evidence.checksum,
                            evidence.size_bytes,
                            evidence.artifact_type,
                            evidence.content_type
                        FROM qc_evidence_artifact AS evidence
                        INNER JOIN qc_capture AS capture
                          ON capture.id = evidence.page_capture_id
                         AND capture.task_id = evidence.task_id
                        WHERE evidence.task_id = $1::uuid
                          AND evidence.page_capture_id = $2::uuid
                          AND evidence.artifact_type = 'screenshot'
                          AND evidence.content_type = 'image/png'
                          AND evidence.source IN ('baseline', 'rule_supplement')
                          AND capture.capture_status = 'captured'
                        ORDER BY evidence.created_at ASC, evidence.id ASC
                        LIMIT 1
                        """,
                        task_id,
                        page_capture_id,
                    )
                return await connection.fetchrow(
                    """
                    SELECT
                        evidence.storage_path,
                        evidence.checksum,
                        evidence.size_bytes,
                        evidence.artifact_type,
                        evidence.content_type
                    FROM qc_evidence_artifact AS evidence
                    INNER JOIN qc_capture AS capture
                      ON capture.id = evidence.page_capture_id
                     AND capture.task_id = evidence.task_id
                    WHERE evidence.task_id = $1::uuid
                      AND evidence.page_capture_id = $2::uuid
                      AND evidence.id = $3::uuid
                      AND evidence.artifact_type = 'screenshot'
                      AND evidence.content_type = 'image/png'
                      AND evidence.source IN ('baseline', 'rule_supplement')
                      AND capture.capture_status = 'captured'
                    LIMIT 1
                    """,
                    task_id,
                    page_capture_id,
                    artifact_id,
                )
        except Exception:
            raise SemanticTargetResolutionError(
                "视觉坐标截图证据暂不可用",
                error_code="VISUAL_SOURCE_ARTIFACT_UNAVAILABLE",
                retryable=True,
            ) from None


def _require_database_uuid(
    value: object,
    *,
    message: str,
    error_code: str,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SemanticTargetResolutionError(
            message,
            error_code=error_code,
            retryable=True,
        ) from None
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise SemanticTargetResolutionError(
            message,
            error_code=error_code,
            retryable=True,
        ) from None


def _require_owned_evidence_storage_path(
    value: object,
    *,
    task_id: str,
    page_capture_id: str,
    artifact_id: str | None,
    extension: str,
    message: str,
    error_code: str,
) -> str:
    def reject() -> None:
        raise SemanticTargetResolutionError(message, error_code=error_code) from None

    if not isinstance(value, str) or not value or value != value.strip():
        reject()
    storage_path = value
    normalized_text = unicodedata.normalize("NFKC", storage_path)
    decoded_text = unquote(normalized_text)
    if normalized_text != storage_path or decoded_text != storage_path:
        reject()
    if "\\" in storage_path or "\x00" in storage_path:
        reject()
    posix_path = PurePosixPath(storage_path)
    windows_path = PureWindowsPath(storage_path)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        reject()
    raw_parts = storage_path.split("/")
    if (
        len(raw_parts) != 3
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw_parts[:2] != [task_id, page_capture_id]
        or posix_path.as_posix() != storage_path
    ):
        reject()
    filename = raw_parts[2]
    expected_filename = f"{artifact_id}.{extension}" if artifact_id else None
    if expected_filename is not None:
        if filename != expected_filename:
            reject()
        return storage_path
    suffix = f".{extension}"
    if not filename.endswith(suffix):
        reject()
    filename_id = filename[: -len(suffix)]
    try:
        if str(UUID(filename_id)) != filename_id:
            reject()
    except (AttributeError, TypeError, ValueError):
        reject()
    return storage_path
