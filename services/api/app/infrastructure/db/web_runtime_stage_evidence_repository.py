"""从现有 Surface Phase 与 Capture 读取阶段完成证据，不存储页面正文。"""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    WebStageCheckpointConflict,
)


class PostgresWebRuntimeStageEvidenceRepository:
    def __init__(self, *, task_id, run_id, session_id, pool=None):
        if session_id != f"web:{run_id}":
            raise ValueError("阶段 Session 必须属于当前 Run")
        self._task_id = task_id
        self._run_id = run_id
        self._session_id = session_id
        self._pool = pool

    async def read_epoch(self):
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT task.finalization_epoch FROM qc_task task
                   JOIN qc_agent_run run ON run.task_id=task.id
                   WHERE task.id=$1::uuid AND run.id=$2::uuid AND task.status='running'
                     AND run.status='running' AND task.target_platform='web'
                     AND task.finalization_state <> 'sealed'
                     AND COALESCE(run.output_context->>'cancel_requested','false')='false'""",
                self._task_id,
                self._run_id,
            )
        if row is None:
            raise WebStageCheckpointConflict("阶段初始化所需 Run 已失效")
        return row["finalization_epoch"]

    async def assert_active(self, cursor):
        if (
            cursor.run_id != self._run_id
            or cursor.cursor_payload.get("session_id") != self._session_id
        ):
            raise WebStageCheckpointConflict("阶段执行身份不一致")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT checkpoint.run_id FROM qc_web_run_stage_checkpoint checkpoint
                   JOIN qc_agent_run run ON run.id=checkpoint.run_id
                   JOIN qc_task task ON task.id=run.task_id
                   WHERE task.id=$1::uuid AND run.id=$2::uuid
                     AND task.status='running' AND run.status='running'
                     AND task.finalization_state <> 'sealed'
                     AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                     AND task.finalization_epoch=$3
                     AND checkpoint.logical_page_key=$4 AND checkpoint.row_version=$5
                     AND checkpoint.claim_token=$6::uuid AND checkpoint.stage_status='running'
                     AND checkpoint.lease_expires_at>clock_timestamp()""",
                self._task_id,
                self._run_id,
                cursor.cursor_payload.get("finalization_epoch"),
                cursor.logical_page_key,
                cursor.row_version,
                cursor.claim_token,
            )
        if row is None:
            raise WebStageCheckpointConflict("阶段租约、运行或 epoch 已失效")

    async def find_completed(self, cursor, *, operation=None):
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT phase.source_capture_id,phase.context_payload,phase.evidence_artifact_ids
                   FROM qc_surface_phase_run phase
                   JOIN qc_capture capture ON capture.id=phase.source_capture_id
                   JOIN qc_surface_session session ON session.task_id=phase.task_id
                     AND session.session_id=phase.session_id
                   WHERE phase.task_id=$1::uuid AND phase.session_id=$2
                     AND phase.phase_key=$3 AND phase.idempotency_key=$4
                     AND phase.phase_protocol_version='web-run-stage-evidence.v1'
                     AND phase.status='completed' AND phase.finalization_epoch=$5
                     AND capture.agent_run_id=$6::uuid AND capture.task_id=phase.task_id
                     AND capture.surface_session_id=phase.session_id
                     AND capture.surface_kind='web' AND capture.capture_status='captured'
                     AND capture.metadata->>'finalization_epoch'=phase.finalization_epoch::text
                     AND session.finalization_epoch=phase.finalization_epoch
                     AND session.session_owner='web-v2-workflow'""",
                self._task_id,
                self._session_id,
                f"web_stage_{cursor.current_stage}",
                self._effect_key(cursor, operation),
                cursor.cursor_payload.get("finalization_epoch"),
                self._run_id,
            )
        if row is None:
            return None
        payload = row["context_payload"]
        artifacts = row["evidence_artifact_ids"]
        payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
        artifacts = json.loads(artifacts) if isinstance(artifacts, str) else artifacts
        if cursor.current_stage.value == "classification" and operation in {None, "classification"}:
            expected_capture_id = cursor.cursor_payload.get("capture_id")
            if (
                str(row["source_capture_id"]) != expected_capture_id
                or payload.get("capture_id") != expected_capture_id
            ):
                raise WebStageFailure("WEB_CLASSIFICATION_CAPTURE_MISMATCH")
        authority = {
            key: payload.pop(key)
            for key in ("authoritative_capture_id", "authoritative_page_type")
            if key in payload
        }
        reuse = {key: payload.pop(key) for key in (
            "reused_capture_id", "reused_logical_page_id"
        ) if key in payload}
        identity_proof = {key: payload.pop(key) for key in (
            "reused_source_url_sha256", "reused_observed_url_sha256"
        ) if key in payload}
        if reuse and not (
            cursor.current_stage.value == "classification" and operation in {None, "classification"}
        ):
            raise ValueError("分类复用引用只允许分类完成证据")
        optional_primary_error_code = payload.pop("optional_primary_error_code", None)
        if optional_primary_error_code is not None and not (
            (cursor.current_stage.value == "classification" and operation is None)
            or (cursor.current_stage.value == "candidate_navigation" and operation == "detail")
        ):
            raise ValueError("可选正文诊断只允许分类阶段完成证据")
        if authority and operation != "detail":
            raise ValueError("权威详情事实只允许 detail 操作证据")
        # 复用领域载荷校验，损坏的 Surface Phase 不能把 DOM 或凭据带回游标。
        validated = replace(cursor, cursor_payload=payload, artifact_ids=tuple(artifacts))
        result = WebStageEffectResult(
            artifact_ids=validated.artifact_ids,
            cursor_payload=validated.cursor_payload,
            optional_primary_error_code=optional_primary_error_code,
            **authority,
            **reuse,
        )
        if result.reused_capture_id is not None:
            from services.api.app.application.web_stage_classification_reuse import (
                validate_reused_classification,
            )

            facts = await validate_reused_classification(self, cursor, result)
            if any(facts.get(key) != value for key, value in identity_proof.items()):
                raise WebStageFailure("WEB_CLASSIFICATION_REUSE_INVALID")
        await self._assert_detail_authority(result)
        return result

    async def _assert_detail_authority(self, outcome):
        if outcome.authoritative_capture_id is None:
            return
        facts = await self.capture_outcome(capture_id=outcome.authoritative_capture_id)
        if facts["page_type"] != outcome.authoritative_page_type:
            raise WebStageCheckpointConflict("详情权威类型与持久 Capture 不一致")

    async def capture_reference(self, *, capture_id):
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT capture.id,page.url FROM qc_capture capture
                   JOIN qc_page_capture page ON page.id=capture.id
                     AND page.task_id=capture.task_id AND page.agent_run_id=capture.agent_run_id
                   JOIN qc_surface_session session ON session.task_id=capture.task_id
                     AND session.session_id=capture.surface_session_id
                   JOIN qc_task task ON task.id=capture.task_id
                   WHERE capture.id=$1::uuid AND capture.task_id=$2::uuid
                     AND capture.agent_run_id=$3::uuid AND capture.surface_session_id=$4
                     AND capture.surface_kind='web' AND capture.capture_status='captured'
                     AND page.capture_status='captured' AND session.current_capture_id=capture.id
                     AND session.session_owner='web-v2-workflow' AND session.status='active'
                     AND session.finalization_epoch=task.finalization_epoch
                     AND capture.metadata->>'finalization_epoch'=session.finalization_epoch::text
                     AND page.metadata->>'finalization_epoch'=session.finalization_epoch::text""",
                capture_id,
                self._task_id,
                self._run_id,
                self._session_id,
            )
        if row is None:
            raise WebStageCheckpointConflict("Capture 不是当前 Session/epoch 的可信引用")
        return {"capture_id": str(row["id"]), "current_url": row["url"]}

    async def current_capture_reference(self):
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT current_capture_id FROM qc_surface_session
                   WHERE task_id=$1::uuid AND session_id=$2 AND status='active'
                     AND session_owner='web-v2-workflow'""",
                self._task_id,
                self._session_id,
            )
        if row is None or row["current_capture_id"] is None:
            raise WebStageCheckpointConflict("当前 Session 缺少可信动作来源")
        return await self.capture_reference(capture_id=str(row["current_capture_id"]))

    async def current_menu_capture_after_primary(
        self, *, site_id, menu_entry_id, menu_action_execution_id,
        menu_probe_capture_id, menu_ready_capture_id,
    ):
        """仅把同菜单 ready 后的已完成主体滚动帧提升为当前终态引用。"""
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """WITH RECURSIVE scope AS (
                     SELECT ledger.agent_run_id, ledger.menu_entry_id,
                            session.finalization_epoch AS epoch,
                            session.current_capture_id,
                            probe.id AS probe_id, ready.id AS ready_id,
                            ready_page.url AS ready_url
                     FROM qc_web_menu_ledger_entry ledger
                     JOIN qc_web_exploration_budget budget
                       ON budget.agent_run_id=ledger.agent_run_id
                      AND budget.site_id=ledger.site_id
                     JOIN qc_task task ON task.id=budget.task_id
                     JOIN qc_surface_session session
                       ON session.task_id=task.id AND session.session_id=budget.session_id
                     JOIN qc_action_execution menu_action
                       ON menu_action.action_execution_id=$6
                      AND menu_action.task_id=task.id
                      AND menu_action.session_id=session.session_id
                      AND menu_action.intent_id=ledger.menu_intent_id
                      AND menu_action.requested_source_capture_id=ledger.menu_source_capture_id
                      AND menu_action.status='completed'
                      AND menu_action.finalization_epoch=session.finalization_epoch
                     JOIN qc_capture probe ON probe.id=$7::uuid
                      AND probe.id=menu_action.result_capture_id
                      AND probe.action_execution_id=menu_action.action_execution_id
                      AND probe.capture_role='after_action'
                      AND probe.capture_status='captured'
                      AND probe.task_id=task.id
                      AND probe.agent_run_id=ledger.agent_run_id
                      AND probe.surface_session_id=session.session_id
                     JOIN qc_capture ready ON ready.id=$8::uuid
                      AND ready.capture_status='captured'
                      AND ready.task_id=task.id
                      AND ready.agent_run_id=ledger.agent_run_id
                      AND ready.surface_session_id=session.session_id
                     JOIN qc_page_capture ready_page ON ready_page.id=ready.id
                      AND ready_page.task_id=task.id
                      AND ready_page.agent_run_id=ledger.agent_run_id
                      AND ready_page.capture_status='captured'
                     JOIN qc_h5_page_type_assessment assessment
                       ON assessment.page_capture_id=ready.id
                      AND assessment.task_id=task.id
                      AND assessment.agent_run_id=ledger.agent_run_id
                      AND assessment.session_id=session.session_id
                      AND assessment.detection_status='recognized'
                      AND assessment.page_type=ready_page.page_type
                     WHERE task.id=$1::uuid AND ledger.agent_run_id=$2::uuid
                       AND session.session_id=$3 AND ledger.site_id=$4
                       AND ledger.menu_entry_id=$5
                       AND ledger.status IN ('menu_reserved','detail_reserved')
                       AND session.status='active'
                       AND session.session_owner='web-v2-workflow'
                       AND session.finalization_epoch=task.finalization_epoch
                       AND ready_page.page_type IN (
                           'content_list','video_list','collection_page',
                           'content_detail','video_detail'
                       )
                       AND probe.metadata->>'finalization_epoch'
                           =session.finalization_epoch::text
                       AND ready.metadata->>'finalization_epoch'
                           =session.finalization_epoch::text
                       AND ready_page.metadata->>'finalization_epoch'
                           =session.finalization_epoch::text
                     LIMIT 1
                   ), valid_step AS (
                     SELECT action.requested_source_capture_id AS source_id,
                            after_capture.id AS result_id, action.intent_id,
                            after_page.url AS result_url
                     FROM scope
                     JOIN qc_action_execution action ON action.task_id=$1::uuid
                       AND action.session_id=$3 AND action.status='completed'
                       AND action.finalization_epoch=scope.epoch
                     JOIN qc_capture before_capture
                       ON before_capture.id=action.source_capture_id
                      AND before_capture.parent_capture_id=action.requested_source_capture_id
                      AND before_capture.action_execution_id=action.action_execution_id
                      AND before_capture.capture_role='before_action'
                      AND before_capture.capture_status='captured'
                      AND before_capture.task_id=$1::uuid
                      AND before_capture.agent_run_id=$2::uuid
                      AND before_capture.surface_session_id=$3
                     JOIN qc_capture after_capture
                       ON after_capture.id=action.result_capture_id
                      AND after_capture.parent_capture_id=before_capture.id
                      AND after_capture.action_execution_id=action.action_execution_id
                      AND after_capture.capture_role='after_action'
                      AND after_capture.capture_status='captured'
                      AND after_capture.task_id=$1::uuid
                      AND after_capture.agent_run_id=$2::uuid
                      AND after_capture.surface_session_id=$3
                     JOIN qc_page_capture after_page ON after_page.id=after_capture.id
                       AND after_page.task_id=$1::uuid
                       AND after_page.agent_run_id=$2::uuid
                       AND after_page.capture_status='captured'
                     WHERE before_capture.metadata->>'finalization_epoch'=scope.epoch::text
                       AND after_capture.metadata->>'finalization_epoch'=scope.epoch::text
                       AND after_page.metadata->>'finalization_epoch'=scope.epoch::text
                   ), menu_path(capture_id, depth) AS (
                     SELECT probe_id, 0 FROM scope
                     UNION ALL
                     SELECT step.result_id, path.depth+1
                     FROM menu_path path
                     JOIN valid_step step ON step.source_id=path.capture_id
                       AND step.intent_id LIKE 'web-readiness:%'
                     WHERE path.depth<8
                   ), primary_path(capture_id, depth) AS (
                     SELECT scope.ready_id, 0 FROM scope
                     WHERE EXISTS (
                       SELECT 1 FROM menu_path WHERE capture_id=scope.ready_id
                     )
                     UNION ALL
                     SELECT step.result_id, path.depth+1
                     FROM primary_path path
                     JOIN valid_step step ON step.source_id=path.capture_id
                     JOIN scope ON true
                     WHERE path.depth<2
                       AND step.intent_id IN (
                         'web-primary-content:' || path.capture_id::text || ':1',
                         'web-primary-content:' || path.capture_id::text || ':2'
                       )
                       AND step.result_url=scope.ready_url
                   )
                   SELECT current.id
                   FROM scope
                   JOIN primary_path path ON path.capture_id=scope.current_capture_id
                   JOIN qc_capture current ON current.id=path.capture_id
                     AND current.task_id=$1::uuid
                     AND current.agent_run_id=$2::uuid
                     AND current.surface_session_id=$3
                     AND current.capture_status='captured'
                   JOIN qc_page_capture current_page ON current_page.id=current.id
                     AND current_page.task_id=$1::uuid
                     AND current_page.agent_run_id=$2::uuid
                     AND current_page.capture_status='captured'
                   WHERE current_page.url=scope.ready_url
                     AND current.metadata->>'finalization_epoch'=scope.epoch::text
                     AND current_page.metadata->>'finalization_epoch'=scope.epoch::text
                   LIMIT 1""",
                self._task_id, self._run_id, self._session_id, site_id,
                menu_entry_id, menu_action_execution_id,
                menu_probe_capture_id, menu_ready_capture_id,
            )
        if row is None:
            raise WebStageCheckpointConflict("菜单主体补证当前帧血缘无效")
        return str(row["id"])

    async def find_navigation(self, cursor):
        """恢复完成动作与当前帧，覆盖 Capture 已提交但阶段标记未提交的中断窗口。"""
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT capture.id,page.url FROM qc_action_execution action
                   JOIN qc_capture capture ON capture.action_execution_id=action.action_execution_id
                     AND capture.task_id=action.task_id
                     AND capture.surface_session_id=action.session_id
                     AND capture.capture_role='after_action' AND capture.capture_status='captured'
                   JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=capture.task_id
                     AND page.agent_run_id=capture.agent_run_id AND page.capture_status='captured'
                   JOIN qc_surface_session session ON session.task_id=action.task_id
                     AND session.session_id=action.session_id AND session.status='active'
                     AND session.current_capture_id=capture.id
                     AND session.session_owner='web-v2-workflow'
                   WHERE action.task_id=$1::uuid AND action.session_id=$2
                     AND capture.agent_run_id=$3::uuid AND action.status='completed'
                     AND action.finalization_epoch=$4 AND session.finalization_epoch=$4
                     AND capture.metadata->>'finalization_epoch'=$4::text
                     AND page.metadata->>'finalization_epoch'=$4::text
                     AND (( $5::text IS NULL AND action.intent_id=$6)
                          OR ($5::text IS NOT NULL AND EXISTS (
                            SELECT 1 FROM qc_web_menu_ledger_entry ledger
                            WHERE ledger.agent_run_id=$3::uuid AND ledger.menu_entry_id=$5
                              AND ledger.menu_intent_id=action.intent_id)))
                   ORDER BY capture.created_at DESC LIMIT 1""",
                self._task_id,
                self._session_id,
                self._run_id,
                cursor.cursor_payload["finalization_epoch"],
                cursor.cursor_payload.get("menu_ledger_id"),
                (
                    f"web-auxiliary:{self._run_id}:{cursor.cursor_payload['idempotency_key']}"
                    if cursor.logical_page_key in {"aux:search", "aux:activity"}
                    else f"web-collection:{self._run_id}:{cursor.cursor_payload['idempotency_key']}"
                    if cursor.logical_page_key.startswith(("collection:", "collection-detail:"))
                    else f"web-bootstrap:{self._run_id}"
                ),
            )
        if row is None:
            return None
        return WebStageEffectResult(
            cursor_payload={
                "capture_id": str(row["id"]), "current_url": row["url"],
                **({"menu_navigation_capture_id": str(row["id"])}
                   if cursor.cursor_payload.get("menu_ledger_id") else {}),
            }
        )

    async def capture_outcome(self, *, capture_id):
        """对账读取历史同 Run 的可信帧；不据此授予新的浏览器动作。"""
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT COALESCE(page.page_type,parent_page.page_type) AS page_type,
                          capture.action_execution_id FROM qc_capture capture
                   JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=capture.task_id
                      AND page.agent_run_id=capture.agent_run_id
                   LEFT JOIN qc_page_capture parent_page
                     ON parent_page.id::text=(
                          page.metadata #>> '{primary_content_capture,parent_capture_id}'
                        )
                    AND parent_page.task_id=page.task_id
                    AND parent_page.agent_run_id=page.agent_run_id
                    AND parent_page.capture_status='captured'
                   JOIN qc_surface_session session ON session.session_id=capture.surface_session_id
                     AND session.task_id=capture.task_id
                   JOIN qc_task task ON task.id=capture.task_id
                   WHERE capture.id=$1::uuid AND capture.task_id=$2::uuid
                     AND capture.agent_run_id=$3::uuid AND capture.surface_session_id=$4
                     AND capture.surface_kind='web' AND capture.capture_status='captured'
                     AND page.capture_status='captured'
                     AND session.finalization_epoch=task.finalization_epoch
                     AND capture.metadata->>'finalization_epoch'=task.finalization_epoch::text
                     AND page.metadata->>'finalization_epoch'=task.finalization_epoch::text""",
                capture_id,
                self._task_id,
                self._run_id,
                self._session_id,
            )
        if row is None:
            raise WebStageCheckpointConflict("阶段对账 Capture 身份不完整")
        return dict(row)

    async def has_durable_page_type_assessment(self, *, capture_id, finalization_epoch):
        """同源 Capture 的正式评估与审计元数据落库后才承认分类完成。"""
        return await self.read_durable_page_type_assessment(
            capture_id=capture_id, finalization_epoch=finalization_epoch
        ) is not None

    async def read_reused_page_type_assessment(self, **kwargs):
        from services.api.app.infrastructure.db.web_classification_reuse import (
            read_reused_classification,
        )

        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            return await read_reused_classification(
                connection, task_id=self._task_id, run_id=self._run_id,
                session_id=self._session_id, **kwargs,
            )

    async def read_durable_page_type_assessment(self, *, capture_id, finalization_epoch):
        """读取同帧已提交的正式最终页型，允许合法未知页型和历史非当前帧。"""
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT assessment.assessment_payload->>'final_page_type' AS page_type
                     FROM qc_capture capture
                     JOIN qc_page_capture page ON page.id=capture.id
                       AND page.task_id=capture.task_id
                       AND page.agent_run_id=capture.agent_run_id
                     JOIN qc_task task ON task.id=capture.task_id
                     JOIN qc_surface_session session ON session.task_id=task.id
                       AND session.session_id=capture.surface_session_id
                     JOIN qc_h5_page_type_assessment assessment
                       ON assessment.page_capture_id=page.id
                       AND assessment.task_id=task.id
                       AND assessment.agent_run_id=capture.agent_run_id
                       AND assessment.session_id=session.session_id
                     WHERE capture.id=$1::uuid AND task.id=$2::uuid
                       AND capture.agent_run_id=$3::uuid
                       AND session.session_id=$4
                       AND task.finalization_epoch=$5
                       AND session.finalization_epoch=$5
                       AND capture.surface_kind='web'
                       AND capture.capture_status='captured'
                       AND page.capture_status='captured'
                       AND session.session_owner='web-v2-workflow'
                       AND capture.metadata->>'finalization_epoch'=$5::text
                       AND page.metadata->>'finalization_epoch'=$5::text
                       AND page.metadata #>> '{web_page_type_assessment,version}'
                           =assessment.schema_version
                       AND page.metadata #>> '{web_page_type_assessment,assessment_id}'
                           =assessment.assessment_id
                       AND assessment.assessment_payload->>'schema_version'
                           =assessment.schema_version
                       AND assessment.assessment_payload->'page_capture_ids'
                           @> jsonb_build_array(capture.id::text)
                   LIMIT 1""",
                capture_id,
                self._task_id,
                self._run_id,
                self._session_id,
                finalization_epoch,
            )
        return dict(row) if row is not None else None

    async def session_closed(self, *, session_id):
        if session_id != self._session_id:
            raise WebStageCheckpointConflict("关闭 Session 归属不一致")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT session.status FROM qc_surface_session session
                   JOIN qc_task task ON task.id=session.task_id
                   JOIN qc_agent_run run ON run.task_id=task.id
                   WHERE task.id=$1::uuid AND run.id=$2::uuid AND session.session_id=$3
                     AND session.session_owner='web-v2-workflow'
                     AND session.finalization_epoch=task.finalization_epoch""",
                self._task_id,
                self._run_id,
                session_id,
            )
        return row is not None and row["status"] == "closed"

    async def report_captures(self):
        """报告从持久阶段和辅助 Phase 补齐正文、详情、辅助页，不依赖进程内列表。"""
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT DISTINCT capture.id AS capture_id,page.page_type,capture.created_at
                   FROM qc_surface_phase_run phase
                   JOIN qc_capture capture ON (
                     (phase.phase_protocol_version='web-run-stage-evidence.v1'
                      AND capture.id=phase.source_capture_id)
                     OR (phase.phase_protocol_version='web-auxiliary-navigation.v1'
                      AND capture.id::text=phase.context_payload->>'result_capture_id'))
                   JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=capture.task_id
                   JOIN qc_task task ON task.id=phase.task_id
                   WHERE phase.task_id=$1::uuid AND phase.session_id=$2 AND phase.status='completed'
                     AND capture.task_id=phase.task_id AND capture.agent_run_id=$3::uuid
                     AND capture.surface_session_id=phase.session_id
                     AND capture.capture_status='captured' AND page.capture_status='captured'
                     AND capture.metadata->>'finalization_epoch'=task.finalization_epoch::text
                     AND phase.finalization_epoch=task.finalization_epoch
                   ORDER BY capture.created_at,capture.id""",
                self._task_id,
                self._session_id,
                self._run_id,
            )
        return tuple((str(row["capture_id"]), row["page_type"]) for row in rows)

    async def save_completed(self, cursor, outcome, *, operation=None):
        # 完成标记与 checkpoint 共享 fencing；浏览器执行期间不保持此事务。
        await self.assert_active(cursor)
        payload = {**cursor.cursor_payload, **outcome.cursor_payload}
        validated = replace(cursor, cursor_payload=payload, artifact_ids=outcome.artifact_ids)
        capture_id = validated.cursor_payload.get("capture_id")
        if capture_id is None:
            # 根导航无任何 Capture 时只有失败；关闭真相由 Session 自身承载。
            return
        context = dict(validated.cursor_payload)
        if outcome.reused_capture_id is not None:
            if cursor.current_stage.value != "classification" or operation not in {
                None, "classification",
            }:
                raise ValueError("分类复用引用只允许分类完成证据")
            from services.api.app.application.web_stage_classification_reuse import (
                validate_reused_classification,
            )

            facts = await validate_reused_classification(self, cursor, outcome)
            context.update(reused_capture_id=outcome.reused_capture_id,
                           reused_logical_page_id=outcome.reused_logical_page_id)
            # SQL 对账只绑定已按冻结策略认证的 URL，不另实现一套地址归一化规则。
            context.update({key: facts[key] for key in (
                "reused_source_url_sha256", "reused_observed_url_sha256"
            )})
        if outcome.optional_primary_error_code is not None:
            if not (
                (cursor.current_stage.value == "classification" and operation is None)
                or (cursor.current_stage.value == "candidate_navigation" and operation == "detail")
            ):
                raise ValueError("可选正文诊断只允许分类阶段完成证据")
            context["optional_primary_error_code"] = outcome.optional_primary_error_code
        if outcome.authoritative_capture_id is not None:
            if operation != "detail":
                raise ValueError("权威详情事实只允许 detail 操作证据")
            await self._assert_detail_authority(outcome)
            context.update(
                authoritative_capture_id=outcome.authoritative_capture_id,
                authoritative_page_type=outcome.authoritative_page_type,
            )
        encoded = json.dumps(context, sort_keys=True)
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            # 与 checkpoint 相同的非主键更新锁，兼容证据外键检查；事务不覆盖浏览器 I/O。
            active = await connection.fetchrow(
                """SELECT task.finalization_epoch FROM qc_agent_run run
                   JOIN qc_task task ON task.id=run.task_id
                   WHERE run.id=$1::uuid AND task.id=$2::uuid AND run.status='running'
                     AND task.status='running' AND task.finalization_state <> 'sealed'
                     AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                   FOR NO KEY UPDATE OF run,task""",
                self._run_id,
                self._task_id,
            )
            if active is None or active["finalization_epoch"] != payload["finalization_epoch"]:
                raise WebStageCheckpointConflict("阶段完成前 Run 或 epoch 已变化")
            locked = await connection.fetchrow(
                """SELECT run_id FROM qc_web_run_stage_checkpoint
                   WHERE run_id=$1::uuid AND logical_page_key=$2 FOR UPDATE""",
                self._run_id,
                cursor.logical_page_key,
            )
            if locked is None:
                raise WebStageCheckpointConflict("阶段完成前 checkpoint 已不存在")
            row = await connection.fetchrow(
                """INSERT INTO qc_surface_phase_run (
                     phase_run_id,task_id,session_id,surface_kind,phase_key,
                     phase_protocol_version,idempotency_key,source_capture_id,status,
                     command_sequence,finalization_epoch,context_payload,context_payload_hash,
                     evidence_artifact_ids,terminal_reason_code,finished_at)
                   SELECT $1,task.id,session.session_id,'web',$2,'web-run-stage-evidence.v1',
                     $3,capture.id,'completed',GREATEST(1,session.command_sequence),
                     task.finalization_epoch,$4::jsonb,$5,$6::jsonb,'WEB_STAGE_COMPLETED',now()
                   FROM qc_web_run_stage_checkpoint checkpoint
                   JOIN qc_agent_run run ON run.id=checkpoint.run_id
                   JOIN qc_task task ON task.id=run.task_id
                   JOIN qc_surface_session session ON session.task_id=task.id
                     AND session.session_id=$7
                   JOIN qc_capture capture ON capture.id=$8::uuid AND capture.task_id=task.id
                     AND capture.agent_run_id=run.id
                     AND capture.surface_session_id=session.session_id
                     AND capture.capture_status='captured' AND capture.surface_kind='web'
                     AND capture.metadata->>'finalization_epoch'=task.finalization_epoch::text
                   WHERE task.id=$9::uuid AND run.id=$10::uuid AND task.status='running'
                     AND run.status='running' AND task.finalization_state <> 'sealed'
                     AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                     AND task.finalization_epoch=$11 AND session.finalization_epoch=$11
                     AND session.session_owner='web-v2-workflow'
                     AND checkpoint.logical_page_key=$12 AND checkpoint.row_version=$13
                     AND checkpoint.claim_token=$14::uuid AND checkpoint.stage_status='running'
                     AND checkpoint.lease_expires_at>clock_timestamp()
                     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text($6::jsonb) wanted(id)
                       WHERE NOT EXISTS (SELECT 1 FROM qc_evidence_artifact artifact
                         JOIN qc_capture owner ON owner.id=artifact.page_capture_id
                         WHERE artifact.id=wanted.id::uuid AND artifact.task_id=task.id
                           AND owner.agent_run_id=run.id
                           AND owner.surface_session_id=session.session_id
                           AND owner.metadata->>'finalization_epoch'=task.finalization_epoch::text))
                   ON CONFLICT (task_id,session_id,phase_key,idempotency_key) DO UPDATE
                     SET updated_at=qc_surface_phase_run.updated_at
                     WHERE qc_surface_phase_run.context_payload_hash=EXCLUDED.context_payload_hash
                   RETURNING phase_run_id""",
                "web-stage-effect:"
                + sha256(self._effect_key(cursor, operation).encode()).hexdigest(),
                f"web_stage_{cursor.current_stage}",
                self._effect_key(cursor, operation),
                encoded,
                sha256(encoded.encode()).hexdigest(),
                json.dumps(list(validated.artifact_ids)),
                self._session_id,
                capture_id,
                self._task_id,
                self._run_id,
                cursor.cursor_payload.get("finalization_epoch"),
                cursor.logical_page_key,
                cursor.row_version,
                cursor.claim_token,
            )
        if row is None:
            raise WebStageCheckpointConflict("阶段证据归属或完成租约已失效")

    @staticmethod
    def _effect_key(cursor, operation):
        if operation is None:
            return cursor.idempotency_key
        if operation not in {
            "primary_content",
            "detail_primary_content",
            "auxiliary",
            "detail",
            "classification",
            "list_href_discovery",
        }:
            raise ValueError("未定义的阶段内操作")
        return f"{cursor.idempotency_key}:{operation}"
