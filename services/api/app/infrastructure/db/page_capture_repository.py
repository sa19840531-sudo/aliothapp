"""PostgreSQL 页面采集与证据产物仓储。"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_presentation import (
    inherited_operations_from_presentation,
)
from services.api.app.application.page_capture_persistence import (
    PersistedEvidenceArtifact,
    PersistedPageRecoveryCapture,
)
from services.api.app.application.web_evidence_admission import (
    assess_web_capture_evidence_admission,
)
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_METADATA_KEY,
    DiagnosticOperation,
    PresentationAssessment,
)
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    insert_manifest_on_connection,
)
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_session_permission_sql,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.page_type_judgement_repository import (
    PostgresPageTypeJudgementRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_PAGE_GRAPH_UNSET = object()


logger = logging.getLogger(__name__)


class PostgresPageCaptureRepository:
    """原子写入页面采集记录和对应证据元数据。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        page_graph_repository: Any = _PAGE_GRAPH_UNSET,
        web_run_policy: Any | None = None,
        page_type_judgement_repository: Any = _PAGE_GRAPH_UNSET,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._web_run_policy = web_run_policy
        # 页型判断台账：识别写库时把结构规则与模型各自的判断追加留档（只记录，不改正式页型）。
        self._page_type_judgement_repository = (
            page_type_judgement_repository
            if page_type_judgement_repository is not _PAGE_GRAPH_UNSET
            else PostgresPageTypeJudgementRepository(database_url=database_url, pool=pool)
        )
        # 省略投影依赖时绑定当前数据库连接池；测试/替身显式传
        # page_graph_repository=None，不能通过 pool 类型静默跳过生产链路。
        self._page_graph_repository = (
            page_graph_repository
            if page_graph_repository is not _PAGE_GRAPH_UNSET
            else PostgresPageGraphRepository(database_url=database_url, pool=pool)
        )

    async def save_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        page_capture_id: str,
        url: str,
        mode: str,
        reachable: bool | None,
        title: str | None,
        status_code: int | None,
        action_log: list[dict[str, Any]],
        artifacts: list[PersistedEvidenceArtifact],
        metrics: dict[str, Any] | None = None,
        parent_page_capture_id: str | None = None,
        capture_role: str = "primary",
        capture_purpose: str | None = None,
        direct_page_type: str | None = None,
        requested_url: str | None = None,
        surface_session_id: str | None = None,
        action_execution_id: str | None = None,
        session_command_sequence: int | None = None,
        finalization_epoch: int | None = None,
        inherit_parent_semantics: bool = False,
        manifest: EvidenceManifestReference | None = None,
        presentation: dict[str, Any] | None = None,
        declared_evidence_flags: dict[str, Any] | None = None,
    ) -> int:
        if direct_page_type is not None and not (
            direct_page_type == "ad_page"
            and capture_purpose == "web_ad_page"
            and capture_role == "supplemental"
            and inherit_parent_semantics is False
        ):
            raise ValueError("直接页面类型仅允许隔离广告证据签发 ad_page")
        if requested_url is not None and not (
            direct_page_type == "ad_page" and capture_purpose == "web_ad_page"
        ):
            raise ValueError("requested_url 仅允许隔离广告证据")
        # E09：manifest 与采集行、附件行必须在同一事务内提交，
        # 不允许出现「采集提交成功但无清单」的中间状态。
        if manifest is not None:
            if manifest.capture_id != page_capture_id or manifest.task_id != task_id:
                raise RuntimeError("Evidence Manifest 与 Capture 身份不一致，拒绝提交")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            page_seq = await connection.fetchval(
                """
                SELECT COALESCE(MAX(page_seq), 0) + 1
                FROM qc_page_capture
                WHERE task_id = $1::uuid
                """,
                task_id,
            )
            path_by_type: dict[str, str] = {}
            for artifact in artifacts:
                # screenshot 还承载多帧序列 JSON；页面摘要只能指向可直接展示的图像。
                if artifact.artifact_type == "screenshot" and not artifact.content_type.startswith(
                    "image/"
                ):
                    continue
                path_by_type[artifact.artifact_type] = artifact.storage_path
            resolved_metrics = dict(metrics or {})
            metadata = {
                "schema_version": "v2",
                "agent_run_id": agent_run_id,
                # 页面图需要保留完整路由（含 query/hash），不能使用 Runtime
                # 的公开 location 摘要把详情、分页或 Tab 合并成同一页。
                "url": url,
                "mode": mode,
                "reachable": reachable,
                "title": title,
                "status_code": status_code,
                "artifact_ids": [artifact.artifact_id for artifact in artifacts],
                "pipeline_metrics": resolved_metrics,
                "capture_role": capture_role,
                "surface_session_id": surface_session_id,
                "action_execution_id": action_execution_id,
                "session_command_sequence": session_command_sequence,
                "finalization_epoch": finalization_epoch,
            }
            if capture_purpose is not None:
                metadata["capture_purpose"] = capture_purpose
            if direct_page_type is not None:
                metadata["direct_page_type"] = direct_page_type
            if requested_url is not None:
                metadata["requested_url"] = requested_url
            if "web_readiness" in resolved_metrics:
                from services.api.app.application.web_run_policy import materialize_web_policy
                from services.api.app.domain.exploration.web_run_policy_snapshot import (
                    WebRunPolicyError,
                )
                from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
                    read_web_policy_on_connection,
                )

                policy_snapshot = self._web_run_policy or await read_web_policy_on_connection(
                    connection, task_id, agent_run_id
                )
                if policy_snapshot is None:
                    raise WebRunPolicyError("WEB_POLICY_HISTORY_MISSING: Web 入库缺少冻结正文")
                if (
                    policy_snapshot.task_id != task_id
                    or policy_snapshot.agent_run_id != agent_run_id
                ):
                    raise WebRunPolicyError("WEB_POLICY_OWNERSHIP_INVALID: Web 入库与策略归属不符")
                policy = materialize_web_policy(policy_snapshot)
                readiness = resolved_metrics.get("web_readiness")
                readiness_values = readiness if isinstance(readiness, dict) else {}
                metadata["web_evidence_admission"] = assess_web_capture_evidence_admission(
                    resolved_metrics,
                    profile=policy.readiness_profile,
                    capture_role=capture_role,
                    is_overlay=bool(readiness_values.get("blocking_layer")),
                    is_external_ad=bool(resolved_metrics.get("external_ad_landing") is True),
                    diagnostic=bool(resolved_metrics.get("diagnostic_evidence") is True),
                ).to_dict()
            if presentation is not None:
                metadata[PRESENTATION_METADATA_KEY] = presentation
            if declared_evidence_flags:
                metadata.update(declared_evidence_flags)
            captured_at = datetime.now(UTC)
            inserted_capture_id = await connection.fetchval(
                """
                INSERT INTO qc_page_capture (
                    id, task_id, agent_run_id, parent_page_capture_id,
                    page_seq, page_slug, url, page_type,
                    page_state, capture_status, inspection_lifecycle_status,
                    detection_status,
                    detection_confidence, detection_duration_ms,
                    screenshot_path, dom_path, visible_text_path, console_log_path,
                    action_log, metadata, captured_at,
                    surface_session_id, capture_role, action_execution_id,
                    session_command_sequence
                )
                SELECT
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7,
                    CASE
                        WHEN $22::text IS NOT NULL THEN $22::text
                        WHEN $16::boolean THEN parent.page_type
                        ELSE NULL
                    END,
                    CASE
                        WHEN $16::boolean THEN COALESCE(parent.page_state, $8)
                        ELSE $8
                    END,
                    'captured',
                    CASE
                        WHEN task.target_platform = 'h5'
                         AND $18::text IN ('probe_before', 'probe_result_replay')
                            THEN 'evidence_only'
                        ELSE 'active'
                    END,
                    CASE WHEN $16::boolean THEN parent.detection_status ELSE NULL END,
                    CASE WHEN $16::boolean THEN parent.detection_confidence ELSE NULL END,
                    CASE WHEN $16::boolean THEN parent.detection_duration_ms ELSE NULL END,
                    $9, $10, $11, $12, $13::jsonb,
                    CASE
                        WHEN $16::boolean
                         AND parent.metadata ? 'page_detection'
                        THEN $14::jsonb || jsonb_build_object(
                            'page_detection', parent.metadata->'page_detection'
                        )
                        ELSE $14::jsonb
                    END,
                    $15,
                    session.session_id,
                    CASE WHEN session.session_id IS NOT NULL THEN $18 ELSE NULL END,
                    action.action_execution_id,
                    session.command_sequence
                FROM qc_task AS task
                LEFT JOIN qc_page_capture parent
                  ON parent.id = $4::uuid
                 AND parent.task_id = $2::uuid
                 AND parent.agent_run_id = $3::uuid
                LEFT JOIN qc_surface_session AS session
                  ON session.session_id = $17
                 AND session.task_id = $2::uuid
                 AND session.command_sequence = $20
                 AND session.finalization_epoch = $21
                 AND session.status IN ('active', 'draining')
                 AND session.lease_token IS NOT NULL
                 AND session.lease_expires_at > now()
                LEFT JOIN qc_action_execution AS action
                  ON action.action_execution_id = $19
                 AND action.task_id = $2::uuid
                 AND action.session_id = session.session_id
                 AND action.command_sequence = session.command_sequence
                 AND action.finalization_epoch = session.finalization_epoch
                WHERE task.id = $2::uuid
                  AND (task.status IN ('queued', 'running')
                       OR (task.status = 'review' AND __H5_SNAPSHOT_SESSION_PERMISSION__))
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND ($17::text IS NULL OR session.session_id IS NOT NULL)
                  AND ($19::text IS NULL OR action.action_execution_id IS NOT NULL)
                  AND ($4::uuid IS NULL OR parent.id IS NOT NULL)
                RETURNING id
                """.replace("__H5_SNAPSHOT_SESSION_PERMISSION__",
                            h5_snapshot_session_permission_sql()),
                page_capture_id,
                task_id,
                agent_run_id,
                parent_page_capture_id,
                page_seq,
                f"{mode}-{page_seq}",
                url,
                "loaded" if reachable else "unreachable",
                path_by_type.get("screenshot"),
                path_by_type.get("dom"),
                path_by_type.get("visible_text"),
                path_by_type.get("console_log"),
                _json_dumps(action_log),
                _json_dumps(metadata),
                captured_at,
                inherit_parent_semantics,
                surface_session_id,
                capture_role,
                action_execution_id,
                session_command_sequence,
                finalization_epoch,
                direct_page_type,
            )
            if inserted_capture_id is None:
                raise RuntimeError(
                    "Page Capture 的父证据、Surface Session、命令序列、"
                    "Finalization Epoch 或 Action Execution 已变化"
                )
            for artifact in artifacts:
                await connection.execute(
                    """
                    INSERT INTO qc_evidence_artifact (
                        id, task_id, page_capture_id, artifact_type, source,
                        storage_path, content_type, checksum, size_bytes, metadata
                    )
                    VALUES (
                        $1::uuid, $2::uuid, $3::uuid, $4, 'baseline',
                        $5, $6, $7, $8, $9::jsonb
                    )
                    """,
                    artifact.artifact_id,
                    task_id,
                    page_capture_id,
                    artifact.artifact_type,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    _json_dumps(artifact.metadata),
                )
            if manifest is not None:
                # review 仅能通过上面的 H5 新会话恢复证明；同事务清单沿用已验证许可。
                await insert_manifest_on_connection(
                    connection,
                    manifest=manifest,
                    permitted_task_statuses=("queued", "running", "review"),
                )
            if self._page_graph_repository is not None:
                await self._page_graph_repository.project_capture_on_connection(
                    connection,
                    task_id=task_id,
                    capture_id=page_capture_id,
                    agent_run_id=agent_run_id,
                    surface_kind="web",
                    session_id=surface_session_id,
                    finalization_epoch=finalization_epoch,
                    command_sequence=session_command_sequence,
                    capture_role=capture_role,
                    metadata=metadata,
                    page_type=None,
                    page_state=None,
                    captured_at=captured_at,
                )
        return int(page_seq)

    async def load_h5_session_auth(
        self,
        *,
        task_id: str,
        surface_session_id: str,
    ) -> str:
        """读取 H5 会话认证账本；没有终态事实时明确返回 unknown。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT COALESCE(
                    (
                        SELECT CASE
                            WHEN step.decision_status IN (
                                'already_authenticated', 'authenticated'
                            ) THEN 'authenticated'
                            WHEN step.decision_status IN (
                                'disabled', 'guest_only', 'failed'
                            ) THEN 'guest'
                            ELSE 'unknown'
                        END
                        FROM qc_surface_phase_run AS phase_run
                        INNER JOIN qc_surface_phase_step AS step
                          ON step.phase_run_id = phase_run.phase_run_id
                         AND step.task_id = phase_run.task_id
                         AND step.session_id = phase_run.session_id
                        WHERE phase_run.task_id = task.id
                          AND phase_run.session_id = $2
                          AND phase_run.phase_key = 'h5_auth_bootstrap'
                        ORDER BY phase_run.started_at DESC, step.step_index DESC
                        LIMIT 1
                    ),
                    CASE
                        WHEN COALESCE(
                            (task.task_config->>'no_test_account')::boolean,
                            false
                        )
                        OR NOT COALESCE(
                            (task.task_config->>'has_test_account')::boolean,
                            true
                        )
                        THEN 'guest'
                        ELSE 'unknown'
                    END
                )
                FROM qc_task AS task
                WHERE task.id = $1::uuid
                  AND lower(task.target_platform) = 'h5'
                """,
                task_id,
                surface_session_id,
            )
        normalized = str(value or "unknown").strip().casefold()
        return normalized if normalized in {"guest", "authenticated", "unknown"} else "unknown"

    async def load_session_diagnostic_lineage(
        self,
        *,
        task_id: str,
        surface_session_id: str,
        document_epoch_ms: float | None = None,
    ) -> tuple[DiagnosticOperation, ...]:
        """同一 Surface Session、同一文档内更早采集的诊断操作；持久化服务在建清单前叠加到本次评估。

        恢复样式不等于恢复自然状态（专项七 §5.5），所以标记在同一文档内只向后传播、不会自行消失；
        整页重新加载后是新文档（document_epoch_ms 变化），被藏的元素已不存在，不再继承。
        任一侧没有记录文档起始时间（历史采集、App、降级快照）时按保守口径照常继承。
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id::text AS capture_id, metadata->'presentation' AS presentation
                FROM qc_page_capture
                WHERE task_id = $1::uuid
                  AND surface_session_id = $2
                  AND metadata->'presentation'->>'mutation_mode' = 'instrumented_diagnostic'
                  AND (
                      $3::double precision IS NULL
                      OR (metadata->'presentation'->>'document_epoch_ms') IS NULL
                      OR abs(
                          (metadata->'presentation'->>'document_epoch_ms')::double precision
                          - $3::double precision
                      ) < 1
                  )
                ORDER BY page_seq DESC
                LIMIT 1
                """,
                task_id,
                surface_session_id,
                document_epoch_ms,
            )
        if row is None:
            return ()
        raw = row["presentation"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return ()
        return inherited_operations_from_presentation(
            PresentationAssessment.from_dict(raw),
            origin_capture_id=str(row["capture_id"]),
        )

    async def update_detection_result(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        page_capture_id: str,
        page_type: str,
        page_state: str,
        detection_status: str,
        confidence: float,
        catalog_version: str,
        catalog_sha256: str,
        confidence_source: str = "unavailable",
        model_page_type: str | None = None,
        model_confidence: float | None = None,
        navigation_knowledge: dict[str, Any] | None = None,
        logical_surface: dict[str, Any] | None = None,
        decision_source: str | None = None,
        fallback_reason: str | None = None,
        matched_catalog_signals: dict[str, Any] | None = None,
        recognition_guide_audit: dict[str, Any] | None = None,
    ) -> bool:
        """按任务与运行归属回写页面识别结果。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            updated_id = await connection.fetchval(
                """
                UPDATE qc_page_capture AS capture
                SET page_type = $4,
                    page_state = $5,
                    detection_status = $6::text,
                    detection_confidence = $7::double precision,
                    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        'page_detection',
                        jsonb_build_object(
                            'detection_status', $6::text,
                            'confidence', $7::double precision,
                            'catalog_version', $8::text,
                            'catalog_sha256', $9::text,
                            'confidence_source', $10::text,
                            'model_page_type', $11::text,
                            'model_confidence', $12::double precision,
                            'navigation_knowledge', $13::jsonb,
                            'logical_surface', $14::jsonb,
                            'decision_source', $15::text,
                            'fallback_reason', $16::text,
                            'matched_catalog_signals', $17::jsonb,
                            'recognition_guide_audit', $18::jsonb
                        )
                    ),
                    updated_at = now()
                FROM qc_task AS task
                WHERE capture.id = $1::uuid
                  AND capture.task_id = $2::uuid
                  AND capture.agent_run_id = $3::uuid
                  AND task.id = capture.task_id
                  AND (task.status = 'running' OR (task.status = 'review' AND EXISTS (
                      SELECT 1 FROM qc_surface_session AS session
                      WHERE session.session_id = capture.surface_session_id
                        AND session.task_id = task.id
                        AND session.finalization_epoch = task.finalization_epoch
                        AND session.status IN ('active', 'draining')
                        AND __H5_SNAPSHOT_SESSION_PERMISSION__
                  )))
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                RETURNING capture.id
                """.replace("__H5_SNAPSHOT_SESSION_PERMISSION__",
                            h5_snapshot_session_permission_sql()),
                page_capture_id,
                task_id,
                agent_run_id,
                page_type,
                page_state,
                detection_status,
                confidence,
                catalog_version,
                catalog_sha256,
                confidence_source,
                model_page_type,
                model_confidence,
                _json_dumps(navigation_knowledge or {}),
                _json_dumps(logical_surface or {}),
                decision_source,
                fallback_reason,
                _json_dumps(matched_catalog_signals or {}),
                _json_dumps(recognition_guide_audit or {}),
            )
            if updated_id is not None and self._page_graph_repository is not None:
                await self._page_graph_repository.update_detection_on_connection(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    capture_id=page_capture_id,
                    page_type=page_type,
                    page_state=page_state,
                    detection_status=detection_status,
                    confidence=confidence,
                    logical_surface=logical_surface,
                )
            if updated_id is not None and self._page_type_judgement_repository is not None:
                await self._record_page_type_judgements(
                    connection,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    page_capture_id=page_capture_id,
                    page_type=page_type,
                    page_state=page_state,
                    detection_status=detection_status,
                    confidence=confidence,
                    catalog_version=catalog_version,
                    catalog_sha256=catalog_sha256,
                    confidence_source=confidence_source,
                    model_page_type=model_page_type,
                    model_confidence=model_confidence,
                )
        return updated_id is not None

    async def _record_page_type_judgements(self, connection: Any, **detection: Any) -> None:
        """判断台账走保存点：记录失败只告警回滚到保存点，不能把识别写库一起拖垮。"""
        try:
            async with connection.transaction():
                await self._page_type_judgement_repository.record_detection_on_connection(
                    connection, **detection
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "页型判断记录失败，正式识别不受影响：page_capture_id=%s",
                detection.get("page_capture_id"),
                exc_info=True,
            )

    async def get_recovery_capture(
        self,
        *,
        capture_id: str,
    ) -> PersistedPageRecoveryCapture | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await _load_recovery_capture(
                connection,
                capture_id=capture_id,
            )

    async def get_action_recovery_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedPageRecoveryCapture | None:
        if role not in {"before_action", "after_action"}:
            raise ValueError("role 必须为 before_action 或 after_action")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            capture_id = await connection.fetchval(
                """
                SELECT capture.id::text
                FROM qc_capture AS capture
                WHERE capture.action_execution_id = $1
                  AND capture.capture_role = $2
                  AND capture.surface_kind = 'web'
                  AND capture.surface_session_id LIKE 'h5:%'
                  AND capture.capture_status = 'captured'
                """,
                action_execution_id,
                role,
            )
            if capture_id is None:
                return None
            return await _load_recovery_capture(connection, capture_id=capture_id)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def _load_recovery_capture(
    connection: Any,
    *,
    capture_id: str,
) -> PersistedPageRecoveryCapture | None:
    row = await connection.fetchrow(
        """
        SELECT
            capture.id::text AS capture_id,
            capture.task_id::text AS task_id,
            capture.surface_session_id AS session_id,
            capture.parent_capture_id::text AS parent_capture_id,
            capture.action_execution_id,
            capture.capture_role,
            latest_manifest.quality_status AS manifest_quality_status
        FROM qc_capture AS capture
        LEFT JOIN LATERAL (
            SELECT manifest.quality_status
            FROM qc_evidence_manifest AS manifest
            WHERE manifest.capture_id = capture.id
            ORDER BY manifest.manifest_version DESC
            LIMIT 1
        ) AS latest_manifest ON TRUE
        WHERE capture.id = $1::uuid
          AND capture.surface_kind = 'web'
          AND capture.surface_session_id LIKE 'h5:%'
          AND capture.capture_status = 'captured'
        """,
        capture_id,
    )
    if row is None:
        return None
    artifact_rows = await connection.fetch(
        """
        SELECT
            artifact.id::text AS artifact_id,
            artifact.artifact_type,
            artifact.storage_path,
            artifact.content_type,
            artifact.checksum,
            artifact.size_bytes,
            artifact.metadata
        FROM qc_evidence_artifact AS artifact
        WHERE artifact.page_capture_id = $1::uuid
        ORDER BY artifact.id
        """,
        row["capture_id"],
    )
    artifacts = tuple(
        PersistedEvidenceArtifact(
            artifact_id=item["artifact_id"],
            artifact_type=item["artifact_type"],
            storage_path=item["storage_path"],
            content_type=item["content_type"],
            checksum=item["checksum"],
            size_bytes=item["size_bytes"],
            metadata=_json_object(item["metadata"]),
            created=False,
        )
        for item in artifact_rows
    )
    return PersistedPageRecoveryCapture(
        capture=CaptureRef(
            task_id=row["task_id"],
            session_id=row["session_id"],
            capture_id=row["capture_id"],
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role=row["capture_role"],
            artifact_ids=tuple(item.artifact_id for item in artifacts),
            parent_capture_id=row["parent_capture_id"],
            action_execution_id=row["action_execution_id"],
        ),
        artifacts=artifacts,
        manifest_quality_status=row["manifest_quality_status"],
    )


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Artifact metadata 必须为 JSON 对象")
    return dict(value)
