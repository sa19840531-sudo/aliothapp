"""PostgreSQL Observation Packet 证据读取与持久化实现。"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from services.api.app.application.evidence_presentation import declared_evidence_flags
from services.api.app.application.observation_packet import (
    ObservationArtifact,
    ObservationJourneyStep,
    ObservationPacketValidationError,
    ObservationSource,
    build_app_observation_location,
    build_observation_input_fingerprint,
)
from services.api.app.application.observation_surface_guard import web_document_facts
from services.api.app.application.page_capture_persistence import EvidenceArtifactStore
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store


class PostgresObservationPacketRepository:
    """从可信证据存储组装页面源，并在数据库中保存规则级证据包。"""

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

    async def load_capture_context(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None:
        """E22：只读页面上下文与旅程骨架，**不查 `qc_evidence_artifact`**。

        返回的 `artifacts` 与各旅程步骤的 `artifacts` 恒为空，
        证据一律由清单加载（`ManifestBackedObservationRepository`）填充。
        """
        return await self.load_source(
            task_id=task_id,
            page_capture_id=page_capture_id,
            include_artifacts=False,
        )

    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        include_artifacts: bool = True,
    ) -> ObservationSource | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            capture = await connection.fetchrow(
                """
                SELECT
                    common_capture.id::text AS page_capture_id,
                    common_capture.task_id::text AS task_id,
                    common_capture.agent_run_id::text AS agent_run_id,
                    common_capture.surface_kind::text AS surface_kind,
                    common_capture.capture_role::text AS capture_role,
                    common_capture.parent_capture_id::text AS parent_capture_id,
                    common_capture.created_at::text AS captured_at,
                    task.target_platform,
                    COALESCE(
                        page_capture.page_seq,
                        common_capture.session_command_sequence,
                        0
                    ) AS page_seq,
                    COALESCE(page_capture.url, common_capture.metadata->>'url', '') AS url,
                    COALESCE(
                        page_capture.page_type,
                        common_capture.metadata->>'page_type'
                    ) AS page_type,
                    COALESCE(
                        page_capture.page_state,
                        common_capture.metadata->>'page_state'
                    ) AS page_state,
                    COALESCE(
                        page_capture.action_log,
                        common_capture.metadata->'action_log'
                    ) AS action_log,
                    common_capture.metadata,
                    app_capture.package_name AS app_package_name,
                    app_capture.activity AS app_activity,
                    app_capture.width AS app_width,
                    app_capture.height AS app_height,
                    app_capture.device_snapshot AS app_device_snapshot
                FROM qc_capture AS common_capture
                INNER JOIN qc_task AS task ON task.id = common_capture.task_id
                LEFT JOIN qc_page_capture AS page_capture
                  ON page_capture.id = common_capture.id
                LEFT JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = common_capture.id
                WHERE common_capture.id = $1::uuid
                  AND common_capture.task_id = $2::uuid
                  AND common_capture.capture_status = 'captured'
                """,
                page_capture_id,
                task_id,
            )
            if capture is None:
                return None
            visual_row = await connection.fetchrow(
                """
                SELECT visual_summary, key_elements, candidate_targets, result_hash,
                       page_state, overlay_signature, surface_facts
                FROM qc_app_visual_observation
                WHERE capture_id = $1::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                page_capture_id,
            )
            rows = (
                await connection.fetch(
                    """
                    SELECT
                        id::text AS artifact_id,
                        artifact_type,
                        storage_path,
                        content_type,
                        checksum,
                        size_bytes
                    FROM qc_evidence_artifact
                    WHERE task_id = $1::uuid
                      AND page_capture_id = $2::uuid
                      AND source IN ('baseline', 'app_runtime')
                      AND artifact_type IN (
                          'dom', 'visible_text', 'screenshot', 'ui_snapshot', 'ui_tree',
                          'har', 'network_trace', 'device_log', 'fault_artifact',
                          'console_log', 'action_trace', 'temporal_trace',
                          'page_coverage', 'ocr_text', 'media_state'
                      )
                    ORDER BY created_at, id
                    """,
                    task_id,
                    page_capture_id,
                )
                if include_artifacts
                else []
            )
            journey_rows = await connection.fetch(
                """
                WITH run_captures AS (
                    SELECT
                        common_capture.id::text AS page_capture_id,
                        COALESCE(
                            page_capture.page_seq,
                            common_capture.session_command_sequence,
                            0
                        ) AS page_seq,
                        COALESCE(page_capture.url, common_capture.metadata->>'url', '') AS url,
                        COALESCE(
                            page_capture.page_type,
                            common_capture.metadata->>'page_type'
                        ) AS page_type,
                        COALESCE(
                            page_capture.page_state,
                            common_capture.metadata->>'page_state'
                        ) AS page_state,
                        COALESCE(
                            page_capture.action_log,
                            common_capture.metadata->'action_log'
                        ) AS action_log,
                        common_capture.metadata,
                        common_capture.capture_role::text AS capture_role,
                        common_capture.parent_capture_id::text AS parent_capture_id,
                        common_capture.created_at::text AS captured_at
                    FROM qc_capture AS common_capture
                    LEFT JOIN qc_page_capture AS page_capture
                      ON page_capture.id = common_capture.id
                    WHERE common_capture.task_id = $1::uuid
                      AND common_capture.capture_status = 'captured'
                      AND COALESCE(
                          page_capture.page_seq,
                          common_capture.session_command_sequence,
                          0
                      ) <= $3
                      AND (
                          $2::uuid IS NULL
                          OR common_capture.agent_run_id = $2::uuid
                      )
                ),
                ranked AS (
                    SELECT
                        run_captures.*,
                        row_number() OVER (ORDER BY page_seq) AS head_rank,
                        row_number() OVER (ORDER BY page_seq DESC) AS tail_rank
                    FROM run_captures
                )
                SELECT
                    page_capture_id,
                    page_seq,
                    url,
                    page_type,
                    page_state,
                    action_log,
                    metadata,
                    capture_role,
                    parent_capture_id,
                    captured_at
                FROM ranked
                WHERE head_rank <= 3 OR tail_rank <= 17
                ORDER BY page_seq
                """,
                task_id,
                capture["agent_run_id"],
                capture["page_seq"],
            )
            journey_capture_ids = [
                str(row["page_capture_id"]) for row in journey_rows
            ]
            ad_screenshot_artifact_ids = _app_ad_screenshot_artifact_ids(
                capture["action_log"]
            )
            journey_artifact_rows = (
                await connection.fetch(
                    """
                    SELECT
                        page_capture_id::text AS page_capture_id,
                        id::text AS artifact_id,
                        artifact_type,
                        storage_path,
                        content_type,
                        checksum,
                        size_bytes
                    FROM qc_evidence_artifact
                    WHERE task_id = $1::uuid
                      AND page_capture_id = ANY($2::uuid[])
                      AND source IN ('baseline', 'app_runtime')
                      AND (
                          artifact_type IN (
                              'visible_text', 'ui_snapshot', 'ui_tree', 'action_trace',
                              'media_state', 'temporal_trace', 'page_coverage',
                              'device_log', 'network_trace', 'fault_artifact'
                          )
                          OR (
                              artifact_type = 'screenshot'
                              AND id = ANY($3::uuid[])
                          )
                      )
                    ORDER BY page_capture_id, created_at, id
                    """,
                    task_id,
                    journey_capture_ids,
                    list(ad_screenshot_artifact_ids),
                )
                if journey_capture_ids and include_artifacts
                else []
            )

        artifacts: list[ObservationArtifact] = []
        for row in rows:
            storage_path = str(row["storage_path"])
            _validate_storage_path(
                storage_path,
                task_id=task_id,
                page_capture_id=page_capture_id,
            )
            payload = await self._artifact_store.read(storage_path)
            _validate_payload(
                payload,
                checksum=str(row["checksum"] or ""),
                size_bytes=(
                    int(row["size_bytes"])
                    if row["size_bytes"] is not None
                    else None
                ),
            )
            artifacts.append(
                ObservationArtifact(
                    artifact_id=str(row["artifact_id"]),
                    artifact_type=str(row["artifact_type"]),
                    content_type=str(row["content_type"] or ""),
                    payload=payload,
                )
            )

        app_capture_facts = _app_capture_facts(capture)
        page_url = str(capture["url"] or "").strip()
        if not page_url and str(capture["surface_kind"] or "").casefold() in {
            "android",
            "ios",
        }:
            page_url = build_app_observation_location(
                package_name=app_capture_facts.get("package_name"),
                activity=app_capture_facts.get("activity"),
            ) or ""
        metadata = _mapping(capture["metadata"])
        journey_artifacts: dict[str, list[ObservationArtifact]] = {}
        for row in journey_artifact_rows:
            journey_page_capture_id = str(row["page_capture_id"])
            storage_path = str(row["storage_path"])
            _validate_storage_path(
                storage_path,
                task_id=task_id,
                page_capture_id=journey_page_capture_id,
            )
            payload = await self._artifact_store.read(storage_path)
            _validate_payload(
                payload,
                checksum=str(row["checksum"] or ""),
                size_bytes=(
                    int(row["size_bytes"])
                    if row["size_bytes"] is not None
                    else None
                ),
            )
            journey_artifacts.setdefault(journey_page_capture_id, []).append(
                ObservationArtifact(
                    artifact_id=str(row["artifact_id"]),
                    artifact_type=str(row["artifact_type"]),
                    content_type=str(row["content_type"] or ""),
                    payload=payload,
                )
            )
        journey_steps = tuple(
            ObservationJourneyStep(
                page_capture_id=str(row["page_capture_id"]),
                page_seq=int(row["page_seq"]),
                page_url=str(row["url"]),
                page_title=(
                    str(step_metadata["title"])
                    if step_metadata.get("title") is not None
                    else None
                ),
                page_type=(
                    str(row["page_type"])
                    if row["page_type"] is not None
                    else None
                ),
                page_state=(
                    str(row["page_state"])
                    if row["page_state"] is not None
                    else None
                ),
                action_log=tuple(_mapping_list(row["action_log"])),
                artifacts=tuple(
                    journey_artifacts.get(str(row["page_capture_id"]), [])
                ),
                capture_role=(
                    str(row["capture_role"])
                    if row["capture_role"] is not None
                    else None
                ),
                parent_capture_id=(
                    str(row["parent_capture_id"])
                    if row["parent_capture_id"] is not None
                    else None
                ),
                surface_phase_run_id=(
                    str(step_metadata["surface_phase_run_id"])
                    if step_metadata.get("surface_phase_run_id") is not None
                    else None
                ),
                captured_at=(
                    str(row["captured_at"])
                    if row["captured_at"] is not None
                    else None
                ),
            )
            for row in journey_rows
            for step_metadata in [_mapping(row["metadata"])]
        )
        return ObservationSource(
            web_document_facts=web_document_facts(metadata),
            task_id=str(capture["task_id"]),
            page_capture_id=str(capture["page_capture_id"]),
            agent_run_id=str(capture["agent_run_id"]),
            page_url=page_url,
            page_title=(
                str(metadata["title"])
                if metadata.get("title") is not None
                else None
            ),
            page_type=(
                str(capture["page_type"])
                if capture["page_type"] is not None
                else None
            ),
            page_state=(
                str(capture["page_state"])
                if capture["page_state"] is not None
                else None
            ),
            action_log=tuple(_mapping_list(capture["action_log"])),
            artifacts=tuple(artifacts),
            journey_steps=journey_steps,
            surface_kind=(
                str(capture["surface_kind"])
                if capture.get("surface_kind") is not None
                else None
            ),
            visual_observation=(
                {
                    "visual_summary": visual_row["visual_summary"],
                    "key_elements": visual_row["key_elements"],
                    "candidate_targets": visual_row["candidate_targets"],
                    "result_hash": visual_row["result_hash"],
                    "page_state": visual_row["page_state"],
                    "overlay_signature": visual_row["overlay_signature"],
                    "surface_facts": _mapping(visual_row["surface_facts"]),
                    "source": "app_visual",
                }
                if visual_row is not None
                else None
            ),
            app_capture_facts=app_capture_facts or None,
            presentation=_mapping(metadata.get("presentation")) or None,
            declared_flags=declared_evidence_flags(metadata) or None,
            capture_role=(
                str(capture["capture_role"])
                if capture.get("capture_role") is not None
                else None
            ),
            parent_capture_id=(
                str(capture["parent_capture_id"])
                if capture.get("parent_capture_id") is not None
                else None
            ),
            surface_phase_run_id=(
                str(metadata["surface_phase_run_id"])
                if metadata.get("surface_phase_run_id") is not None
                else None
            ),
            captured_at=(
                str(capture["captured_at"])
                if capture.get("captured_at") is not None
                else None
            ),
            target_platform=(
                str(capture["target_platform"]).strip().casefold()
                if capture.get("target_platform") is not None
                else None
            ),
        )

    async def save_packet(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        rule_id: str,
        evidence_artifact_ids: tuple[str, ...],
        packet_payload: dict[str, Any],
        quality_gate_status: str,
        quality_gate_reason: str | None,
        agent_run_id: str | None,
    ) -> str:
        identity = packet_payload.get("identity")
        page = packet_payload.get("page")
        facts = packet_payload.get("facts")
        h5_semantic_revision = None
        if isinstance(identity, dict) and identity.get("target_platform") == "h5":
            h5_semantic_revision = {
                "page": {
                    "page_type": page.get("page_type"),
                    "page_state": page.get("page_state"),
                }
                if isinstance(page, dict)
                else {},
                "presentation": facts.get("presentation")
                if isinstance(facts, dict)
                else None,
            }
            if isinstance(identity.get("original_page_input"), dict):
                h5_semantic_revision["original_page_input"] = identity["original_page_input"]
        input_fingerprint = build_observation_input_fingerprint(
            evidence_artifact_ids=evidence_artifact_ids,
            capture_roles=identity.get("capture_roles") if isinstance(identity, dict) else None,
            semantic_revision=h5_semantic_revision,
        )
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_observation_packet AS packet (
                    task_id,
                    page_capture_id,
                    rule_id,
                    evidence_artifact_ids,
                    packet_payload,
                    quality_gate_status,
                    quality_gate_reason,
                    created_by_agent_run_id,
                    input_fingerprint
                )
                VALUES (
                    $1::uuid,
                    $2::uuid,
                    $3::uuid,
                    $4::jsonb,
                    $5::jsonb,
                    $6,
                    $7,
                    $8::uuid,
                    $9
                )
                ON CONFLICT (
                    created_by_agent_run_id,
                    page_capture_id,
                    rule_id,
                    input_fingerprint
                )
                WHERE created_by_agent_run_id IS NOT NULL
                  AND page_capture_id IS NOT NULL
                  AND rule_id IS NOT NULL
                  AND input_fingerprint IS NOT NULL
                DO UPDATE SET updated_at = packet.updated_at
                RETURNING
                    packet.id::text AS observation_packet_id,
                    packet.evidence_artifact_ids::text AS evidence_artifact_ids,
                    packet.packet_payload::text AS packet_payload,
                    packet.quality_gate_status AS quality_gate_status,
                    packet.quality_gate_reason AS quality_gate_reason
                """,
                task_id,
                page_capture_id,
                rule_id,
                json.dumps(list(evidence_artifact_ids), ensure_ascii=False),
                json.dumps(packet_payload, ensure_ascii=False, default=str),
                quality_gate_status,
                quality_gate_reason,
                agent_run_id,
                input_fingerprint,
            )
            if row is None:
                raise RuntimeError("Observation Packet 幂等写入后未找到已存在记录")
            _ensure_frozen_packet_matches(
                row=row,
                evidence_artifact_ids=evidence_artifact_ids,
                packet_payload=packet_payload,
                quality_gate_status=quality_gate_status,
                quality_gate_reason=quality_gate_reason,
            )
            return str(row["observation_packet_id"])


def _ensure_frozen_packet_matches(
    *,
    row: Any,
    evidence_artifact_ids: tuple[str, ...],
    packet_payload: dict[str, Any],
    quality_gate_status: str,
    quality_gate_reason: str | None,
) -> None:
    """命中同一幂等身份时验证冻结内容，禁止静默复用漂移输入。"""
    drifted = [
        field
        for field, stored, expected in (
            ("证据附件", json.loads(row["evidence_artifact_ids"]), list(evidence_artifact_ids)),
            ("观察包内容", json.loads(row["packet_payload"]), packet_payload),
            ("证据门禁结论", row["quality_gate_status"], quality_gate_status),
            ("证据门禁原因", row["quality_gate_reason"], quality_gate_reason),
        )
        if stored != expected
    ]
    if drifted:
        raise ObservationPacketValidationError(
            f"Observation Packet {row['observation_packet_id']} 已冻结，"
            f"本次{'、'.join(drifted)}与冻结内容不一致"
        )


def _validate_storage_path(
    storage_path: str,
    *,
    task_id: str,
    page_capture_id: str,
) -> None:
    path_parts = PurePosixPath(storage_path).parts
    if path_parts[:2] != (task_id, page_capture_id):
        raise ObservationPacketValidationError("证据存储路径归属不匹配")


def _validate_payload(
    payload: bytes,
    *,
    checksum: str,
    size_bytes: int | None,
) -> None:
    if size_bytes is not None and len(payload) != size_bytes:
        raise ObservationPacketValidationError("证据文件大小与元数据不一致")
    if not checksum:
        return
    algorithm, separator, expected = checksum.partition(":")
    if separator != ":" or algorithm.casefold() != "sha256":
        raise ObservationPacketValidationError("证据 checksum 格式无效")
    actual = hashlib.sha256(payload).hexdigest()
    if actual.casefold() != expected.casefold():
        raise ObservationPacketValidationError("证据 checksum 校验失败")


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _app_capture_facts(row: Any) -> dict[str, Any]:
    """只读取 qc_app_capture 的目标包、尺寸和设备快照事实。"""
    facts: dict[str, Any] = {}
    for alias, key in (
        ("app_package_name", "package_name"),
        ("app_activity", "activity"),
    ):
        value = row.get(alias) if hasattr(row, "get") else None
        if isinstance(value, str) and value.strip():
            facts[key] = value.strip()
    for alias, key in (("app_width", "width"), ("app_height", "height")):
        value = row.get(alias) if hasattr(row, "get") else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            facts[key] = value
    device_snapshot = _mapping(
        row.get("app_device_snapshot") if hasattr(row, "get") else None
    )
    if device_snapshot:
        facts["device_snapshot"] = device_snapshot
    return facts


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _app_ad_screenshot_artifact_ids(value: Any) -> tuple[str, ...]:
    """只为当前已完成的外部浏览器广告动作加载冻结截图对。"""
    artifact_ids: list[str] = []
    for action in _mapping_list(value):
        evidence = _mapping(action.get("ad_navigation_evidence"))
        if not (
            evidence.get("status") == "observed"
            and evidence.get("pair_complete") is True
            and evidence.get("navigation_mode") == "external_browser"
        ):
            continue
        for role in ("source", "landing"):
            artifact_id = str(_mapping(evidence.get(role)).get("artifact_id") or "")
            try:
                normalized = str(UUID(artifact_id))
            except (ValueError, AttributeError):
                continue
            if normalized not in artifact_ids:
                artifact_ids.append(normalized)
    return tuple(artifact_ids)
