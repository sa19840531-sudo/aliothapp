"""Web V2 按 Capture 读取已持久化证据索引。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from services.api.app.application.web_page_inspection_notification import (
    WebPageInspectionEvidence,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeCatalogIdentity,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
    WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES,
    web_page_type_codes_for_catalog_version,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


@dataclass(frozen=True, kw_only=True)
class WebCaptureArtifactIndex:
    capture_id: str
    url: str
    artifact_ids: dict[str, str]
    captured_at: datetime
    capture_purpose: str = "web_page_ready_baseline"
    pipeline_metrics: dict[str, object] = field(default_factory=dict)
    requested_url: str | None = None
    redirect_chain: tuple[str, ...] = ()
    finalization_epoch: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "pipeline_metrics", dict(self.pipeline_metrics or {}))


class WebCapturePageTypePersistenceError(RuntimeError):
    """Web 页型桥接未通过可信 Capture 身份门禁。"""


class PostgresWebCaptureEvidenceRepository:
    """只读取 Capture 与附件归属，不读取或解释大体积内容。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        assessment_schema_version: str = "web-page-type-assessment.v3",
        catalog_identity: WebPageTypeCatalogIdentity | None = None,
    ) -> None:
        if assessment_schema_version not in {"web-page-type-assessment.v2", "web-page-type-assessment.v3", "web-page-type-assessment.v4"}:
            raise ValueError("Web assessment 协议不受支持")
        self._assessment_schema_version = assessment_schema_version
        self._catalog_identity = catalog_identity
        self._database_url = database_url
        self._pool = pool

    async def matches_active_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        current_only: bool = False,
    ) -> bool:
        """用持久化归属和当前运行围栏复核，不把调用上下文当 Capture 身份。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _MATCH_ACTIVE_CAPTURE_SQL,
                task_id,
                agent_run_id,
                session_id,
                capture_id,
                current_only,
            )
        return row is not None and str(row["capture_id"]) == capture_id

    async def read(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> WebCaptureArtifactIndex:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT capture.url, capture.captured_at, artifact.id, artifact.artifact_type,
                       artifact.content_type,
                       COALESCE(
                           common.metadata->'pipeline_metrics',
                           capture.metadata->'pipeline_metrics'
                       ) AS pipeline_metrics,
                       COALESCE(
                           common.metadata->>'finalization_epoch',
                           capture.metadata->>'finalization_epoch'
                       ) AS finalization_epoch,
                       COALESCE(
                           NULLIF(capture.metadata->>'capture_purpose', ''),
                           'web_page_ready_baseline'
                       ) AS capture_purpose
                FROM qc_page_capture AS capture
                INNER JOIN qc_capture AS common
                  ON common.task_id = capture.task_id
                 AND common.id = capture.id
                 AND common.capture_status = 'captured'
                INNER JOIN qc_evidence_artifact AS artifact
                  ON artifact.task_id = capture.task_id
                 AND artifact.page_capture_id = capture.id
                WHERE capture.task_id = $1::uuid
                  AND capture.id = $2::uuid
                  AND capture.capture_status = 'captured'
                  AND artifact.source IN ('baseline', 'rule_supplement')
                  AND artifact.artifact_type IN (
                      'screenshot', 'dom', 'visible_text', 'ui_snapshot'
                  )
                  AND (
                      artifact.artifact_type <> 'screenshot'
                      OR artifact.content_type = 'image/png'
                  )
                ORDER BY artifact.created_at ASC, artifact.id ASC
                """,
                task_id,
                capture_id,
            )
        if not rows:
            raise RuntimeError("WEB_CAPTURE_EVIDENCE_MISSING")
        artifact_ids: dict[str, str] = {}
        for row in rows:
            if (
                str(row["artifact_type"]) == "screenshot"
                and str(row["content_type"] or "") != "image/png"
            ):
                continue
            artifact_ids[str(row["artifact_type"])] = str(row["id"])
        pipeline_metrics = _json_mapping(rows[0].get("pipeline_metrics"))
        redirect_chain = _web_redirect_chain(
            pipeline_metrics.get("web_natural_redirect_chain")
        )
        return WebCaptureArtifactIndex(
            capture_id=capture_id,
            url=str(rows[0]["url"] or ""),
            artifact_ids=artifact_ids,
            captured_at=_captured_at(rows[0]["captured_at"]),
            capture_purpose=(
                _optional_row_text(rows[0], "capture_purpose") or "web_page_ready_baseline"
            ),
            pipeline_metrics=pipeline_metrics,
            requested_url=redirect_chain[0] if redirect_chain else str(rows[0]["url"] or ""),
            redirect_chain=redirect_chain,
            finalization_epoch=_optional_row_int(rows[0], "finalization_epoch") or 0,
        )

    async def read_current_capture_id(self, *, task_id: str, agent_run_id: str, session_id: str) -> str:
        """只提供权威当前帧定位；读取者仍须执行既有 current/owner/readiness 校验。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            capture_id = await connection.fetchval(
                "SELECT session.current_capture_id::text FROM qc_surface_session session "
                "JOIN qc_agent_run run ON run.task_id=session.task_id "
                "WHERE session.task_id=$1::uuid AND run.id=$2::uuid AND session.session_id=$3 "
                "AND session.session_id='web:' || run.id::text "
                "AND session.session_owner='web-v2-workflow' AND session.status='active'",
                task_id, agent_run_id, session_id,
            )
        if not capture_id:
            raise RuntimeError("WEB_CURRENT_CAPTURE_MISSING")
        return str(capture_id)

    async def persist_web_page_type_assessment(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        canonical_page_type: str | None,
        audit_metadata: Mapping[str, object],
        assessment_payload: Mapping[str, object] | None,
    ) -> str | None:
        """在同一事务内按冻结版本更新 Web 页型审计和 Assessment。"""
        _validate_web_page_type_assessment_payload(
            canonical_page_type=canonical_page_type,
            audit_metadata=audit_metadata,
            assessment_payload=assessment_payload,
            expected_schema_version=self._assessment_schema_version,
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture_id=capture_id,
            expected_catalog_identity=self._catalog_identity,
        )
        detection_status = (
            str(assessment_payload["detection_status"])
            if self._assessment_schema_version == "web-page-type-assessment.v4"
            and canonical_page_type == "unknown"
            else "recognized"
            if canonical_page_type is not None
            else str(assessment_payload["detection_status"])
        )
        capture_detection_status = {
            "recognized": "recognized",
            "unrecognized": "insufficient",
            "needs_review": "ambiguous",
        }[detection_status]
        assessment_page_type = str(assessment_payload["page_type"])
        assessment_page_states = assessment_payload.get("page_states")
        page_state = (
            str(assessment_page_states[0]).strip()
            if isinstance(assessment_page_states, list)
            and assessment_page_states
            and str(assessment_page_states[0]).strip()
            else "unknown"
        )
        assessment_audit = audit_metadata["web_page_type_assessment"]
        raw_confidence = (
            assessment_payload.get("confidence")
            if self._assessment_schema_version == "web-page-type-assessment.v4"
            else assessment_audit.get("confidence")
        )
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool)
            else None
        )
        assessment_record_id = (
            _web_assessment_record_id(
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=session_id,
                capture_id=capture_id,
                schema_version=self._assessment_schema_version,
            )
            if assessment_payload is not None
            else None
        )
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    _PERSIST_WEB_PAGE_TYPE_ASSESSMENT_SQL,
                    task_id,
                    agent_run_id,
                    session_id,
                    capture_id,
                    canonical_page_type,
                    _json_dumps(audit_metadata),
                    assessment_record_id,
                    assessment_payload.get("assessment_id") if assessment_payload else None,
                    assessment_payload.get("status") if assessment_payload else None,
                    _json_dumps(
                        [assessment_payload.get("reason_code")]
                        if assessment_payload is not None
                        else []
                    ),
                    _json_dumps(assessment_payload or {}),
                    _payload_hash(assessment_payload) if assessment_payload else None,
                    detection_status,
                    assessment_page_type,
                    page_state,
                    confidence,
                    capture_detection_status,
                    self._assessment_schema_version,
                )
                if row is None:
                    raise WebCapturePageTypePersistenceError(
                        "Web Capture 页型判定身份不匹配或状态无效，已拒绝写入"
                    )
                if str(row["capture_id"]) != capture_id:
                    raise WebCapturePageTypePersistenceError(
                        "Web Capture 页型判定返回了错误 Capture"
                    )
                if row["assessment_record_id"] is None or (
                    canonical_page_type is not None
                    and str(row["canonical_page_type"]) != canonical_page_type
                ):
                    raise WebCapturePageTypePersistenceError(
                        "Web Capture 页型判定未完整写入 Assessment"
                    )
                from services.api.app.infrastructure.db.page_graph_repository import (
                    PostgresPageGraphRepository,
                )

                graph = PostgresPageGraphRepository(pool=pool)
                await graph.project_capture_from_database_on_connection(
                    connection, capture_id=capture_id
                )
                await graph._refresh_capture_actions(connection, capture_id=capture_id)
                return (
                    str(row["assessment_record_id"])
                    if row["assessment_record_id"] is not None
                    else None
                )

    async def read_for_notification(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
    ) -> WebPageInspectionEvidence | None:
        """使用 ``web:{run_id}`` / ``web-v2-workflow`` 读取单 Capture 事实。"""

        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _READ_NOTIFICATION_EVIDENCE_SQL,
                task_id,
                agent_run_id,
                session_id,
                capture_id,
            )
        if row is None:
            return None
        return WebPageInspectionEvidence(
            task_id=_required_row_text(row, "task_id"),
            agent_run_id=_required_row_text(row, "agent_run_id"),
            session_id=_required_row_text(row, "session_id"),
            session_owner=_optional_row_text(row, "session_owner"),
            session_status=_optional_row_text(row, "session_status"),
            task_status=_optional_row_text(row, "task_status"),
            task_finalization_state=_optional_row_text(row, "task_finalization_state"),
            task_finalization_epoch=_optional_row_int(row, "task_finalization_epoch"),
            session_finalization_epoch=_optional_row_int(
                row,
                "session_finalization_epoch",
            ),
            action_finalization_epoch=_optional_row_int(row, "action_finalization_epoch"),
            run_status=_optional_row_text(row, "run_status"),
            current_run=bool(row.get("current_run")),
            capture_id=_required_row_text(row, "capture_id"),
            capture_status=_optional_row_text(row, "capture_status"),
            capture_role=_optional_row_text(row, "capture_role"),
            action_execution_id=_optional_row_text(row, "action_execution_id"),
            parent_capture_id=_optional_row_text(row, "parent_capture_id"),
            page_type=_optional_row_text(row, "page_type"),
            page_state=_optional_row_text(row, "page_state"),
            detection_status=_optional_row_text(row, "detection_status"),
            assessment_record_id=_optional_row_text(row, "assessment_record_id"),
            assessment_hash=_optional_row_text(row, "assessment_hash"),
            assessment_schema_version=_optional_row_text(
                row,
                "assessment_schema_version",
            ),
            artifact_types=_row_text_tuple(row, "artifact_types"),
            before_artifact_types=_row_text_tuple(row, "before_artifact_types"),
            manifest_id=_optional_row_text(row, "manifest_id"),
            manifest_hash=_optional_row_text(row, "manifest_hash"),
            manifest_quality_status=_optional_row_text(row, "manifest_quality_status"),
            before_manifest_id=_optional_row_text(row, "before_manifest_id"),
            before_manifest_hash=_optional_row_text(row, "before_manifest_hash"),
            before_manifest_quality_status=_optional_row_text(
                row,
                "before_manifest_quality_status",
            ),
            execution_mode=_optional_row_text(row, "execution_mode"),
            notification_mode=_optional_row_text(row, "notification_mode"),
            inspection_evidence_role=_optional_row_text(
                row,
                "inspection_evidence_role",
            ),
            logical_page_id=_optional_row_text(row, "logical_page_id"),
            content_fingerprint=_optional_row_text(row, "content_fingerprint"),
        )

    async def list_ready_capture_ids(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[str, ...]:
        """补偿扫描当前 Web Run 已桥接的动作后 Capture。"""

        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _LIST_NOTIFICATION_CAPTURE_IDS_SQL,
                task_id,
                agent_run_id,
            )
        return tuple(str(row["capture_id"]) for row in rows)

    async def list_ready_capture_ids_for_final_handoff(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> tuple[str, ...]:
        """仅为当前 Web 收口 owner 补扫停止探索后的可发布 Capture。"""

        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _LIST_FINAL_HANDOFF_CAPTURE_IDS_SQL,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
        return tuple(str(row["capture_id"]) for row in rows)

    async def record_notification_diagnostic(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        capture_id: str,
        reason_code: str,
        diagnostic_type: str | None,
        publication_id: str | None,
        retryable: bool,
        status: str = "retryable",
    ) -> None:
        """把通知补偿状态附着在可信 Capture 元数据，不改变证据事实。"""

        payload = {
            "status": status,
            "reason_code": reason_code,
            "diagnostic_type": diagnostic_type,
            "publication_id": publication_id,
            "retryable": retryable,
        }
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            await connection.execute(
                _RECORD_NOTIFICATION_DIAGNOSTIC_SQL,
                task_id,
                agent_run_id,
                capture_id,
                _json_dumps(payload),
            )

    async def mark_primary_content_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        parent_capture_id: str,
        direct_parent_capture_id: str,
        logical_page_id: str,
        capture_purpose: str,
        target_candidate_id: str,
        scroll_count: int,
        before_ad_coverage: float | None,
        after_ad_coverage: float,
    ) -> None:
        """把滚动补图标记为同一逻辑页面的证据版本，不改变表结构。"""

        metadata = {
            "logical_page_id": logical_page_id,
            "capture_purpose": capture_purpose,
            "primary_content_capture": {
                "parent_capture_id": parent_capture_id,
                "direct_parent_capture_id": direct_parent_capture_id,
                "target_candidate_id": target_candidate_id,
                "scroll_count": scroll_count,
                "before_ad_coverage": before_ad_coverage,
                "after_ad_coverage": after_ad_coverage,
            },
        }
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await _require_primary_capture_lineage(
                connection, task_id=task_id, agent_run_id=agent_run_id, session_id=session_id,
                capture_id=capture_id, baseline_id=parent_capture_id,
                direct_parent_id=direct_parent_capture_id, logical_page_id=logical_page_id,
                scroll_count=scroll_count, capture_purpose=capture_purpose,
            )
            row = await connection.fetchrow(
                _MARK_PRIMARY_CONTENT_CAPTURE_SQL,
                task_id,
                agent_run_id,
                session_id,
                capture_id,
                direct_parent_capture_id,
                _json_dumps(metadata),
            )
            if row is None or str(row["capture_id"]) != capture_id:
                raise WebCapturePageTypePersistenceError("Web 主体补图 Capture 身份或父证据不匹配")

    async def record_player_visual_recheck(
        self, *, task_id: str, agent_run_id: str, session_id: str,
        capture_id: str, payload: Mapping[str, object],
    ) -> None:
        """仅追加当前运行的视觉诊断，不改正式页型或动作权限。"""

        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != "web-player-visual-recheck.v1"
            or payload.get("status") not in {"visible", "not_visible", "uncertain"}
        ):
            raise ValueError("播放器视觉补判载荷无效")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_page_capture AS page
                SET metadata = jsonb_set(
                        COALESCE(page.metadata, '{}'::jsonb),
                        '{web_player_visual_recheck}', $5::jsonb, true
                    ),
                    updated_at = now()
                FROM qc_capture AS capture
                INNER JOIN qc_agent_run AS run
                  ON run.id = capture.agent_run_id
                 AND run.task_id = capture.task_id
                 AND run.agent_type = 'main'
                INNER JOIN qc_surface_session AS session
                  ON session.task_id = capture.task_id
                 AND session.session_id = capture.surface_session_id
                 AND session.session_owner = 'web-v2-workflow'
                 AND session.surface_kind = 'web'
                 AND session.status = 'active'
                WHERE page.task_id = $1::uuid
                  AND capture.agent_run_id = $2::uuid
                  AND capture.surface_session_id = $3
                  AND capture.id = $4::uuid
                  AND capture.id = page.id
                  AND capture.task_id = page.task_id
                  AND capture.surface_kind = 'web'
                  AND capture.capture_status = 'captured'
                  AND capture.surface_session_id = ('web:' || run.id::text)
                RETURNING page.id::text AS capture_id
                """,
                task_id, agent_run_id, session_id, capture_id,
                _json_dumps(dict(payload)),
            )
        if row is None or str(row["capture_id"]) != capture_id:
            raise WebCapturePageTypePersistenceError("播放器补判 Capture 身份不匹配")

    async def list_for_task(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope,
    ) -> dict[str, object] | None:
        """读取当前 Web V2 Run 的最小证据闭包，不读取附件正文或敏感载荷。"""

        if not isinstance(access_scope, AccessScope):
            raise TypeError("Web 证据读取必须提供 AccessScope")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            qualification = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    run.id::text AS agent_run_id,
                    budget.session_id,
                    budget.site_id,
                    budget.menu_limit,
                    task.finalization_epoch,
                    budget.state AS budget_state,
                    home_entry.normalization_version
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS run
                  ON run.task_id = task.id
                 AND run.id = $2::uuid
                 AND run.agent_type = 'main'
                INNER JOIN qc_web_exploration_budget AS budget
                  ON budget.task_id = task.id
                 AND budget.agent_run_id = run.id
                 AND budget.surface_kind = 'web'
                 AND budget.session_owner = 'web-v2-workflow'
                INNER JOIN qc_surface_session AS session
                  ON session.task_id = task.id
                 AND session.session_id = budget.session_id
                 AND session.surface_kind = 'web'
                 AND session.session_owner = 'web-v2-workflow'
                INNER JOIN qc_web_menu_ledger_entry AS home_entry
                  ON home_entry.agent_run_id = run.id
                 AND home_entry.site_id = budget.site_id
                 AND home_entry.decision = 'home_baseline'
                 AND home_entry.status = 'home_baseline'
                 AND home_entry.normalization_version IS NOT NULL
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'web'
                  AND LOWER(task.task_config->>'workflow_version') = 'v2'
                  AND LOWER(
                      run.input_context #>> '{task_config,_task_target,platform}'
                  ) = 'web'
                  AND run.id = (
                      SELECT latest.id
                      FROM qc_agent_run AS latest
                      WHERE latest.task_id = task.id
                        AND latest.agent_type = 'main'
                      ORDER BY latest.created_at DESC, latest.id DESC
                      LIMIT 1
                  )
                  AND (
                      SELECT COUNT(*)
                      FROM qc_web_exploration_budget AS counted
                      WHERE counted.task_id = task.id
                        AND counted.agent_run_id = run.id
                  ) = 1
                  AND (
                      $3::boolean
                      OR (
                          task.tenant_id = $4::uuid
                          AND (
                              $5::boolean
                              OR task.submitter_id = $6::uuid
                              OR task.department_name = $7::varchar
                          )
                      )
                  )
                """,
                task_id,
                agent_run_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
            if qualification is None:
                return None
            session_id = str(qualification["session_id"])
            site_id = str(qualification["site_id"])
            from services.api.app.domain.exploration.web_run_policy_snapshot import (
                WebRunPolicyError,
            )
            from services.api.app.infrastructure.db.web_capture_identity import (
                read_web_capture_identities,
            )

            identity_error = "WEB_LOGICAL_ID_UNTRUSTED"
            try:
                identities = await read_web_capture_identities(connection, task_id=task_id, agent_run_id=agent_run_id)
            except WebRunPolicyError as exc:
                identities = {}
                identity_error = str(exc).split(":", 1)[0]
            ledger_rows = await connection.fetch(
                """
                SELECT
                    entry.menu_entry_id,
                    entry.menu_path,
                    entry.discovery_manifest->'candidate'->>'url' AS candidate_url,
                    entry.source_kind,
                    entry.discovery_source_capture_id::text AS discovery_source_capture_id,
                    entry.parent_menu_entry_id,
                    entry.menu_depth,
                    entry.exploration_path,
                    entry.logical_target_identity,
                    entry.discovery_manifest->'candidate'->>'parent_logical_target_identity'
                        AS parent_logical_target_identity,
                    entry.diagnostic->>'error_code' AS error_code,
                    entry.catalog_position,
                    entry.decision,
                    entry.execution_slot,
                    entry.status,
                    entry.menu_action_count,
                    entry.menu_intent_id,
                    entry.menu_source_capture_id::text AS menu_source_capture_id,
                    entry.menu_action_execution_id,
                    entry.menu_probe_capture_id::text AS menu_probe_capture_id,
                    entry.menu_ready_capture_id::text AS menu_ready_capture_id,
                    entry.menu_page_type,
                    entry.detail_action_count,
                    entry.detail_intent_id,
                    entry.detail_source_capture_id::text AS detail_source_capture_id,
                    entry.detail_candidate_id,
                    entry.detail_destination_sha256,
                    entry.detail_action_execution_id,
                    entry.detail_probe_capture_id::text AS detail_probe_capture_id,
                    entry.detail_ready_capture_id::text AS detail_ready_capture_id,
                    entry.detail_page_type,
                    entry.detail_selection,
                    entry.diagnostic->>'reused_capture_id' AS reused_capture_id,
                    entry.diagnostic->>'reused_logical_page_id' AS reused_logical_page_id,
                    entry.diagnostic->>'reused_target_stage' AS reused_target_stage,
                    entry.diagnostic->'primary_content_diagnostics' AS primary_content_diagnostics,
                    entry.diagnostic->>'navigation_handoff_error' AS navigation_handoff_error,
                    entry.diagnostic->>'type' AS diagnostic_type,
                    entry.diagnostic->'ad_landing_evidence' AS ad_landing_evidence,
                    entry.diagnostic->'menu_interaction_outcome' AS menu_interaction_outcome
                FROM qc_web_menu_ledger_entry AS entry
                WHERE entry.agent_run_id = $1::uuid
                  AND entry.site_id = $2
                ORDER BY entry.catalog_position ASC, entry.menu_entry_id ASC
                """,
                agent_run_id,
                site_id,
            )
            capture_rows = await connection.fetch(
                """
                SELECT
                    common_capture.id::text AS capture_id,
                    common_capture.task_id::text AS task_id,
                    common_capture.agent_run_id::text AS agent_run_id,
                    common_capture.surface_kind,
                    common_capture.surface_session_id,
                    common_capture.parent_capture_id::text AS parent_capture_id,
                    common_capture.action_execution_id,
                    common_capture.capture_role,
                    common_capture.session_command_sequence,
                    common_capture.capture_status,
                    CASE WHEN jsonb_typeof(common_capture.metadata->'finalization_epoch') = 'number'
                        AND common_capture.metadata->>'finalization_epoch' ~ '^[0-9]{1,9}$'
                        THEN (common_capture.metadata->>'finalization_epoch')::integer END AS finalization_epoch,
                    CASE WHEN jsonb_typeof(page.metadata->'finalization_epoch') = 'number'
                        AND page.metadata->>'finalization_epoch' ~ '^[0-9]{1,9}$'
                        THEN (page.metadata->>'finalization_epoch')::integer END AS page_finalization_epoch,
                    page.url,
                    NULL::text AS logical_page_id,
                    page.page_type,
                    COALESCE(
                        NULLIF(page.metadata->>'capture_purpose', ''),
                        'web_page_ready_baseline'
                    ) AS capture_purpose,
                    page.metadata->'primary_content_capture'
                        AS primary_content_capture,
                    page.metadata->'web_evidence_admission' AS web_evidence_admission,
                    common_capture.metadata->'pipeline_metrics' AS pipeline_metrics,
                    page.metadata->'web_product_capability_assessment'
                        AS web_product_capability_assessment,
                    page.task_id::text AS page_task_id,
                    page.agent_run_id::text AS page_agent_run_id,
                    page.surface_session_id AS page_surface_session_id,
                    page.parent_page_capture_id::text AS page_parent_capture_id,
                    page.action_execution_id AS page_action_execution_id,
                    page.capture_role AS page_capture_role,
                    page.session_command_sequence AS page_session_command_sequence,
                    page.capture_status AS page_capture_status
                FROM qc_capture AS common_capture
                INNER JOIN qc_page_capture AS page
                  ON page.id = common_capture.id
                 AND page.task_id = common_capture.task_id
                WHERE common_capture.task_id = $1::uuid
                  AND common_capture.agent_run_id = $2::uuid
                  AND common_capture.surface_kind = 'web'
                  AND common_capture.surface_session_id = $3
                ORDER BY common_capture.created_at ASC, common_capture.id ASC
                """,
                task_id,
                agent_run_id,
                session_id,
            )
            action_rows = await connection.fetch(
                """
                SELECT
                    action.action_execution_id,
                    action.task_id::text AS task_id,
                    action.session_id,
                    action.intent_id,
                    action.command_sequence,
                    action.finalization_epoch,
                    action.intent_payload->>'action_type' AS action_type,
                    action.intent_payload->>'task_id' AS intent_task_id,
                    action.intent_payload->>'session_id' AS intent_session_id,
                    action.intent_payload->>'source_capture_id' AS intent_source_capture_id,
                    action.intent_payload->>'surface_kind' AS intent_surface_kind,
                    (action.intent_payload #> '{parameters,initial_surface_action}' = 'true'::jsonb) IS TRUE AS initial_surface_action,
                    action.intent_payload #>> '{parameters,web_capture_stage}' AS web_capture_stage,
                    action.intent_payload #>> '{parameters,target_mode}' AS target_mode,
                    action.requested_source_capture_id::text
                        AS requested_source_capture_id,
                    action.source_capture_id::text AS source_capture_id,
                    action.result_capture_id::text AS result_capture_id,
                    action.status,
                    action.error_code,
                    action.started_at,
                    action.action_finished_at,
                    action.evidence_finished_at,
                    edge.proof AS transition_proof,
                    edge.counts_as_business_arrival
                FROM qc_action_execution AS action
                LEFT JOIN qc_page_graph_transition AS edge
                  ON edge.task_id = action.task_id
                 AND edge.agent_run_id = $3::uuid
                 AND edge.action_execution_id = action.action_execution_id
                WHERE action.task_id = $1::uuid
                  AND action.session_id = $2
                ORDER BY action.command_sequence ASC, action.action_execution_id ASC
                """,
                task_id,
                session_id,
                agent_run_id,
            )
            artifact_rows = await connection.fetch(
                """
                SELECT
                    artifact.id::text AS artifact_id,
                    artifact.page_capture_id::text AS capture_id,
                    artifact.artifact_type,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.source,
                    artifact.lifecycle_status
                FROM qc_evidence_artifact AS artifact
                INNER JOIN qc_capture AS common_capture
                  ON common_capture.id = artifact.page_capture_id
                WHERE common_capture.task_id = $1::uuid
                  AND common_capture.agent_run_id = $2::uuid
                  AND common_capture.surface_kind = 'web'
                  AND common_capture.surface_session_id = $3
                  AND artifact.artifact_type IN (
                      'screenshot', 'dom', 'visible_text', 'ui_snapshot', 'action_trace'
                  )
                  AND artifact.source IN ('baseline', 'rule_supplement')
                  AND artifact.lifecycle_status IN ('active', 'archived')
                ORDER BY artifact.created_at ASC, artifact.id ASC
                """,
                task_id,
                agent_run_id,
                session_id,
            )
        return {
            "task_id": str(qualification["task_id"]),
            "target_platform": "web",
            "workflow_version": "v2",
            "session_owner": "web-v2-workflow",
            "agent_run_id": str(qualification["agent_run_id"]),
            "session_id": session_id,
            "site_id": site_id,
            "menu_limit": (
                int(qualification["menu_limit"])
                if qualification["menu_limit"] is not None else None
            ),
            "budget_state": str(qualification["budget_state"]),
            "finalization_epoch": int(qualification["finalization_epoch"]),
            "normalization_version": str(qualification["normalization_version"]),
            "ledger": [
                dict(
                    row,
                    menu_path=(json.loads(row["menu_path"]) if isinstance(row.get("menu_path"), str)
                               else row.get("menu_path") or []),
                    ad_landing_evidence=_json_mapping(row.get("ad_landing_evidence")),
                    detail_selection=_json_mapping(row.get("detail_selection")),
                    primary_content_diagnostics=_json_rows(
                        row.get("primary_content_diagnostics")
                    ),
                )
                for row in ledger_rows
            ],
            "captures": [
                {
                    **dict(row),
                    **identities.get(str(row["capture_id"]), {"logical_page_id": None, "logical_page_identity_status": identity_error}),
                    **{
                        field: _json_mapping(row.get(field))
                        for field in (
                            "pipeline_metrics",
                            "web_evidence_admission",
                            "web_product_capability_assessment",
                            "primary_content_capture",
                        )
                    },
                }
                for row in capture_rows
            ],
            "actions": [dict(row) for row in action_rows],
            "artifacts": [dict(row) for row in artifact_rows],
        }


__all__ = [
    "PostgresWebCaptureEvidenceRepository",
    "WebCaptureArtifactIndex",
    "WebCapturePageTypePersistenceError",
]


_MATCH_ACTIVE_CAPTURE_SQL = """
SELECT capture.id AS capture_id
FROM qc_task AS task
JOIN qc_agent_run AS run ON run.task_id = task.id AND run.id = $2::uuid
 AND run.agent_type = 'main'
JOIN qc_surface_session AS session ON session.task_id = task.id
 AND session.session_id = $3 AND session.session_id = ('web:' || run.id::text)
 AND session.surface_kind = 'web' AND session.session_owner = 'web-v2-workflow'
 AND session.status = 'active' AND session.finalization_epoch = task.finalization_epoch
JOIN qc_capture AS capture ON capture.task_id = task.id AND capture.id = $4::uuid
 AND capture.agent_run_id = run.id AND capture.surface_kind = 'web'
 AND capture.surface_session_id = session.session_id AND capture.capture_status = 'captured'
 AND NULLIF(capture.metadata->>'finalization_epoch', '')::integer = task.finalization_epoch
JOIN qc_page_capture AS page ON page.task_id = task.id AND page.id = capture.id
 AND page.agent_run_id = run.id AND page.surface_session_id = session.session_id
 AND page.capture_status = 'captured'
WHERE task.id = $1::uuid AND task.target_platform = 'web'
 AND (NOT $5::boolean OR session.current_capture_id = capture.id)
 AND LOWER(task.task_config->>'workflow_version') = 'v2'
 AND run.id = (SELECT latest.id FROM qc_agent_run AS latest
     WHERE latest.task_id = task.id AND latest.agent_type = 'main'
     ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)
"""


_PERSIST_WEB_PAGE_TYPE_ASSESSMENT_SQL = """
WITH qualified AS (
    SELECT page.id, page.page_type AS canonical_page_type
    FROM qc_task AS task
    INNER JOIN qc_agent_run AS run
      ON run.task_id = task.id
     AND run.id = $2::uuid
     AND run.agent_type = 'main'
    INNER JOIN qc_surface_session AS session
      ON session.task_id = task.id
     AND session.session_id = $3
     AND session.session_id = ('web:' || run.id::text)
     AND session.surface_kind = 'web'
     AND session.session_owner = 'web-v2-workflow'
     AND session.status = 'active'
    INNER JOIN qc_capture AS capture
      ON capture.task_id = task.id
     AND capture.id = $4::uuid
     AND capture.agent_run_id = run.id
     AND capture.surface_kind = 'web'
     AND capture.surface_session_id = $3
     AND capture.capture_status = 'captured'
    INNER JOIN qc_page_capture AS page
      ON page.task_id = capture.task_id
     AND page.id = capture.id
     AND page.agent_run_id = run.id
     AND page.surface_session_id = $3
     AND page.capture_status = 'captured'
    WHERE task.id = $1::uuid
      AND task.target_platform = 'web'
      AND LOWER(task.task_config->>'workflow_version') = 'v2'
      AND session.finalization_epoch = task.finalization_epoch
      AND NULLIF(capture.metadata->>'finalization_epoch', '')::integer
          = task.finalization_epoch
      AND run.id = (
          SELECT latest.id
          FROM qc_agent_run AS latest
          WHERE latest.task_id = task.id
            AND latest.agent_type = 'main'
          ORDER BY latest.created_at DESC, latest.id DESC
          LIMIT 1
      )
    FOR UPDATE OF page
), existing_assessment AS (
    SELECT
        stored.assessment_record_id,
        stored.task_id,
        stored.agent_run_id,
        stored.session_id,
        stored.payload_hash
    FROM qualified
    INNER JOIN qc_h5_page_type_assessment AS stored
      ON stored.page_capture_id = qualified.id
     AND stored.schema_version = $18::text
), upserted_assessment AS (
    INSERT INTO qc_h5_page_type_assessment (
        assessment_record_id, assessment_id, task_id, agent_run_id, session_id,
        page_capture_id, detection_status, page_type, page_state, status,
        reason_codes, safety_summary, assessment_payload, schema_version, payload_hash
    )
    SELECT
        $7, $8, $1::uuid, $2::uuid, $3, qualified.id,
        $13, $14, $15::text, $9,
        $10::jsonb, '{}'::jsonb, $11::jsonb,
        $18::text, $12
    FROM qualified
    WHERE $14::text IS NOT NULL
    ON CONFLICT (page_capture_id, schema_version) DO NOTHING
    RETURNING assessment_record_id
), resolved_assessment AS (
    SELECT
        upserted_assessment.assessment_record_id,
        TRUE AS assessment_is_new
    FROM upserted_assessment
    UNION ALL
    SELECT
        existing_assessment.assessment_record_id,
        FALSE AS assessment_is_new
    FROM existing_assessment
    WHERE existing_assessment.task_id = $1::uuid
      AND existing_assessment.agent_run_id = $2::uuid
      AND existing_assessment.session_id = $3
      AND existing_assessment.payload_hash = $12
      AND NOT EXISTS (SELECT 1 FROM upserted_assessment)
), updated_capture AS (
    UPDATE qc_page_capture AS page
    SET page_type = CASE
            WHEN $5::text IS NULL THEN page.page_type
            ELSE $5
        END,
        page_state = $15::text,
        detection_status = $17::text,
        detection_confidence = $16::double precision,
        metadata = COALESCE(page.metadata, '{}'::jsonb)
            || $6::jsonb
            || jsonb_build_object(
                'page_detection',
                jsonb_build_object(
                    'schema_version', $18::text,
                    'detection_status', $17::text,
                    'assessment_detection_status', $13::text,
                    'page_type', $14::text,
                    'page_state', $15::text,
                    'confidence', $16::double precision,
                    'confidence_source', 'web_page_type_assessment'
                )
            ),
        updated_at = now()
    FROM qualified
    INNER JOIN resolved_assessment
      ON resolved_assessment.assessment_is_new
    WHERE page.id = qualified.id
    RETURNING page.id::text AS capture_id, page.page_type AS canonical_page_type
)
SELECT
    qualified.id::text AS capture_id,
    COALESCE(updated_capture.canonical_page_type, qualified.canonical_page_type)
        AS canonical_page_type,
    resolved_assessment.assessment_record_id
FROM qualified
LEFT JOIN resolved_assessment ON TRUE
LEFT JOIN updated_capture ON TRUE
"""


_READ_NOTIFICATION_EVIDENCE_SQL = """
SELECT
    task.id::text AS task_id,
    $2::uuid::text AS agent_run_id,
    $3::text AS session_id,
    session.session_owner,
    session.status AS session_status,
    task.status AS task_status,
    task.finalization_state AS task_finalization_state,
    task.finalization_epoch AS task_finalization_epoch,
    session.finalization_epoch AS session_finalization_epoch,
    action.finalization_epoch AS action_finalization_epoch,
    run.status AS run_status,
    run.id = (
        SELECT latest.id
        FROM qc_agent_run AS latest
        WHERE latest.task_id = task.id
          AND latest.agent_type = 'main'
        ORDER BY latest.created_at DESC, latest.id DESC
        LIMIT 1
    ) AS current_run,
    capture.id::text AS capture_id,
    capture.capture_status,
    capture.capture_role,
    capture.action_execution_id,
    before_capture.id::text AS parent_capture_id,
    page.page_type,
    COALESCE(page.page_state, 'stable') AS page_state,
    assessment.detection_status,
    assessment.assessment_record_id,
    assessment.payload_hash AS assessment_hash,
    assessment.schema_version AS assessment_schema_version,
    assessment.assessment_payload->>'inspection_evidence_role'
        AS inspection_evidence_role,
    COALESCE(
        page.metadata->>'logical_page_id',
        capture.metadata->>'logical_page_id'
    ) AS logical_page_id,
    after_artifacts.content_fingerprint,
    COALESCE(after_artifacts.artifact_types, ARRAY[]::text[]) AS artifact_types,
    COALESCE(before_artifacts.artifact_types, ARRAY[]::text[]) AS before_artifact_types,
    after_manifest.manifest_id,
    after_manifest.manifest_hash,
    after_manifest.quality_status AS manifest_quality_status,
    before_manifest.manifest_id AS before_manifest_id,
    before_manifest.manifest_hash AS before_manifest_hash,
    before_manifest.quality_status AS before_manifest_quality_status,
    task.task_config->>'execution_mode' AS execution_mode,
    task.task_config->>'web_inspection_notification_mode' AS notification_mode
FROM qc_task AS task
INNER JOIN qc_capture AS capture
  ON capture.task_id = task.id
 AND capture.id = $4::uuid
INNER JOIN qc_page_capture AS page
  ON page.task_id = capture.task_id
 AND page.id = capture.id
LEFT JOIN qc_agent_run AS run
  ON run.task_id = task.id
 AND run.id = $2::uuid
 AND run.id = capture.agent_run_id
 AND run.agent_type = 'main'
LEFT JOIN qc_surface_session AS session
  ON session.task_id = task.id
 AND session.session_id = $3
 AND session.session_id = ('web:' || $2::uuid::text)
 AND session.surface_kind = 'web'
 AND session.session_owner = 'web-v2-workflow'
 AND capture.surface_kind = 'web'
 AND capture.surface_session_id = session.session_id
LEFT JOIN qc_action_execution AS action
  ON action.task_id = task.id
 AND action.session_id = session.session_id
 AND action.action_execution_id = capture.action_execution_id
 AND action.result_capture_id = capture.id
LEFT JOIN qc_capture AS before_capture
  ON before_capture.task_id = task.id
 AND before_capture.id = action.source_capture_id
 AND before_capture.agent_run_id = run.id
 AND before_capture.surface_kind = 'web'
 AND before_capture.surface_session_id = session.session_id
 AND before_capture.capture_role = 'before_action'
 AND before_capture.action_execution_id = action.action_execution_id
LEFT JOIN LATERAL (
    SELECT
        array_agg(DISTINCT artifact.artifact_type ORDER BY artifact.artifact_type)
            AS artifact_types,
        md5(string_agg(
            artifact.artifact_type || ':' || artifact.checksum,
            '|' ORDER BY artifact.artifact_type, artifact.checksum
        ) FILTER (WHERE artifact.artifact_type IN ('dom', 'visible_text')))
            AS content_fingerprint
    FROM qc_evidence_artifact AS artifact
    WHERE artifact.task_id = task.id
      AND artifact.page_capture_id = capture.id
      AND artifact.source IN ('baseline', 'rule_supplement')
      AND artifact.lifecycle_status IN ('active', 'archived')
      AND (
          artifact.artifact_type <> 'screenshot'
          OR artifact.content_type = 'image/png'
      )
) AS after_artifacts ON TRUE
LEFT JOIN LATERAL (
    SELECT array_agg(DISTINCT artifact.artifact_type ORDER BY artifact.artifact_type)
        AS artifact_types
    FROM qc_evidence_artifact AS artifact
    WHERE artifact.task_id = task.id
      AND artifact.page_capture_id = before_capture.id
      AND artifact.source IN ('baseline', 'rule_supplement')
      AND artifact.lifecycle_status IN ('active', 'archived')
      AND (
          artifact.artifact_type <> 'screenshot'
          OR artifact.content_type = 'image/png'
      )
) AS before_artifacts ON TRUE
LEFT JOIN LATERAL (
    SELECT stored.assessment_record_id, stored.detection_status,
           stored.payload_hash, stored.schema_version, stored.assessment_payload
    FROM qc_h5_page_type_assessment AS stored
    WHERE stored.task_id = task.id
      AND stored.agent_run_id = run.id
      AND stored.session_id = session.session_id
      AND stored.page_capture_id = capture.id
      AND stored.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
      AND stored.page_type = page.page_type
    ORDER BY stored.updated_at DESC, stored.assessment_record_id ASC
    LIMIT 1
) AS assessment ON TRUE
LEFT JOIN LATERAL (
    SELECT manifest.manifest_id, manifest.manifest_hash, manifest.quality_status
    FROM qc_evidence_manifest AS manifest
    WHERE manifest.task_id = task.id
      AND manifest.capture_id = capture.id
    ORDER BY manifest.manifest_version DESC
    LIMIT 1
) AS after_manifest ON TRUE
LEFT JOIN LATERAL (
    SELECT manifest.manifest_id, manifest.manifest_hash, manifest.quality_status
    FROM qc_evidence_manifest AS manifest
    WHERE manifest.task_id = task.id
      AND manifest.capture_id = before_capture.id
    ORDER BY manifest.manifest_version DESC
    LIMIT 1
) AS before_manifest ON TRUE
WHERE task.id = $1::uuid
  AND task.target_platform = 'web'
  AND LOWER(task.task_config->>'workflow_version') = 'v2'
  AND LOWER(
      run.input_context #>> '{task_config,_task_target,platform}'
  ) = 'web'
"""


_LIST_NOTIFICATION_CAPTURE_IDS_SQL = """
SELECT capture.id::text AS capture_id
FROM qc_task AS task
INNER JOIN qc_agent_run AS run
  ON run.task_id = task.id
 AND run.id = $2::uuid
 AND run.agent_type = 'main'
INNER JOIN qc_surface_session AS session
  ON session.task_id = task.id
 AND session.session_id = ('web:' || run.id::text)
 AND session.surface_kind = 'web'
 AND session.session_owner = 'web-v2-workflow'
 AND session.status = 'active'
INNER JOIN qc_capture AS capture
  ON capture.task_id = task.id
 AND capture.agent_run_id = run.id
 AND capture.surface_kind = 'web'
 AND capture.surface_session_id = session.session_id
 AND capture.capture_status = 'captured'
 AND capture.capture_role = 'after_action'
INNER JOIN qc_page_capture AS page
  ON page.task_id = capture.task_id
 AND page.id = capture.id
INNER JOIN qc_h5_page_type_assessment AS assessment
  ON assessment.task_id = task.id
 AND assessment.agent_run_id = run.id
 AND assessment.session_id = session.session_id
 AND assessment.page_capture_id = capture.id
 AND assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
 AND (
      assessment.detection_status IN ('unrecognized', 'needs_review')
      OR (
          assessment.detection_status = 'recognized'
          AND assessment.page_type = page.page_type
      )
 )
WHERE task.id = $1::uuid
  AND task.target_platform = 'web'
  AND task.status = 'running'
  AND task.finalization_state = 'open'
  AND task.task_config->>'execution_mode' = 'full_inspection'
  AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
  AND session.finalization_epoch = task.finalization_epoch
  AND run.id = (
      SELECT latest.id
      FROM qc_agent_run AS latest
      WHERE latest.task_id = task.id
        AND latest.agent_type = 'main'
      ORDER BY latest.created_at DESC, latest.id DESC
      LIMIT 1
  )
ORDER BY capture.created_at ASC, capture.id ASC
"""


_LIST_FINAL_HANDOFF_CAPTURE_IDS_SQL = """
SELECT capture.id::text AS capture_id
FROM qc_task AS task
INNER JOIN qc_agent_run AS run
 ON run.task_id = task.id
 AND run.id = $2::uuid
 AND run.agent_type = 'main'
 AND run.status IN ('pending', 'running', 'done', 'failed', 'aborted')
INNER JOIN qc_surface_session AS session
  ON session.task_id = task.id
 AND session.session_id = ('web:' || run.id::text)
 AND session.surface_kind = 'web'
 AND session.session_owner = 'web-v2-workflow'
 AND session.status = 'active'
 AND session.finalization_epoch = task.finalization_epoch
INNER JOIN qc_capture AS capture
  ON capture.task_id = task.id
 AND capture.agent_run_id = run.id
 AND capture.surface_kind = 'web'
 AND capture.surface_session_id = session.session_id
 AND capture.capture_status = 'captured'
 AND capture.capture_role = 'after_action'
INNER JOIN qc_page_capture AS page
  ON page.task_id = capture.task_id
 AND page.id = capture.id
 AND COALESCE(
       page.inspection_lifecycle_status,
       capture.metadata->>'inspection_lifecycle_status',
       ''
     ) NOT IN ('superseded', 'abandoned')
INNER JOIN qc_h5_page_type_assessment AS assessment
  ON assessment.task_id = task.id
 AND assessment.agent_run_id = run.id
 AND assessment.session_id = session.session_id
 AND assessment.page_capture_id = capture.id
 AND assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
 AND (
      assessment.detection_status IN ('unrecognized', 'needs_review')
      OR (
          assessment.detection_status = 'recognized'
          AND assessment.page_type = page.page_type
      )
 )
WHERE task.id = $1::uuid
  AND task.target_platform = 'web'
  AND LOWER(task.task_config->>'workflow_version') = 'v2'
  AND LOWER(
      run.input_context #>> '{task_config,_task_target,platform}'
  ) = 'web'
  AND task.status = 'running'
  AND task.finalization_state = 'exploration_draining'
  AND task.finalization_agent_run_id = run.id
  AND task.finalization_epoch = $3
  AND task.task_config->>'execution_mode' = 'full_inspection'
  AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
  AND run.id = (
      SELECT latest.id
      FROM qc_agent_run AS latest
      WHERE latest.task_id = task.id
        AND latest.agent_type = 'main'
      ORDER BY latest.created_at DESC, latest.id DESC
      LIMIT 1
  )
ORDER BY capture.created_at ASC, capture.id ASC
"""


_RECORD_NOTIFICATION_DIAGNOSTIC_SQL = """
WITH current_task AS MATERIALIZED (
    SELECT task.id, task.finalization_epoch
    FROM qc_task AS task
    WHERE task.id = $1::uuid AND task.status = 'running'
      AND task.finalization_state = 'open'
      AND $2::uuid = (
          SELECT run.id FROM qc_agent_run AS run
          WHERE run.task_id = task.id AND run.agent_type = 'main'
          ORDER BY run.created_at DESC, run.id DESC LIMIT 1
      )
    FOR UPDATE
)
UPDATE qc_page_capture AS page
SET metadata = jsonb_set(
        COALESCE(page.metadata, '{}'::jsonb),
        '{web_inspection_notification}',
        $4::jsonb,
        true
    ),
    updated_at = now()
FROM qc_capture AS capture
INNER JOIN current_task AS task ON task.id = capture.task_id
INNER JOIN qc_surface_session AS session
  ON session.task_id = capture.task_id
 AND session.session_id = capture.surface_session_id
 AND session.session_id = ('web:' || $2::uuid::text)
 AND session.surface_kind = 'web'
 AND session.session_owner = 'web-v2-workflow'
 AND session.status = 'active'
 AND session.finalization_epoch = task.finalization_epoch
INNER JOIN qc_action_execution AS action
  ON action.action_execution_id = capture.action_execution_id
 AND action.task_id = capture.task_id
 AND action.finalization_epoch = task.finalization_epoch
WHERE page.task_id = $1::uuid
  AND page.id = $3::uuid
  AND capture.task_id = page.task_id
  AND capture.id = page.id
  AND capture.agent_run_id = $2::uuid
  AND capture.capture_status = 'captured'
"""


async def _require_primary_capture_lineage(
    connection, *, task_id, agent_run_id, session_id, capture_id, baseline_id,
    direct_parent_id, logical_page_id, scroll_count, capture_purpose,
):
    """在写元数据前按冻结策略复核首屏及至多两次真实滚动，不能自报原父帧。"""
    from services.api.app.application.web_evidence_admission import (
        assess_web_capture_evidence_admission,
    )
    from services.api.app.application.web_run_policy import materialize_web_policy
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        read_web_policy_on_connection,
    )

    def reject():
        raise WebCapturePageTypePersistenceError("Web 主体补图范围、自然滚动或父链无效")

    if type(scroll_count) is not int or not 1 <= scroll_count <= 2 or capture_purpose != "web_primary_content_evidence":
        reject()
    # 与会话的下一次命令及任务收口互斥，验证和写入使用同一事务。
    lifecycle = await connection.fetchrow(
        "SELECT task.id FROM qc_task task JOIN qc_agent_run run ON run.task_id=task.id "
        "WHERE task.id=$1::uuid AND run.id=$2::uuid AND task.target_platform='web' "
        "AND task.task_config->>'workflow_version'='v2' AND task.status='running' "
        "AND task.finalization_state='open' AND run.agent_type='main' AND run.status='running' "
        "FOR SHARE OF task,run", task_id, agent_run_id,
    )
    if lifecycle is None:
        reject()
    await connection.fetchrow("SELECT session_id FROM qc_surface_session WHERE session_id=$1 FOR UPDATE", session_id)
    for candidate, current_only in ((baseline_id, False), (capture_id, True)):
        if await connection.fetchrow(_MATCH_ACTIVE_CAPTURE_SQL, task_id, agent_run_id, session_id, candidate, current_only) is None:
            reject()
    snapshot = await read_web_policy_on_connection(connection, task_id, agent_run_id)
    if snapshot is None:
        reject()
    policy = materialize_web_policy(snapshot)
    rows = await connection.fetch(
        "SELECT page.id::text AS capture_id,page.url,common.capture_role,common.metadata->'pipeline_metrics' AS metrics "
        "FROM qc_page_capture page JOIN qc_capture common ON common.id=page.id "
        "WHERE page.id=ANY($1::uuid[])", [baseline_id, capture_id],
    )
    if len(rows) != 2:
        reject()
    for row in rows:
        if policy.identity_policy.logical_id(row["url"]) != logical_page_id or not assess_web_capture_evidence_admission(
            _json_mapping(row["metrics"]), capture_role=row["capture_role"], profile=policy.readiness_profile,
        ).can_enter_formal_bundle:
            reject()
    current = capture_id
    for step in range(scroll_count):
        row = await connection.fetchrow(
            "SELECT action.requested_source_capture_id::text AS requested_id,before.id::text AS before_id "
            "FROM qc_capture after JOIN qc_action_execution action ON action.action_execution_id=after.action_execution_id "
            "JOIN qc_capture before ON before.id=action.source_capture_id "
            "JOIN qc_task task ON task.id=after.task_id "
            "WHERE after.id=$1::uuid AND after.task_id=$2::uuid AND after.agent_run_id=$3::uuid "
            "AND after.surface_session_id=$4 AND before.task_id=after.task_id "
            "AND before.agent_run_id=after.agent_run_id AND before.surface_session_id=after.surface_session_id "
            "AND after.capture_role='after_action' AND before.capture_role='before_action' "
            "AND after.capture_status='captured' AND before.capture_status='captured' "
            "AND after.parent_capture_id=before.id AND before.parent_capture_id=action.requested_source_capture_id "
            "AND action.result_capture_id=after.id AND action.task_id=after.task_id AND action.session_id=$4 "
            "AND before.action_execution_id=action.action_execution_id "
            "AND action.command_sequence=after.session_command_sequence AND action.command_sequence=before.session_command_sequence "
            "AND action.status='completed' AND action.error_code IS NULL "
            "AND action.finalization_epoch=task.finalization_epoch "
            "AND after.metadata->>'finalization_epoch'=task.finalization_epoch::text "
            "AND before.metadata->>'finalization_epoch'=task.finalization_epoch::text "
            "AND action.intent_payload->>'action_type'='scroll' "
            "AND action.intent_payload #>> '{parameters,web_capture_stage}'='web_primary_content_evidence' "
            "AND action.intent_payload #>> '{parameters,target_mode}'='web_primary_content_candidate'",
            current, task_id, agent_run_id, session_id,
        )
        if row is None or (step == 0 and row["before_id"] != direct_parent_id):
            reject()
        current = row["requested_id"]
        if current == baseline_id and step != scroll_count - 1:
            reject()
    if current != baseline_id:
        reject()


_MARK_PRIMARY_CONTENT_CAPTURE_SQL = """
WITH updated_common AS (
    UPDATE qc_capture AS capture
    SET metadata = COALESCE(capture.metadata, '{}'::jsonb) || $6::jsonb
    WHERE capture.task_id = $1::uuid
      AND capture.agent_run_id = $2::uuid
      AND capture.surface_session_id = $3
      AND capture.surface_kind = 'web'
      AND capture.capture_status = 'captured'
      AND capture.id = $4::uuid
      AND capture.parent_capture_id = $5::uuid
    RETURNING capture.id
)
UPDATE qc_page_capture AS page
SET metadata = COALESCE(page.metadata, '{}'::jsonb) || $6::jsonb,
    updated_at = now()
FROM updated_common
WHERE page.task_id = $1::uuid
  AND page.id = updated_common.id
RETURNING page.id::text AS capture_id
"""


def _validate_web_page_type_assessment_payload(
    *,
    canonical_page_type: str | None,
    audit_metadata: Mapping[str, object],
    assessment_payload: Mapping[str, object] | None,
    expected_schema_version: str = "web-page-type-assessment.v3",
    task_id: str | None = None,
    agent_run_id: str | None = None,
    session_id: str | None = None,
    capture_id: str | None = None,
    expected_catalog_identity: WebPageTypeCatalogIdentity | None = None,
) -> None:
    if not isinstance(audit_metadata, Mapping):
        raise TypeError("Web 页型判定 audit_metadata 必须为对象")
    direct_assessment = audit_metadata.get("web_page_type_assessment")
    if not isinstance(direct_assessment, Mapping):
        raise ValueError("新 Web 页型写入必须使用 web_page_type_assessment.v2")
    if direct_assessment.get("version") != expected_schema_version:
        raise ValueError("Web 页型判定审计版本不受支持")
    if assessment_payload is None or assessment_payload.get("schema_version") != expected_schema_version:
        raise ValueError("Web 页型判定必须带 v3 Assessment")
    allowed_types_by_version = {
        "web-page-type-assessment.v2": {
            "video_list", "video_detail", "content_list", "content_detail",
        },
        "web-page-type-assessment.v3": {
            "home", "video_list", "video_detail", "content_list", "content_detail",
        },
        "web-page-type-assessment.v4": {
            "home", "video_list", "video_detail", "content_list", "content_detail",
            "activity", "search_page", "age_gate", "unknown",
        },
    }
    allowed_types = allowed_types_by_version.get(expected_schema_version)
    if allowed_types is None:
        raise ValueError("Web 页型判定审计版本不受支持")
    if expected_schema_version == "web-page-type-assessment.v4" and expected_catalog_identity:
        allowed_types = set(
            web_page_type_codes_for_catalog_version(
                expected_catalog_identity.catalog_version
            )
        )
    if canonical_page_type is not None:
        if canonical_page_type not in allowed_types:
            raise ValueError("Web 正式页型不在对应版本闭集")
        if expected_schema_version == "web-page-type-assessment.v4":
            expected_group = (
                "lifecycle" if canonical_page_type == "age_gate"
                else "neither" if canonical_page_type == "unknown"
                else "evidence_only" if canonical_page_type in WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES
                else "business"
            )
            if assessment_payload.get("coverage_group") != expected_group:
                raise ValueError("Web v4 Assessment coverage_group 与页型不一致")
        if assessment_payload.get("page_type") != canonical_page_type:
            raise ValueError("Web Assessment 页型与最终页型不一致")
        expected_status = "needs_review" if canonical_page_type == "unknown" else "classified"
        if assessment_payload.get("classification_status") != expected_status:
            raise ValueError("已分类 Web Assessment 状态不一致")
        if expected_schema_version == "web-page-type-assessment.v4":
            expected_detection_status = (
                "needs_review" if canonical_page_type == "unknown" else "recognized"
            )
            if assessment_payload.get("detection_status") != expected_detection_status:
                raise ValueError("Web Assessment detection_status 与页型不一致")
    elif assessment_payload.get("classification_status") != "needs_review":
        raise ValueError("未定案 Web Assessment 必须进入待复核")
    if expected_schema_version == "web-page-type-assessment.v4":
        _validate_v4_web_page_type_assessment_payload(
            canonical_page_type=canonical_page_type,
            audit_metadata=audit_metadata,
            direct_assessment=direct_assessment,
            assessment_payload=assessment_payload,
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture_id=capture_id,
            expected_catalog_identity=expected_catalog_identity,
        )


def _validate_v4_web_page_type_assessment_payload(
    *,
    canonical_page_type: str | None,
    audit_metadata: Mapping[str, object],
    direct_assessment: Mapping[str, object],
    assessment_payload: Mapping[str, object],
    task_id: str | None,
    agent_run_id: str | None,
    session_id: str | None,
    capture_id: str | None,
    expected_catalog_identity: WebPageTypeCatalogIdentity | None,
) -> None:
    """v4 写入必须绑定 Run 冻结目录、归属与完整证据谱系。"""

    required_fields = {
        "assessment_id",
        "task_id",
        "agent_run_id",
        "session_id",
        "page_type",
        "final_page_type",
        "proposed_page_type",
        "raw_page_type",
        "coverage_group",
        "classification_status",
        "detection_status",
        "status",
        "needs_review",
        "confidence",
        "reason_code",
        "reason_codes",
        "assessed_at",
        "page_capture_ids",
        "evidence_artifact_ids",
        "primary_evidence_artifact_ids",
        "supplemental_screenshot_artifact_ids",
        "supplemental_evidence_status",
        "supplemental_evidence_reason_code",
        "page_states",
        "web_capture_stage",
        "page_role",
        "attempt_count",
        "catalog_version",
        "catalog_sha256",
    }
    missing_fields = required_fields - set(assessment_payload)
    if missing_fields:
        raise ValueError(
            "Web v4 Assessment 必填字段缺失：" + ", ".join(sorted(missing_fields))
        )
    allowed_audit_only_fields = {"version", "evidence"}
    missing_payload_projection = set(assessment_payload) - set(direct_assessment)
    if missing_payload_projection:
        raise ValueError(
            "Web v4 Assessment 审计 payload 投影缺失："
            + ", ".join(sorted(missing_payload_projection))
        )
    unexpected_audit_only_fields = (
        set(direct_assessment) - set(assessment_payload) - allowed_audit_only_fields
    )
    if unexpected_audit_only_fields:
        raise ValueError(
            "Web v4 Assessment 存在未允许的 audit-only 字段："
            + ", ".join(sorted(unexpected_audit_only_fields))
        )
    for field_name in set(direct_assessment) & set(assessment_payload):
        if not _strict_json_value_equal(
            direct_assessment[field_name], assessment_payload[field_name]
        ):
            raise ValueError(
                f"Web v4 Assessment 审计 {field_name} 与 payload 不一致"
            )
    if expected_catalog_identity is None:
        raise ValueError("Web v4 Assessment 缺少 Run 冻结 Web Catalog 身份")
    ownership = {
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "session_id": session_id,
    }
    if any(not isinstance(value, str) or not value.strip() for value in ownership.values()):
        raise ValueError("Web v4 Assessment 写入归属缺失")
    for field_name, expected_value in ownership.items():
        if not _strict_json_value_equal(
            assessment_payload.get(field_name), expected_value
        ):
            raise ValueError(f"Web v4 Assessment {field_name} 归属不一致")
    if not isinstance(capture_id, str) or not capture_id.strip():
        raise ValueError("Web v4 Assessment capture_id 缺失")
    if not _strict_json_value_equal(
        assessment_payload.get("page_capture_ids"), [capture_id]
    ):
        raise ValueError("Web v4 Assessment 必须只绑定当前 Capture")
    for field_name in (
        "assessment_id",
        "reason_code",
        "assessed_at",
        "web_capture_stage",
        "page_role",
    ):
        value = assessment_payload.get(field_name)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"Web v4 Assessment {field_name} 无效")
    confidence = assessment_payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0 <= float(confidence) <= 1
    ):
        raise ValueError("Web v4 Assessment confidence 无效")
    attempt_count = assessment_payload.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or not 1 <= attempt_count <= 3
    ):
        raise ValueError("Web v4 Assessment attempt_count 无效")
    _strict_identifier_list(assessment_payload.get("page_states"), "page_states")

    catalog_facts = {
        "catalog_version": expected_catalog_identity.catalog_version,
        "catalog_sha256": expected_catalog_identity.catalog_sha256,
    }
    for field_name, expected_value in catalog_facts.items():
        if (
            not _strict_json_value_equal(
                assessment_payload.get(field_name), expected_value
            )
            or not _strict_json_value_equal(
                direct_assessment.get(field_name), expected_value
            )
        ):
            raise ValueError(f"Web v4 Assessment {field_name} 与冻结目录不一致")

    primary_ids = _strict_identifier_list(
        assessment_payload.get("primary_evidence_artifact_ids"),
        "primary_evidence_artifact_ids",
    )
    supplemental_ids = _strict_identifier_list(
        assessment_payload.get("supplemental_screenshot_artifact_ids"),
        "supplemental_screenshot_artifact_ids",
        allow_empty=True,
    )
    expected_primary_count = 2 if canonical_page_type == "age_gate" else 3
    if len(primary_ids) != expected_primary_count or set(primary_ids) & set(supplemental_ids):
        raise ValueError("Web v4 Assessment 主证据与 supplemental 证据组合无效")
    evidence_ids = _strict_identifier_list(
        assessment_payload.get("evidence_artifact_ids"),
        "evidence_artifact_ids",
    )
    if not _strict_json_value_equal(
        evidence_ids, (*primary_ids, *supplemental_ids)
    ):
        raise ValueError("Web v4 Assessment evidence_artifact_ids 谱系不一致")
    audit_evidence = direct_assessment.get("evidence")
    if (
        not isinstance(audit_evidence, Mapping)
        or not _strict_json_value_equal(
            audit_evidence.get("artifact_ids"), list(evidence_ids)
        )
        or not _strict_json_value_equal(
            direct_assessment.get("primary_evidence_artifact_ids"),
            list(primary_ids),
        )
        or not _strict_json_value_equal(
            direct_assessment.get("supplemental_screenshot_artifact_ids"),
            list(supplemental_ids),
        )
    ):
        raise ValueError("Web v4 Assessment 审计证据谱系不一致")

    supplemental_status = assessment_payload.get("supplemental_evidence_status")
    supplemental_reason = assessment_payload.get("supplemental_evidence_reason_code")
    if supplemental_status == "captured":
        supplemental_valid = bool(supplemental_ids) and supplemental_reason is None
    elif supplemental_status == "failed":
        supplemental_valid = (
            not supplemental_ids
            and isinstance(supplemental_reason, str)
            and bool(supplemental_reason.strip())
        )
    elif supplemental_status == "not_attempted":
        supplemental_valid = not supplemental_ids and supplemental_reason is None
    else:
        supplemental_valid = False
    if (
        not supplemental_valid
        or not _strict_json_value_equal(
            direct_assessment.get("supplemental_evidence_status"),
            supplemental_status,
        )
        or not _strict_json_value_equal(
            direct_assessment.get("supplemental_evidence_reason_code"),
            supplemental_reason,
        )
    ):
        raise ValueError("Web v4 Assessment supplemental 状态组合无效")

    page_type = assessment_payload.get("page_type")
    allowed_page_types = set(
        web_page_type_codes_for_catalog_version(
            expected_catalog_identity.catalog_version
        )
    )
    if page_type not in {*allowed_page_types, "__unclassified__"}:
        raise ValueError("Web v4 Assessment page_type 不在闭集")
    if page_type == "unknown" and canonical_page_type != "unknown":
        raise ValueError("Web v4 unknown 必须作为正式待复核页型持久化")
    if page_type == "age_gate" and canonical_page_type != "age_gate":
        raise ValueError("Web v4 age_gate 必须作为生命周期页型持久化")
    if canonical_page_type == "age_gate":
        expected = ("lifecycle", "covered", "classified", "recognized", False)
    elif canonical_page_type == "unknown":
        expected = ("neither", "evidence_insufficient", "needs_review", "needs_review", True)
    elif canonical_page_type in WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES:
        expected = ("evidence_only", "covered", "classified", "recognized", False)
    elif canonical_page_type is not None:
        expected = ("business", "covered", "classified", "recognized", False)
    else:
        expected_group = (
            "business" if page_type in WEB_BUSINESS_PAGE_TYPE_CODES
            else "evidence_only" if page_type in WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES
            else "neither"
        )
        expected = (
            expected_group,
            "evidence_insufficient",
            "needs_review",
            "needs_review",
            True,
        )
    actual = (
        assessment_payload.get("coverage_group"),
        assessment_payload.get("status"),
        assessment_payload.get("classification_status"),
        assessment_payload.get("detection_status"),
        assessment_payload.get("needs_review"),
    )
    if not _strict_json_value_equal(actual, expected):
        raise ValueError("Web v4 Assessment 覆盖、状态与页型组合不一致")
    if not _strict_json_value_equal(
        assessment_payload.get("final_page_type"), canonical_page_type
    ):
        raise ValueError("Web v4 Assessment final_page_type 与正式页型不一致")
    _validate_v4_age_gate_lifecycle_projection(
        canonical_page_type=canonical_page_type,
        audit_metadata=audit_metadata,
        direct_assessment=direct_assessment,
        assessment_payload=assessment_payload,
        capture_id=capture_id,
    )


def _validate_v4_age_gate_lifecycle_projection(
    *,
    canonical_page_type: str | None,
    audit_metadata: Mapping[str, object],
    direct_assessment: Mapping[str, object],
    assessment_payload: Mapping[str, object],
    capture_id: str,
) -> None:
    """年龄门写入发生于动作前；三份投影必须描述同一初始生命周期。"""

    lifecycle = audit_metadata.get("web_age_gate_lifecycle")
    if canonical_page_type != "age_gate":
        if lifecycle is not None:
            raise ValueError("非 age_gate Assessment 禁止携带 lifecycle 投影")
        return
    if not isinstance(lifecycle, Mapping):
        raise ValueError("Web v4 age_gate lifecycle 投影缺失")
    required_fields = {
        "pre_gate_capture_id",
        "gate_present",
        "dismiss_action_id",
        "clicked_candidate_id",
        "post_gate_capture_id",
        "gate_disappeared",
        "supplemental_evidence_status",
        "supplemental_evidence_reason_code",
    }
    missing_fields = required_fields - set(lifecycle)
    if missing_fields:
        raise ValueError(
            "Web v4 age_gate lifecycle 必需投影缺失："
            + ", ".join(sorted(missing_fields))
        )
    unexpected_fields = set(lifecycle) - required_fields
    if unexpected_fields:
        raise ValueError(
            "Web v4 age_gate lifecycle 存在未允许字段："
            + ", ".join(sorted(unexpected_fields))
        )
    for field_name in required_fields:
        if (
            field_name not in assessment_payload
            or field_name not in direct_assessment
            or not _strict_json_value_equal(
                lifecycle[field_name], assessment_payload[field_name]
            )
            or not _strict_json_value_equal(
                lifecycle[field_name], direct_assessment[field_name]
            )
        ):
            raise ValueError(
                f"Web v4 age_gate lifecycle {field_name} 投影不一致"
            )
    expected_initial_state = {
        "pre_gate_capture_id": capture_id,
        "gate_present": True,
        "dismiss_action_id": None,
        "clicked_candidate_id": None,
        "post_gate_capture_id": None,
        "gate_disappeared": False,
    }
    if any(
        not _strict_json_value_equal(lifecycle[field_name], value)
        for field_name, value in expected_initial_state.items()
    ):
        raise ValueError("Web v4 age_gate lifecycle 动作前状态不一致")


def _strict_json_value_equal(left: object, right: object) -> bool:
    """按 JSON 类型和值比较；对象无序，数组有序，bool 不等同于 number。"""

    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if any(not isinstance(key, str) for key in (*left.keys(), *right.keys())):
            return False
        if set(left) != set(right):
            return False
        return all(
            _strict_json_value_equal(left[key], right[key])
            for key in left
        )
    if isinstance(left, list | tuple) or isinstance(right, list | tuple):
        if type(left) is not type(right) or len(left) != len(right):
            return False
        return all(
            _strict_json_value_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def _strict_identifier_list(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in value
    ):
        raise ValueError(f"Web v4 Assessment {field_name} 无效")
    normalized = tuple(value)
    if len(set(normalized)) != len(normalized) or (not allow_empty and not normalized):
        raise ValueError(f"Web v4 Assessment {field_name} 无效")
    return normalized


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_rows(value: object) -> list[dict[str, object]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _json_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): item for key, item in parsed.items()}
    return {}


def _web_redirect_chain(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    chain = tuple(str(item).strip() for item in value if str(item).strip())
    return chain if len(chain) >= 2 else ()


def _payload_hash(payload: Mapping[str, object]) -> str:
    return sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _web_assessment_record_id(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture_id: str,
    schema_version: str = "web-page-type-assessment.v3",
) -> str:
    source = _json_dumps(
        {
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "session_id": session_id,
            "capture_id": capture_id,
            "schema_version": schema_version,
        }
    )
    return f"web-page-type-assessment:{sha256(source.encode('utf-8')).hexdigest()}"


def _captured_at(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("WEB_CAPTURE_CAPTURED_AT_MISSING")
    return value.astimezone(UTC)


def _required_row_text(row: Any, field_name: str) -> str:
    value = _optional_row_text(row, field_name)
    if value is None:
        raise RuntimeError(f"Web 通知证据缺少 {field_name}")
    return value


def _optional_row_text(row: Any, field_name: str) -> str | None:
    value = str(row.get(field_name) or "").strip()
    return value or None


def _optional_row_int(row: Any, field_name: str) -> int | None:
    value = row.get(field_name)
    return int(value) if value is not None else None


def _row_text_tuple(row: Any, field_name: str) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (row.get(field_name) or ()) if str(item).strip()})
    )
