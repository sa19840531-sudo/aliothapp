"""成功 H5 Probe 的补采挂接恢复读取。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.h5_evidence_manifest import H5EvidenceManifestFinalizer
from services.api.app.application.h5_exploration_workflow import H5PageDetectorPort
from services.api.app.application.h5_probe_work import H5ProbeWorkError, H5ProbeWorkOutcome
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.exploration import ProbeWork
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind
from services.api.app.infrastructure.db.pool import get_postgres_pool


class H5ProbeCompletionRecoveryPersistenceError(RuntimeError):
    """持久化 H5 Probe 与动作结果不能形成可信补挂候选。"""


class PostgresH5ProbeEvidencePreparer:
    """挂接前校验同一 H5 Probe 动作的前后清单，不改变公共补采流程。"""

    def __init__(
        self,
        *,
        finalizer: H5EvidenceManifestFinalizer,
        manifests: EvidenceManifestRepository,
        detector: H5PageDetectorPort,
        pool: Any | None = None,
    ) -> None:
        self._finalizer = finalizer
        self._manifests = manifests
        self._detector = detector
        self._pool = pool

    async def prepare(self, *, task_id: str, outcome: H5ProbeWorkOutcome) -> None:
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT before_capture.id::text AS before_capture_id,
                       result_capture.id::text AS result_capture_id,
                       result_capture.agent_run_id::text AS agent_run_id,
                       result_capture.surface_session_id AS session_id,
                       work.deadline, request.expires_at
                FROM qc_v2_work_item AS work
                JOIN qc_task AS task ON task.id = work.task_id
                    AND task.target_platform = 'h5'
                    AND task.status IN ('running', 'review')
                    AND task.finalization_epoch = work.finalization_epoch
                JOIN qc_evidence_request AS request
                    ON request.request_id = work.evidence_request_id
                    AND request.task_id = work.task_id
                    AND request.finalization_epoch = work.finalization_epoch
                JOIN qc_action_execution AS action
                    ON action.task_id = work.task_id
                    AND action.session_id = work.session_id
                    AND action.finalization_epoch = work.finalization_epoch
                    AND action.status = 'completed'
                    AND action.intent_payload #>> '{parameters,exploration_work_id}' = work.work_id
                JOIN qc_capture AS result_capture
                    ON result_capture.id = action.result_capture_id
                    AND result_capture.task_id = work.task_id
                    AND result_capture.surface_session_id = work.session_id
                    AND result_capture.action_execution_id = action.action_execution_id
                    AND result_capture.capture_role = 'probe_result_replay'
                    AND result_capture.capture_status = 'captured'
                JOIN qc_capture AS before_capture
                    ON before_capture.id = result_capture.parent_capture_id
                    AND before_capture.task_id = result_capture.task_id
                    AND before_capture.agent_run_id = result_capture.agent_run_id
                    AND before_capture.surface_session_id = result_capture.surface_session_id
                    AND before_capture.action_execution_id = result_capture.action_execution_id
                    AND before_capture.capture_role = 'probe_before'
                    AND before_capture.capture_status = 'captured'
                JOIN qc_capture AS source_capture
                    ON source_capture.id = work.source_capture_id
                    AND source_capture.task_id = work.task_id
                    AND source_capture.agent_run_id = result_capture.agent_run_id
                WHERE work.task_id = $1::uuid AND work.work_id = $2
                    AND work.evidence_request_id = $3
                    AND result_capture.id = $4::uuid
                    AND work.work_type = 'probe' AND work.status = 'succeeded'
                    AND work.work_payload->>'surface_kind' = 'web'
                """,
                task_id,
                outcome.work_id,
                outcome.evidence_request_id,
                outcome.result_capture_id,
            )
        if row is None:
            row = await self._shared_pair(task_id=task_id, outcome=outcome, pool=pool)
        if row is None:
            raise H5ProbeWorkError(
                "H5 Probe 前后证据未通过同任务、动作、Run 与 Epoch 校验",
                error_code="H5_PROBE_CAPTURE_PAIR_INVALID",
                retryable=False,
            )
        deadlines = [row.get(key) for key in ("deadline", "expires_at")
                     if isinstance(row.get(key), datetime)]
        if deadlines and datetime.now(UTC) >= min(deadlines):
            raise H5ProbeWorkError(
                "H5 Probe 挂接已到冻结截止，保留原证据并结算缺口，不再重复识别",
                error_code="H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED", retryable=False,
            )
        # primary 可能是刷新前或动作后帧，两份都需按真实制品识别，
        # 不能让默认 loaded 状态或旧 Capture 页型进入重判。
        for capture_id, role in (
            (row["before_capture_id"], "probe_before"),
            (row["result_capture_id"], "probe_result_replay"),
        ):
            detection = await _before_attachment_deadline(self._detector.detect, deadlines,
                task_id=task_id,
                agent_run_id=row["agent_run_id"],
                capture=CaptureRef(
                    task_id=task_id,
                    session_id=row["session_id"],
                    capture_id=capture_id,
                    surface_kind=SurfaceKind.WEB,
                    capture_kind=CaptureKind.PAGE,
                    role=role,
                ),
            )
            if (
                not isinstance(detection, Mapping)
                or detection.get("detection_status") != "recognized"
            ):
                raise H5ProbeWorkError(
                    "H5 Probe 页面事实尚未完成可信识别和持久化",
                    error_code="H5_PROBE_PAGE_DETECTION_UNAVAILABLE",
                    retryable=True,
                )
            # 弹窗等已识别状态可以作为前后对照证据，不等于可独立匹配业务规则。
            # 清单只证明附件可信；规则适用性仍由实际页型、状态和证据计划决定。
        for capture_id in (row["before_capture_id"], row["result_capture_id"]):
            await _before_attachment_deadline(
                self._finalizer.finalize_capture, deadlines, task_id=task_id, capture_id=capture_id,
            )
            manifest = await _before_attachment_deadline(
                self._manifests.get_latest_for_capture, deadlines,
                task_id=task_id, capture_id=capture_id,
            )
            if manifest is None or manifest.quality_status is not EvidenceQualityStatus.PASS:
                raise H5ProbeWorkError(
                    "H5 Probe 前后证据尚未形成 PASS Manifest",
                    error_code="H5_PROBE_CAPTURE_PAIR_MANIFEST_UNAVAILABLE",
                    retryable=(
                        manifest is None or manifest.quality_status is EvidenceQualityStatus.PENDING
                    ),
                )

    async def _shared_pair(self, *, task_id, outcome, pool):
        """共享请求独立复核来源，只引用原动作，不改写 Action Trace 的归属。"""
        from services.api.app.infrastructure.db.h5_shared_probe_repository import (
            PostgresH5SharedProbeEvidenceReader,
        )

        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT work.work_payload, work.terminal_payload,
                    capture.agent_run_id::text AS agent_run_id
                FROM qc_v2_work_item AS work
                JOIN qc_capture AS capture ON capture.id = work.source_capture_id
                    AND capture.task_id = work.task_id
                WHERE work.task_id = $1::uuid AND work.work_id = $2
                    AND work.status = 'succeeded' AND work.work_type = 'probe'
                    AND work.evidence_request_id = $3
                    AND work.terminal_payload->>'status' = 'completed'
                    AND work.terminal_payload->>'reason' LIKE 'h5_shared_evidence:%'
                """, task_id, outcome.work_id, outcome.evidence_request_id,
            )
        if row is None:
            return None
        terminal = _as_mapping(row["terminal_payload"])
        work = ProbeWork.from_dict(_as_mapping(row["work_payload"]))
        if terminal.get("work_id") != work.work_id or terminal.get("task_id") != task_id:
            return None
        pair = await PostgresH5SharedProbeEvidenceReader(pool=pool).read_pair(
            work=work, agent_run_id=row["agent_run_id"],
            origin_work_id=terminal["reason"].removeprefix("h5_shared_evidence:"),
            result_capture_id=outcome.result_capture_id,
        )
        return {**pair, "deadline": work.deadline,
                "expires_at": work.evidence_request.expires_at} if pair is not None else None


async def _before_attachment_deadline(operation, deadlines, **kwargs):
    """一次挂接内每个异步阶段共用原截止，取消并等待本操作退出后才结算。"""
    if not deadlines:
        return await operation(**kwargs)
    remaining = (min(deadlines) - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise H5ProbeWorkError(
            "H5 Probe 挂接阶段已到冻结截止，不再启动操作",
            error_code="H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED", retryable=False,
        )
    limit = asyncio.timeout(max(0.0, remaining))
    try:
        async with limit:
            result = await operation(**kwargs)
    except TimeoutError:
        if not limit.expired():
            raise
    else:
        # 底层吞掉取消或阻塞事件循环后晚返回，也不能授予过期结果成功资格。
        if not limit.expired() and datetime.now(UTC) < min(deadlines):
            return result
    raise H5ProbeWorkError(
        "H5 Probe 挂接已到冻结截止，保留原证据并结算缺口",
        error_code="H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED", retryable=False,
    ) from None


class PostgresH5ProbeCompletionRecoveryReader:
    """读取已成功但尚未完成 Supplement 挂接或 InputSet 重封的 H5 Probe。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def find_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ProbeWorkOutcome | None:
        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    work.work_payload,
                    result_capture.id::text AS result_capture_id
                FROM qc_v2_work_item AS work
                INNER JOIN qc_task AS task
                    ON task.id = work.task_id
                   AND task.status IN ('running', 'review')
                   AND task.finalization_state IN (
                       'open', 'exploration_draining', 'session_draining')
                   AND lower(task.target_platform) = 'h5'
                   AND task.finalization_epoch = work.finalization_epoch
                INNER JOIN qc_evidence_request AS request
                    ON request.request_id = work.evidence_request_id
                   AND request.task_id = work.task_id
                   AND request.finalization_epoch = work.finalization_epoch
                   AND request.session_dependency IN ('active_session', 'snapshot')
                   AND request.status IN ('routed', 'running', 'satisfied')
                INNER JOIN qc_capture AS source_capture
                    ON source_capture.id = work.source_capture_id
                   AND source_capture.task_id = work.task_id
                   AND source_capture.agent_run_id = $2::uuid
                INNER JOIN qc_action_execution AS action
                    ON action.task_id = work.task_id
                   AND action.session_id = work.session_id
                   AND action.finalization_epoch = work.finalization_epoch
                   AND action.status = 'completed'
                   AND action.result_capture_id IS NOT NULL
                   AND action.intent_payload
                        #>> '{parameters,exploration_work_id}' = CASE
                            WHEN work.terminal_payload->>'status' = 'completed'
                              AND work.terminal_payload->>'reason' LIKE 'h5_shared_evidence:%'
                            THEN substring(work.terminal_payload->>'reason' FROM 20)
                            ELSE work.work_id END
                INNER JOIN qc_capture AS result_capture
                    ON result_capture.id = action.result_capture_id
                   AND result_capture.task_id = work.task_id
                   AND result_capture.agent_run_id = $2::uuid
                   AND result_capture.surface_session_id = work.session_id
                   AND result_capture.capture_role = 'probe_result_replay'
                   AND result_capture.action_execution_id = action.action_execution_id
                LEFT JOIN qc_evidence_supplement AS supplement
                    ON supplement.request_id = request.request_id
                   AND supplement.probe_work_id = work.work_id
                WHERE work.task_id = $1::uuid
                  AND work.work_type = 'probe'
                  AND work.work_payload->>'surface_kind' = 'web'
                  AND work.status = 'succeeded'
                  AND (
                      supplement.supplement_id IS NULL
                      OR (
                          supplement.evidence_input_set_id IS NULL
                          AND strpos(
                              COALESCE(request.resolution_reason, ''),
                              'resealed_input_set:'
                          ) = 0
                      )
                  )
                ORDER BY work.finished_at, work.work_id
                LIMIT 1
                """,
                task_id,
                agent_run_id,
            )
        if not rows:
            return None
        work = ProbeWork.from_dict(_as_mapping(rows[0]["work_payload"]))
        result_capture_id = str(rows[0]["result_capture_id"] or "").strip()
        if work.task_id != task_id or work.surface_kind.value != "web" or not result_capture_id:
            raise H5ProbeCompletionRecoveryPersistenceError("待挂接 H5 Probe 身份无效")
        return H5ProbeWorkOutcome(
            claimed=True,
            work_id=work.work_id,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            result_capture_id=result_capture_id,
            evidence_request_id=work.evidence_request.request_id,
            restoration_attempt_id=work.restoration_attempt_id,
            equivalence_assessment_id=work.equivalence_assessment_id,
            source_grade=work.source_grade,
        )


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise H5ProbeCompletionRecoveryPersistenceError("Probe Work 载荷必须为对象")
    return value


__all__ = [
    "H5ProbeCompletionRecoveryPersistenceError",
    "PostgresH5ProbeCompletionRecoveryReader",
]
