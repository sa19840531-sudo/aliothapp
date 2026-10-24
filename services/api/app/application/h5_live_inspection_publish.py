"""H5 活会话期间的增量质检发布：探索留证后质检同步产出。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import md5
from typing import TYPE_CHECKING, Any, Protocol

from services.api.app.application.app_inspection_publisher import (
    CaptureInspectionPublisher,
    build_default_app_capture_inspection_publisher,
)
from services.api.app.application.app_live_inspection_publish import (
    DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS,
    DEFAULT_ACTIVE_SESSION_DRAIN_MAX_WAIT_SECONDS,
)
from services.api.app.application.h5_evidence_handoff import (
    H5EvidenceCapture,
    H5EvidenceCaptureReader,
)
from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
    select_stable_publication_batch,
)
from services.api.app.domain.task import (
    H5InspectionNotificationMode,
    TaskExecutionMode,
    parse_h5_inspection_notification_mode,
    task_execution_mode_from_config,
)
from services.api.app.infrastructure.db.h5_evidence_handoff_repository import (
    PostgresH5EvidenceCaptureReader,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

if TYPE_CHECKING:
    from services.api.app.application.evidence_request_admission import (
        EvidenceRequestAdmissionRepository,
    )
    from services.api.app.application.supplemental_evidence_policy import (
        SupplementalEvidencePolicy,
    )

logger = logging.getLogger(__name__)

# 已进入发布后生命周期的页面级质检状态；其余视为待发布。
_PUBLISHED_INSPECTION_STATUSES = frozenset({"evaluating", "completed", "degraded", "failed"})
_JUDGEABLE_CAPTURE_ROLE = "after_action"
DEFAULT_H5_ACTIVE_SESSION_CHECKPOINT_MAX_WAIT_SECONDS = 300.0


@dataclass(frozen=True, kw_only=True)
class H5LiveInspectionTaskGate:
    """增量发布的任务门禁事实：平台、执行模式与锁定通知模式。"""

    target_platform: str
    task_config: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class H5ActiveSessionDrainState:
    """关闭前 H5 活会话规则的数据库排空事实。"""

    pending_capture_count: int
    nonterminal_work_item_count: int
    exhausted_work_item_count: int = 0
    nonterminal_evidence_request_count: int = 0
    snapshot_work_item_count: int = 0
    snapshot_evidence_request_count: int = 0
    scheduled_work_item_count: int = 0
    terminal_fact_count: int = 0
    next_attempt_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "pending_capture_count",
            "nonterminal_work_item_count",
            "exhausted_work_item_count",
            "nonterminal_evidence_request_count",
            "snapshot_work_item_count",
            "snapshot_evidence_request_count",
            "scheduled_work_item_count",
            "terminal_fact_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.next_attempt_at is not None and self.next_attempt_at.utcoffset() is None:
            raise ValueError("next_attempt_at 必须带时区")
        if self.exhausted_work_item_count > self.nonterminal_work_item_count:
            raise ValueError("exhausted_work_item_count 不能大于非终态工作项数")
        if self.scheduled_work_item_count > self.nonterminal_work_item_count + self.snapshot_work_item_count:
            raise ValueError("scheduled_work_item_count 不能大于非终态工作项总数")

    @property
    def drained(self) -> bool:
        return (
            self.pending_capture_count == 0
            and self.nonterminal_work_item_count == 0
            and self.nonterminal_evidence_request_count == 0
        )

    @property
    def close_drained(self) -> bool:
        return (
            self.drained
            and self.snapshot_work_item_count == 0
            and self.snapshot_evidence_request_count == 0
        )


@dataclass(frozen=True, kw_only=True)
class H5ActiveSessionInspectionSummary:
    """关闭前 H5 活会话规则发布与消费摘要。"""

    published_work_item_count: int
    drain_round_count: int
    # 排空窗口用尽后按证据不足结算的活会话工作项数；0 表示正常排空。
    settled_work_item_count: int = 0
    # 无现场请求的快照判定可在关闭后继续，仍由消费 Run 和 Completion Gate 等待正式终态。
    deferred_snapshot_work_item_count: int = 0
    deferred_snapshot_evidence_request_count: int = 0


class H5LiveInspectionTaskGateReader(Protocol):
    """读取增量发布所需的任务事实。"""

    async def load_task_gate(self, *, task_id: str) -> H5LiveInspectionTaskGate | None: ...


class H5ActiveSessionInspectionStateReader(Protocol):
    """读取 H5 活会话待发布 Capture 与非终态工作项。"""

    async def load_drain_state(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ActiveSessionDrainState: ...


class H5InspectionWorkConsumerPort(Protocol):
    """同步消费当前 H5 Run 持有的活会话规则工作项。"""

    async def drain_available(self, **kwargs: Any) -> object: ...


class H5InspectionEvidenceWaitPort(Protocol):
    """暂停已有补证请求的 H5 工作项，避免等待期间重复 Judge。"""

    async def park_open_requests(self, *, task_id: str, agent_run_id: str) -> int: ...


class H5ProbeDrainPort(Protocol):
    """H5 活会话内一次补挂或 Probe 消费端口。"""

    async def drain_one(self, *, task_id: str, agent_run_id: str) -> bool: ...


class H5TargetedRecaptureDrainPort(Protocol):
    """H5 活会话内一次 Evidence Request 恢复与安全派发端口。"""

    async def drain_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
        include_snapshot: bool = False,
        defer_recoverable_snapshots: bool = False,
    ) -> bool: ...


class H5CoverageAuthorityReconcilerPort(Protocol):
    """补齐已识别 Capture 的 H5 页面评估和覆盖权威。"""

    async def reconcile_available(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int = 50,
    ) -> int: ...


class H5ActiveSessionCancellationPort(Protocol):
    """H5 活会话排空阶段的协作式取消检查。"""

    async def raise_if_requested(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        stage: str,
    ) -> None: ...


class PostgresH5LiveInspectionTaskGateReader:
    """从 qc_task 读取平台与执行模式门禁。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def load_task_gate(self, *, task_id: str) -> H5LiveInspectionTaskGate | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT target_platform, task_config
                FROM qc_task
                WHERE id = $1::uuid
                """,
                task_id,
            )
        if row is None:
            return None
        config = row["task_config"]
        if isinstance(config, str):
            # 连接池未注册 jsonb 编解码器时取回字符串，必须先解码。
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        return H5LiveInspectionTaskGate(
            target_platform=str(row["target_platform"] or "").strip(),
            task_config=dict(config) if isinstance(config, Mapping) else {},
        )


class PostgresH5ActiveSessionInspectionStateReader:
    """以 Capture 生命周期和工作项终态读取 H5 关闭屏障事实。"""

    def __init__(
        self,
        *,
        capture_reader: H5EvidenceCaptureReader | None = None,
        database_url: str | None = None,
        pool: Any | None = None,
    ) -> None:
        self._capture_reader = capture_reader or PostgresH5EvidenceCaptureReader()
        self._database_url = database_url
        self._pool = pool

    async def load_drain_state(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ActiveSessionDrainState:
        captures = await self._capture_reader.list_judgeable_captures(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        pending_capture_count = sum(_is_pending_publishable(item) for item in captures)
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH work_state AS (
                    SELECT
                        count(*) FILTER (
                            WHERE work_item.session_dependency = 'active_session'
                        )::integer AS nonterminal_work_item_count,
                        count(*) FILTER (
                            WHERE work_item.session_dependency = 'snapshot'
                        )::integer AS snapshot_work_item_count,
                        count(*) FILTER (
                            WHERE task.target_platform = 'h5'
                              AND (
                                  (work_item.status IN ('leased', 'running')
                                   AND work_item.lease_token IS NOT NULL
                                   AND work_item.lease_expires_at > now())
                                  OR (work_item.status = 'retry_wait'
                                      AND work_item.attempt_count < work_item.max_attempts
                                      AND work_item.next_attempt_at > now())
                              )
                        )::integer AS scheduled_work_item_count,
                        count(*) FILTER (
                            WHERE work_item.status IN ('retry_wait', 'waiting_evidence')
                              AND work_item.session_dependency = 'active_session'
                              AND work_item.attempt_count >= work_item.max_attempts
                              AND work_item.lease_token IS NULL
                        )::integer AS exhausted_work_item_count,
                        min(
                            CASE
                                WHEN work_item.status IN ('leased', 'running')
                                    THEN work_item.lease_expires_at
                                ELSE work_item.next_attempt_at
                            END
                        ) AS next_attempt_at
                    FROM qc_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE work_item.task_id = $1::uuid
                      AND work_item.producer_agent_run_id = $2::uuid
                      AND (
                          work_item.session_dependency = 'snapshot'
                          OR (
                              work_item.session_dependency = 'active_session'
                              AND work_item.session_owner_agent_run_id = $2::uuid
                          )
                      )
                      AND work_item.control_mode = 'enforced'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status NOT IN (
                          'succeeded', 'partial', 'failed',
                          'cancelled', 'superseded'
                      )
                      -- 等待补采本身不阻塞；H5 重试耗尽且没有在途请求时必须正式结算。
                      AND (
                          work_item.status <> 'waiting_evidence'
                          OR (
                              task.target_platform = 'h5'
                              AND work_item.session_dependency = 'active_session'
                              AND work_item.attempt_count >= work_item.max_attempts
                              AND work_item.lease_token IS NULL
                              AND NOT EXISTS (
                                  SELECT 1 FROM qc_evidence_request AS inflight
                                  WHERE inflight.work_item_id = work_item.id
                                    AND inflight.status IN ('pending', 'routed', 'running')
                              )
                          )
                      )
                ), request_state AS (
                    SELECT
                        count(*) FILTER (
                            WHERE request.session_dependency = 'active_session'
                        )::integer AS nonterminal_evidence_request_count,
                        count(*) FILTER (
                            WHERE request.session_dependency = 'snapshot'
                        )::integer AS snapshot_evidence_request_count,
                        min(
                            CASE
                                WHEN request.status = 'running'
                                    THEN request.lease_expires_at
                                ELSE request.next_attempt_at
                            END
                        ) AS next_attempt_at
                    FROM qc_evidence_request AS request
                    INNER JOIN qc_capture AS capture
                        ON capture.id = request.target_capture_id
                       AND capture.task_id = request.task_id
                       AND capture.agent_run_id = $2::uuid
                    INNER JOIN qc_task AS task ON task.id = request.task_id
                    WHERE request.task_id = $1::uuid
                      AND request.session_dependency IN ('active_session', 'snapshot')
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.status IN ('pending', 'routed', 'running')
                )
                SELECT
                    work_state.nonterminal_work_item_count,
                    work_state.snapshot_work_item_count,
                    work_state.scheduled_work_item_count,
                    work_state.exhausted_work_item_count,
                    request_state.nonterminal_evidence_request_count,
                    request_state.snapshot_evidence_request_count,
                    (
                        SELECT count(*) FROM qc_inspection_work_item AS finished
                        INNER JOIN qc_task AS task ON task.id = finished.task_id
                        WHERE finished.task_id = $1::uuid
                          AND finished.producer_agent_run_id = $2::uuid
                          AND finished.finalization_epoch = task.finalization_epoch
                          AND finished.status IN (
                              'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
                          )
                    ) + (
                        SELECT count(*) FROM qc_evidence_request AS finished
                        INNER JOIN qc_capture AS capture
                            ON capture.id = finished.target_capture_id
                           AND capture.task_id = finished.task_id
                           AND capture.agent_run_id = $2::uuid
                        INNER JOIN qc_task AS task ON task.id = finished.task_id
                        WHERE finished.task_id = $1::uuid
                          AND finished.finalization_epoch = task.finalization_epoch
                          AND finished.status NOT IN ('pending', 'routed', 'running')
                    ) AS terminal_fact_count,
                    LEAST(
                        work_state.next_attempt_at,
                        request_state.next_attempt_at
                    ) AS next_attempt_at
                FROM work_state
                CROSS JOIN request_state
                """,
                task_id,
                agent_run_id,
            )
        return H5ActiveSessionDrainState(
            pending_capture_count=pending_capture_count,
            nonterminal_work_item_count=int(row["nonterminal_work_item_count"] or 0),
            exhausted_work_item_count=int(row["exhausted_work_item_count"] or 0),
            nonterminal_evidence_request_count=int(
                row["nonterminal_evidence_request_count"] or 0
            ),
            next_attempt_at=row["next_attempt_at"],
            snapshot_work_item_count=int(row.get("snapshot_work_item_count") or 0),
            snapshot_evidence_request_count=int(row.get("snapshot_evidence_request_count") or 0),
            scheduled_work_item_count=int(row.get("scheduled_work_item_count") or 0),
            terminal_fact_count=int(row.get("terminal_fact_count") or 0),
        )


class PostgresH5InspectionEvidenceWaitRepository:
    """只停泊当前 H5 Run 中已经产生正式补证请求的重试工作项。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool

    async def park_open_requests(self, *, task_id: str, agent_run_id: str) -> int:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                WITH parked AS (
                    UPDATE qc_inspection_work_item AS work_item
                    SET
                        status = 'waiting_evidence',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = work_item.row_version + 1,
                        last_error_code = 'INSPECTION_EVIDENCE_REQUEST_PENDING',
                        last_error_message =
                            'H5 已生成补证请求，暂停 Judge 重试并等待请求终态',
                        updated_at = now()
                    FROM qc_task AS task
                    WHERE work_item.task_id = $1::uuid
                      AND work_item.producer_agent_run_id = $2::uuid
                      AND (
                          work_item.session_dependency = 'snapshot'
                          OR (
                              work_item.session_dependency = 'active_session'
                              AND work_item.session_owner_agent_run_id = $2::uuid
                          )
                      )
                      AND work_item.control_mode = 'enforced'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND task.id = work_item.task_id
                      AND lower(task.target_platform) = 'h5'
                      AND task.status = 'running'
                      AND work_item.status = 'retry_wait'
                      AND work_item.lease_token IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM qc_evidence_request AS request
                          WHERE request.work_item_id = work_item.id
                            AND request.task_id = work_item.task_id
                            AND request.finalization_epoch = work_item.finalization_epoch
                            AND request.status IN ('pending', 'routed', 'running')
                      )
                    RETURNING work_item.id
                )
                SELECT count(*)::integer FROM parked
                """,
                task_id,
                agent_run_id,
            )
        return int(value or 0)


class H5LiveInspectionPublishService:
    """活会话中把新落库的可判定 Capture 立即发布为质检工作项。

    发布按 input_hash 幂等，收口证据交接门禁仍是完整性权威；
    active-session 规则必须在当前 Run 持有 Session 时执行，不能留到关闭后补发；
    单个 Capture 发布失败只损失本次唤醒窗口，由后续唤醒或收口门禁显式收敛。
    """

    def __init__(
        self,
        *,
        gate_reader: H5LiveInspectionTaskGateReader,
        capture_reader: H5EvidenceCaptureReader,
        publisher: CaptureInspectionPublisher,
        web_identity_reader: Any | None = None,
    ) -> None:
        self._gate_reader = gate_reader
        self._capture_reader = capture_reader
        self._publisher = publisher
        self._web_identity_reader = web_identity_reader

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int:
        if limit is not None:
            raise ValueError("有界发布必须调用 publish_pending_batch")
        task = str(task_id or "").strip()
        run_id = str(agent_run_id or "").strip()
        if not task or not run_id:
            raise ValueError("task_id 和 agent_run_id 不能为空")
        allow_web_final_handoff = await self._load_gate(task_id=task)
        if allow_web_final_handoff is None:
            return 0
        captures = await self._capture_reader.list_judgeable_captures(
            task_id=task,
            agent_run_id=run_id,
        )
        return await self._publish_captures(
            task=task,
            run_id=run_id,
            captures=captures,
            allow_web_final_handoff=allow_web_final_handoff,
        )

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult:
        """H5 只轮转待发布证据；Web 保留原有清单轮转行为。"""

        task = str(task_id or "").strip()
        run_id = str(agent_run_id or "").strip()
        if not task or not run_id:
            raise ValueError("task_id 和 agent_run_id 不能为空")
        allow_web_final_handoff = await self._load_gate(task_id=task)
        if allow_web_final_handoff is None:
            return LiveInspectionPublicationBatchResult(0, 0, False)
        captures = await self._capture_reader.list_judgeable_captures(
            task_id=task,
            agent_run_id=run_id,
        )
        if not allow_web_final_handoff:
            # 已判定和不具备发布资格的历史截图不是待办，不能反复唤醒空消费。
            captures = [capture for capture in captures if _is_pending_publishable(capture)]
        batch = select_stable_publication_batch(
            captures,
            identity=lambda capture: capture.capture_id,
            limit=limit,
            cursor=cursor,
        )
        created = await self._publish_captures(
            task=task,
            run_id=run_id,
            captures=batch.items,
            allow_web_final_handoff=allow_web_final_handoff,
        )
        return LiveInspectionPublicationBatchResult(
            attempted_count=len(batch.items),
            created_count=created,
            has_more=batch.has_more,
            next_cursor=batch.next_cursor,
        )

    async def _load_gate(self, *, task_id: str) -> bool | None:
        gate = await self._gate_reader.load_task_gate(task_id=task_id)
        if gate is None or gate.target_platform.casefold() not in {"h5", "web"}:
            return None
        if task_execution_mode_from_config(gate.task_config) is not (
            TaskExecutionMode.FULL_INSPECTION
        ):
            return None
        notification_mode = parse_h5_inspection_notification_mode(gate.task_config)
        if notification_mode is H5InspectionNotificationMode.SHADOW:
            # shadow 只允许收口阶段记录冻结计划，不得提前产生正式 Work。
            return None
        return gate.target_platform.casefold() == "web"

    async def _publish_captures(
        self,
        *,
        task: str,
        run_id: str,
        captures: list[H5EvidenceCapture] | tuple[H5EvidenceCapture, ...],
        allow_web_final_handoff: bool,
    ) -> int:
        if not captures:
            # 通用 Judge 调度携带的是消费 Run；Web 主 Run 由专用适配器另行映射。
            # 当前 Run 没有 Capture 时不得误读其冻结策略并制造历史缺口诊断。
            return 0
        published = 0
        published_h5_states: set[tuple[str, str, str, str]] = set()
        published_web_versions: set[tuple[str, str]] = set()
        web_identities = {}
        if allow_web_final_handoff:
            from services.api.app.domain.exploration.web_run_policy_snapshot import (
                WebRunPolicyError,
            )
            from services.api.app.infrastructure.db.web_capture_identity import (
                PostgresWebCaptureIdentityReader,
            )

            reader = self._web_identity_reader or PostgresWebCaptureIdentityReader()
            try:
                web_identities = await reader.read_for_run(task_id=task, agent_run_id=run_id)
            except WebRunPolicyError as exc:
                logger.warning("task_id=%s Web 身份未认证，保留待发布：%s", task, str(exc).split(":", 1)[0])
                return 0
        for capture in captures:
            if not _is_pending_publishable(
                capture,
                allow_web_final_handoff=allow_web_final_handoff,
            ):
                continue
            content_version: tuple[str, str] | None = None
            h5_logical_state: tuple[str, str, str, str] | None = None
            if allow_web_final_handoff:
                identity = web_identities.get(capture.capture_id, {})
                if identity.get("logical_page_identity_status") != "verified" or not identity.get("logical_page_id"):
                    logger.warning("task_id=%s capture_id=%s WEB_LOGICAL_ID_UNTRUSTED，保留待发布", task, capture.capture_id)
                    continue
                content_version = _web_content_version_key(capture, logical_page_id=identity["logical_page_id"])
                if (
                    content_version is not None
                    and content_version in published_web_versions
                ):
                    continue
            else:
                h5_logical_state = _h5_logical_state_key(capture)
                if (
                    h5_logical_state is not None
                    and h5_logical_state in published_h5_states
                ):
                    continue
            try:
                result = await self._publisher.publish_for_capture(
                    task_id=task,
                    capture_id=capture.capture_id,
                    visual_page_type=capture.page_type,
                    page_state=(str(capture.page_state or "stable").strip() or "stable"),
                    agent_run_id=run_id,
                    # H5 先发布快照规则，关闭前可用现存会话补齐；Web 保持原发布口径。
                    active_session_only=allow_web_final_handoff,
                )
                if content_version is not None:
                    published_web_versions.add(content_version)
                if h5_logical_state is not None:
                    published_h5_states.add(h5_logical_state)
                items = tuple(getattr(result, "items", ()) or ())
                published += (
                    sum(bool(item.get("created")) for item in items if isinstance(item, Mapping))
                    if items
                    else int(result.published_count)
                )
            except Exception:  # noqa: BLE001 - 单点发布失败不得阻断活会话消费
                logger.exception(
                    "task_id=%s capture_id=%s 活会话质检发布失败，等待收口门禁兜底。",
                    task,
                    capture.capture_id,
                )
        return published


def _web_content_version_key(capture: H5EvidenceCapture, *, logical_page_id: str) -> tuple[str, str] | None:
    """只使用同 Run 冻结策略认证的持久 ID 与 DOM/文本指纹。"""
    fingerprint_parts = sorted(
        f"{artifact.artifact_type}:{artifact.checksum}"
        for artifact in capture.artifact_refs
        if artifact.artifact_type in {"dom", "visible_text"}
        and artifact.lifecycle_status in {"active", "archived"}
    )
    if logical_page_id is None or len(fingerprint_parts) < 2:
        return None
    content_fingerprint = md5(  # noqa: S324 - 与 PostgreSQL 完成门的 md5 合同一致
        "|".join(fingerprint_parts).encode("utf-8")
    ).hexdigest()
    return logical_page_id, content_fingerprint


def _h5_logical_state_key(
    capture: H5EvidenceCapture,
) -> tuple[str, str, str, str] | None:
    identity = str(capture.logical_identity_fingerprint or "").strip()
    state = str(capture.logical_state_fingerprint or "").strip()
    page_type = str(capture.page_type or "").strip().casefold()
    page_state = str(capture.page_state or "stable").strip().casefold() or "stable"
    if not identity or not state or not page_type:
        return None
    return identity, state, page_type, page_state


class H5ActiveSessionInspectionDrainService:
    """在关闭 H5 Session 前补发、判定并排空 active-session 规则。"""

    def __init__(
        self,
        *,
        publisher: H5LiveInspectionPublishService,
        consumer: H5InspectionWorkConsumerPort,
        state_reader: H5ActiveSessionInspectionStateReader,
        grace_seconds: float = DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS,
        max_wait_seconds: float = DEFAULT_ACTIVE_SESSION_DRAIN_MAX_WAIT_SECONDS,
        checkpoint_max_wait_seconds: float = (
            DEFAULT_H5_ACTIVE_SESSION_CHECKPOINT_MAX_WAIT_SECONDS
        ),
        max_poll_seconds: float = 5.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        closure_settlement: Any | None = None,
        targeted_recapture_drain: H5TargetedRecaptureDrainPort | None = None,
        probe_drain: H5ProbeDrainPort | None = None,
        cancellation: H5ActiveSessionCancellationPort | None = None,
        evidence_wait: H5InspectionEvidenceWaitPort | None = None,
        coverage_authority: H5CoverageAuthorityReconcilerPort | None = None,
        closed_session_cleanup: Any | None = None,
    ) -> None:
        if (
            grace_seconds <= 0
            or max_wait_seconds <= 0
            or checkpoint_max_wait_seconds <= 0
            or max_poll_seconds <= 0
        ):
            raise ValueError(
                "H5 活会话排空宽限、绝对上限、页面检查点无进展上限"
                "与轮询间隔必须大于 0"
            )
        self._closure_settlement = closure_settlement
        self._publisher = publisher
        self._consumer = consumer
        self._state_reader = state_reader
        self._grace_seconds = grace_seconds
        self._max_wait_seconds = max_wait_seconds
        self._checkpoint_max_wait_seconds = checkpoint_max_wait_seconds
        self._max_poll_seconds = max_poll_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._targeted_recapture_drain = targeted_recapture_drain
        self._probe_drain = probe_drain
        self._cancellation = cancellation
        self._evidence_wait = evidence_wait
        self._coverage_authority = coverage_authority
        self._closed_session_cleanup = closed_session_cleanup

    async def drain_closed_session_once(self, *, task_id: str, agent_run_id: str,
                                        deadline: datetime) -> bool:
        """先消费已派发 Probe，再领取关闭后的 Snapshot；不启动新一轮探索。"""
        if self._closed_session_cleanup is None:
            raise RuntimeError("H5 关闭后补采未装配新会话 Runtime")
        if self._probe_drain is not None and await self._probe_drain.drain_one(
            task_id=task_id, agent_run_id=agent_run_id,
        ):
            return True
        return bool(self._targeted_recapture_drain is not None
                    and await self._targeted_recapture_drain.drain_one(
                        task_id=task_id, agent_run_id=agent_run_id,
                        deadline=deadline, include_snapshot=True,
                    ))

    async def close_recapture_sessions(self) -> None:
        if self._closed_session_cleanup is not None:
            await self._closed_session_cleanup()

    async def run_current_surface_checkpoint(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> H5ActiveSessionInspectionSummary:
        """在 H5 离开当前页面前完成一次发布、补采和重判推进。"""

        task = str(task_id or "").strip()
        run_id = str(agent_run_id or "").strip()
        if not task or not run_id:
            raise ValueError("task_id 和 agent_run_id 不能为空")
        if deadline.utcoffset() is None:
            raise ValueError("deadline 必须带时区")
        published_count = 0
        drain_round_count = 0
        settled_count = 0
        waited = 0.0
        started_at = self._now()
        last_progress_elapsed = 0.0
        last_outstanding: tuple[int, ...] | None = None
        last_terminal_fact_count: int | None = None
        holding_schedule_until: datetime | None = None
        # 页面检查点的健康推进受冻结 Run 截止约束；900 秒只限制无进展等待。
        # 最终关闭仍使用下面独立的绝对排空窗口，不能被页面推进无限延长。
        while self._now() < deadline:
            waited = max(waited, (self._now() - started_at).total_seconds())
            if waited - last_progress_elapsed >= self._max_wait_seconds or (
                waited - last_progress_elapsed >= self._checkpoint_max_wait_seconds
                and (holding_schedule_until is None or self._now() >= holding_schedule_until)
            ):
                break
            if self._cancellation is not None:
                await self._cancellation.raise_if_requested(
                    task_id=task,
                    agent_run_id=run_id,
                    stage="active_session_inspection_checkpoint",
                )
            if self._now() >= deadline:
                break
            authority_promoted = await self._reconcile_coverage_authority(
                task_id=task, agent_run_id=run_id,
            )
            if self._now() >= deadline:
                break
            published = await self._publisher.publish_pending(
                task_id=task,
                agent_run_id=run_id,
            )
            published_count += published
            await self._park_open_evidence_requests(task_id=task, agent_run_id=run_id)
            state = await self._state_reader.load_drain_state(
                task_id=task,
                agent_run_id=run_id,
            )
            # 先判断可安全延后，再执行恢复；否则已可恢复的旧快照也会
            # 抢占当前页面并消耗模型预算。关闭前仍须完整排空。
            defer_snapshot = (
                state.pending_capture_count == 0
                and state.nonterminal_evidence_request_count == 0
                and state.snapshot_evidence_request_count > 0
                and await self._can_defer_snapshot_requests(task=task, run_id=run_id, state=state)
            )
            if state.drained and defer_snapshot:
                return H5ActiveSessionInspectionSummary(
                    published_work_item_count=published_count,
                    drain_round_count=drain_round_count,
                    settled_work_item_count=settled_count,
                    deferred_snapshot_evidence_request_count=state.snapshot_evidence_request_count,
                )
            consumed: object | None = None
            if state.nonterminal_evidence_request_count == 0 and self._now() < deadline:
                consumed = await self._consumer.drain_available(
                    task_id=task,
                    consumer_agent_run_id=run_id,
                    session_dependency="active_session",
                    max_items=1,
                )
                await self._park_open_evidence_requests(
                    task_id=task,
                    agent_run_id=run_id,
                )
            probe_progressed = False
            if self._probe_drain is not None and self._now() < deadline:
                probe_progressed = await self._probe_drain.drain_one(
                    task_id=task,
                    agent_run_id=run_id,
                )
            recapture_progressed = False
            # 先完成已经绑定现场的 Probe；下一次恢复会推进 current_capture_id。
            if not probe_progressed and self._targeted_recapture_drain is not None and self._now() < deadline:
                recapture_progressed = await self._targeted_recapture_drain.drain_one(
                    task_id=task, agent_run_id=run_id, deadline=deadline,
                    include_snapshot=not defer_snapshot,
                    defer_recoverable_snapshots=True,
                )
                if recapture_progressed and self._probe_drain is not None and self._now() < deadline:
                    probe_progressed = await self._probe_drain.drain_one(
                        task_id=task, agent_run_id=run_id,
                    )
            if (
                _claimed_count(consumed) > 0
                or (consumed is not None and state.scheduled_work_item_count > 0)
                or recapture_progressed
                or probe_progressed
            ):
                if probe_progressed or recapture_progressed:
                    authority_promoted += await self._reconcile_coverage_authority(
                        task_id=task, agent_run_id=run_id,
                    )
                state = await self._state_reader.load_drain_state(
                    task_id=task,
                    agent_run_id=run_id,
                )
            followup_claimed = 0
            if (
                (recapture_progressed or probe_progressed)
                and state.nonterminal_evidence_request_count == 0
                and self._now() < deadline
            ):
                followup = await self._consumer.drain_available(
                    task_id=task,
                    consumer_agent_run_id=run_id,
                    session_dependency="active_session",
                    max_items=1,
                )
                await self._park_open_evidence_requests(
                    task_id=task,
                    agent_run_id=run_id,
                )
                followup_claimed = _claimed_count(followup)
                if followup_claimed > 0:
                    state = await self._state_reader.load_drain_state(
                        task_id=task,
                        agent_run_id=run_id,
                    )
            drain_round_count += 1
            settled = 0
            if (
                state.pending_capture_count == 0
                and state.nonterminal_evidence_request_count == 0
                and state.exhausted_work_item_count > 0
            ):
                settled = await self._settle_or_raise(
                    task_id=task,
                    agent_run_id=run_id,
                    pending_capture_count=state.pending_capture_count,
                    nonterminal_work_item_count=state.nonterminal_work_item_count,
                    nonterminal_evidence_request_count=(
                        state.nonterminal_evidence_request_count
                    ),
                    published_count=published_count,
                    drain_round_count=drain_round_count,
                    platform="H5 当前页面",
                    active_session_exhausted_only=True,
                )
                settled_count += settled
                # 数据库按工作项再次核对租约、在途请求、Run 与 epoch；快照请求
                # 不能阻止无关工作结算，但结算数量也不能代替离页前的权威复读。
                state = await self._state_reader.load_drain_state(
                    task_id=task, agent_run_id=run_id,
                )
            if state.drained and (
                state.snapshot_evidence_request_count == 0
                or await self._can_defer_snapshot_requests(task=task, run_id=run_id, state=state)
            ):
                return H5ActiveSessionInspectionSummary(
                    published_work_item_count=published_count,
                    drain_round_count=drain_round_count,
                    settled_work_item_count=settled_count,
                    deferred_snapshot_evidence_request_count=state.snapshot_evidence_request_count,
                )
            progressed = (
                published > 0
                or authority_promoted > 0
                or _completed_count(consumed) > 0
                or settled > 0
            )
            current = self._now()
            waited = max(waited, (current - started_at).total_seconds())
            outstanding = (
                state.pending_capture_count,
                state.nonterminal_work_item_count,
                state.nonterminal_evidence_request_count,
                state.snapshot_evidence_request_count,
            )
            # 领取、租约和未来重试只代表许可，不代表新增证据或正式结算。
            if (
                progressed
                or (last_outstanding is not None and sum(outstanding) < sum(last_outstanding))
                or (last_terminal_fact_count is not None
                    and state.terminal_fact_count > last_terminal_fact_count)
            ):
                last_progress_elapsed = waited
            last_outstanding = outstanding
            last_terminal_fact_count = state.terminal_fact_count
            stalled = waited - last_progress_elapsed
            holding_schedule = _has_future_schedule(state, current)
            holding_schedule_until = state.next_attempt_at if holding_schedule else None
            if stalled >= self._checkpoint_max_wait_seconds and not holding_schedule:
                break
            if progressed:
                continue
            remaining = min(
                self._max_wait_seconds - stalled,
                (
                    max(0.0, (state.next_attempt_at - current).total_seconds())
                    if holding_schedule else self._checkpoint_max_wait_seconds - stalled
                ),
                max(0.0, (deadline - current).total_seconds()),
            )
            if remaining <= 0:
                break
            delay = min(self._max_poll_seconds, remaining)
            if state.next_attempt_at is not None and state.next_attempt_at > current:
                delay = min(
                    delay,
                    max(0.05, (state.next_attempt_at - current).total_seconds()),
                )
            delay = min(remaining, max(0.05, delay))
            await self._sleep(delay)
            waited += delay

        state = await self._state_reader.load_drain_state(
            task_id=task,
            agent_run_id=run_id,
        )
        if state.drained and (
            state.snapshot_evidence_request_count == 0
            or await self._can_defer_snapshot_requests(task=task, run_id=run_id, state=state)
        ):
            return H5ActiveSessionInspectionSummary(
                published_work_item_count=published_count,
                drain_round_count=drain_round_count,
                settled_work_item_count=settled_count,
                deferred_snapshot_evidence_request_count=state.snapshot_evidence_request_count,
            )
        detail = (
            f"pending_captures={state.pending_capture_count}, "
            f"nonterminal_work_items={state.nonterminal_work_item_count}, "
            f"nonterminal_evidence_requests={state.nonterminal_evidence_request_count}, "
            f"snapshot_evidence_requests={state.snapshot_evidence_request_count}"
        )
        raise RuntimeError(f"H5 离开当前页面前 active-session 质检未排空：{detail}")

    async def _can_defer_snapshot_requests(
        self, *, task: str, run_id: str, state: H5ActiveSessionDrainState,
    ) -> bool:
        check = getattr(self._targeted_recapture_drain, "can_defer_snapshot_requests", None)
        if not callable(check) or state.snapshot_evidence_request_count == 0:
            return False
        return await check(
            task_id=task, agent_run_id=run_id,
            expected_request_count=state.snapshot_evidence_request_count,
        )

    async def drain_before_close(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> H5ActiveSessionInspectionSummary:
        task = str(task_id or "").strip()
        run_id = str(agent_run_id or "").strip()
        if not task or not run_id:
            raise ValueError("task_id 和 agent_run_id 不能为空")
        if deadline.utcoffset() is None:
            raise ValueError("deadline 必须带时区")
        published_count = 0
        drain_round_count = 0
        waited = 0.0
        stalled = 0.0
        started_at = self._now()
        last_progress_at = started_at
        last_outstanding: tuple[int, ...] | None = None
        last_terminal_fact_count: int | None = None
        while waited < self._max_wait_seconds:
            waited = max(waited, (self._now() - started_at).total_seconds())
            if waited >= self._max_wait_seconds:
                break
            if self._cancellation is not None:
                await self._cancellation.raise_if_requested(
                    task_id=task,
                    agent_run_id=run_id,
                    stage="active_session_inspection_drain",
                )
            if self._now() >= deadline:
                break
            authority_promoted = await self._reconcile_coverage_authority(
                task_id=task,
                agent_run_id=run_id,
            )
            if self._now() >= deadline:
                break
            published = await self._publisher.publish_pending(
                task_id=task,
                agent_run_id=run_id,
            )
            published_count += published
            await self._park_open_evidence_requests(task_id=task, agent_run_id=run_id)
            state = await self._state_reader.load_drain_state(
                task_id=task,
                agent_run_id=run_id,
            )
            consumed: object | None = None
            if state.nonterminal_evidence_request_count == 0 and self._now() < deadline:
                consumed = await self._consumer.drain_available(
                    task_id=task,
                    consumer_agent_run_id=run_id,
                    session_dependency="active_session",
                    max_items=1,
                )
                await self._park_open_evidence_requests(
                    task_id=task,
                    agent_run_id=run_id,
                )
            if (
                state.nonterminal_work_item_count == 0
                and state.nonterminal_evidence_request_count == 0
                and state.snapshot_evidence_request_count == 0
                and state.snapshot_work_item_count > 0
                and self._now() < deadline
            ):
                consumed = await self._consumer.drain_available(
                    task_id=task,
                    consumer_agent_run_id=run_id,
                    session_dependency="snapshot",
                    max_items=1,
                )
                await self._park_open_evidence_requests(task_id=task, agent_run_id=run_id)
            probe_progressed = False
            if self._probe_drain is not None and self._now() < deadline:
                probe_progressed = await self._probe_drain.drain_one(
                    task_id=task,
                    agent_run_id=run_id,
                )
            recapture_progressed = False
            if not probe_progressed and self._targeted_recapture_drain is not None and self._now() < deadline:
                recapture_progressed = await self._targeted_recapture_drain.drain_one(
                    task_id=task, agent_run_id=run_id, deadline=deadline, include_snapshot=True,
                )
                if recapture_progressed and self._probe_drain is not None and self._now() < deadline:
                    probe_progressed = await self._probe_drain.drain_one(
                        task_id=task, agent_run_id=run_id,
                    )
            if recapture_progressed or probe_progressed:
                authority_promoted += await self._reconcile_coverage_authority(
                    task_id=task,
                    agent_run_id=run_id,
                )
            if (
                _claimed_count(consumed) > 0
                or authority_promoted > 0
                or recapture_progressed
                or probe_progressed
            ):
                state = await self._state_reader.load_drain_state(
                    task_id=task,
                    agent_run_id=run_id,
                )
            drain_round_count += 1
            # Snapshot 判定也可能要求现场补采，须在关闭前消费并结算，不能只排空活会话计数。
            if state.close_drained:
                final_promoted = await self._reconcile_coverage_authority(
                    task_id=task,
                    agent_run_id=run_id,
                )
                if final_promoted > 0:
                    continue
                logger.info(
                    "task_id=%s run_id=%s H5 关闭前活会话规则已排空："
                    "published=%s rounds=%s waited=%.1fs。",
                    task,
                    run_id,
                    published_count,
                    drain_round_count,
                    waited,
                )
                return H5ActiveSessionInspectionSummary(
                    published_work_item_count=published_count,
                    drain_round_count=drain_round_count,
                )
            outstanding = (
                state.pending_capture_count,
                state.nonterminal_work_item_count,
                state.nonterminal_evidence_request_count,
                state.snapshot_work_item_count,
                state.snapshot_evidence_request_count,
            )
            remaining = min(
                self._max_wait_seconds - max(waited, (self._now() - started_at).total_seconds()),
                (deadline - self._now()).total_seconds(),
            )
            if remaining <= 0:
                break
            current = self._now()
            delay = min(self._max_poll_seconds, remaining)
            if state.next_attempt_at is not None and state.next_attempt_at > current:
                delay = min(
                    delay,
                    max(0.05, (state.next_attempt_at - current).total_seconds()),
                )
            delay = min(remaining, max(0.05, delay))
            if (
                last_outstanding is None
                or sum(outstanding) < sum(last_outstanding)
                or published > 0
                or authority_promoted > 0
                or _completed_count(consumed) > 0
                or (last_terminal_fact_count is not None
                    and state.terminal_fact_count > last_terminal_fact_count)
            ):
                stalled = 0.0
                last_progress_at = current
            elif sum(outstanding) > 0:
                # 查询、判定和补采耗时也算停滞时间，不能只累计短轮询的休眠。
                stalled = max(stalled + delay, (current - last_progress_at).total_seconds())
                if stalled >= self._grace_seconds and not _has_future_schedule(state, current):
                    if state.drained and state.snapshot_evidence_request_count == 0:
                        return self._defer_snapshot_judging(
                            task=task, run_id=run_id, state=state,
                            published_count=published_count, drain_round_count=drain_round_count,
                        )
                    settled = await self._settle_or_raise(
                        task_id=task,
                        agent_run_id=run_id,
                        pending_capture_count=state.pending_capture_count,
                        nonterminal_work_item_count=state.nonterminal_work_item_count,
                        nonterminal_evidence_request_count=(
                            state.nonterminal_evidence_request_count
                        ),
                        published_count=published_count,
                        drain_round_count=drain_round_count,
                        platform="H5",
                    )
                    if state.snapshot_work_item_count or state.snapshot_evidence_request_count:
                        # 结算已经改变规则/请求终态，关闭判断必须重新读取权威计数。
                        state = await self._state_reader.load_drain_state(
                            task_id=task, agent_run_id=run_id,
                        )
                        if state.drained and state.snapshot_evidence_request_count == 0:
                            return replace(
                                self._defer_snapshot_judging(
                                    task=task, run_id=run_id, state=state,
                                    published_count=published_count,
                                    drain_round_count=drain_round_count,
                                ),
                                settled_work_item_count=settled,
                            )
                        raise RuntimeError("H5 关闭前 Snapshot 判定或补采尚未排空，不能声称会话已安全关闭")
                    return H5ActiveSessionInspectionSummary(
                        published_work_item_count=published_count,
                        drain_round_count=drain_round_count,
                        settled_work_item_count=settled,
                    )
            last_outstanding = outstanding
            last_terminal_fact_count = state.terminal_fact_count
            await self._sleep(delay)
            waited += delay
        state = await self._state_reader.load_drain_state(
            task_id=task,
            agent_run_id=run_id,
        )
        if state.drained and state.snapshot_evidence_request_count == 0:
            return self._defer_snapshot_judging(
                task=task, run_id=run_id, state=state,
                published_count=published_count, drain_round_count=drain_round_count,
            )
        if state.snapshot_work_item_count or state.snapshot_evidence_request_count:
            raise RuntimeError("H5 关闭前 Snapshot 判定或补采尚未排空，不能声称会话已安全关闭")
        settled = await self._settle_or_raise(
            task_id=task,
            agent_run_id=run_id,
            pending_capture_count=state.pending_capture_count,
            nonterminal_work_item_count=state.nonterminal_work_item_count,
            nonterminal_evidence_request_count=state.nonterminal_evidence_request_count,
            published_count=published_count,
            drain_round_count=drain_round_count,
            platform="H5",
        )
        return H5ActiveSessionInspectionSummary(
            published_work_item_count=published_count,
            drain_round_count=drain_round_count,
            settled_work_item_count=settled,
        )

    @staticmethod
    def _defer_snapshot_judging(
        *, task: str, run_id: str, state: H5ActiveSessionDrainState,
        published_count: int, drain_round_count: int,
    ) -> H5ActiveSessionInspectionSummary:
        logger.info(
            "task_id=%s run_id=%s 现场请求已排空，%s 个 Snapshot 工作项继续异步判定；"
            "不视为质检完成，仍等待 Gate。", task, run_id, state.snapshot_work_item_count,
        )
        return H5ActiveSessionInspectionSummary(
            published_work_item_count=published_count,
            drain_round_count=drain_round_count,
            deferred_snapshot_work_item_count=state.snapshot_work_item_count,
        )

    async def _reconcile_coverage_authority(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> int:
        if self._coverage_authority is None:
            return 0
        return await self._coverage_authority.reconcile_available(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def _park_open_evidence_requests(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> int:
        if self._evidence_wait is None:
            return 0
        reopen = getattr(self._closure_settlement, "reopen_waiting_evidence", None)
        if callable(reopen):
            # 重启前已终结的请求也要重开判定，不能仅依赖本轮新 Probe 的唤醒。
            await reopen(task_id=task_id)
        return await self._evidence_wait.park_open_requests(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def _settle_or_raise(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        pending_capture_count: int,
        nonterminal_work_item_count: int,
        nonterminal_evidence_request_count: int,
        published_count: int,
        drain_round_count: int,
        platform: str,
        active_session_exhausted_only: bool = False,
    ) -> int:
        """排空窗口用尽：能结算就结算并继续关闭会话，没有结算能力才抛错。

        会话一关，活会话工作项不允许跨会话重放，再等也拿不到证据；按证据不足结算并留
        放弃原因，比让收口无限重试、浏览器与设备迟迟不释放更符合设计（01 专项 §6 步骤 9）。
        """
        detail = (
            f"pending_captures={pending_capture_count}, "
            f"nonterminal_work_items={nonterminal_work_item_count}, "
            f"nonterminal_evidence_requests={nonterminal_evidence_request_count}"
        )
        if nonterminal_evidence_request_count > 0:
            raise RuntimeError(f"{platform} Session 关闭前补证请求未排空：{detail}")
        settlement = self._closure_settlement
        if settlement is None:
            raise RuntimeError(
                f"{platform} Session 关闭前 active-session 规则未排空：{detail}"
            )
        # 页面检查点不能使用整个会话关闭的结算范围。
        checkpoint_options = (
            {"active_session_exhausted_only": True} if active_session_exhausted_only else {}
        )
        result = await settlement.settle_stalled_work_for_closure(
            task_id=task_id,
            active_session_agent_run_id=agent_run_id,
            **checkpoint_options,
        )
        logger.warning(
            "task_id=%s run_id=%s %s 活会话规则结算（%s），"
            "已按证据不足结算 %s 个工作项、%s 条规则，exhausted_only=%s：published=%s rounds=%s",
            task_id,
            agent_run_id,
            platform,
            detail,
            result.settled_work_item_count,
            result.settled_rule_count,
            active_session_exhausted_only,
            published_count,
            drain_round_count,
        )
        return result.settled_work_item_count


def _claimed_count(result: object) -> int:
    value = getattr(result, "claimed_count", 0)
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def _completed_count(result: object) -> int:
    """只有正式完成的 Judge 工作项才构成进展，不使用领取数量替代。"""
    value = getattr(result, "completed_count", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _has_future_schedule(state: H5ActiveSessionDrainState, current: datetime) -> bool:
    """合法租约只延迟抢占和结算，不刷新进展；始终受绝对窗口约束。"""
    return bool(
        state.scheduled_work_item_count > 0
        and state.next_attempt_at is not None
        and state.next_attempt_at > current
    )


def _is_pending_publishable(
    capture: H5EvidenceCapture,
    *,
    allow_web_final_handoff: bool = False,
) -> bool:
    """活会话增量发布的最小可判定条件；完整校验仍归收口交接门禁。"""
    if capture.session_status != "active":
        return False
    allowed_task_states = (
        {"open", "exploration_draining"}
        if allow_web_final_handoff
        else {"open"}
    )
    if capture.task_finalization_state not in allowed_task_states:
        return False
    if (
        capture.session_finalization_epoch is None
        or capture.task_finalization_epoch is None
        or capture.session_finalization_epoch != capture.task_finalization_epoch
    ):
        return False
    if capture.capture_role != _JUDGEABLE_CAPTURE_ROLE:
        return False
    if not allow_web_final_handoff and capture.inspection_ready is not True:
        return False
    if (
        not allow_web_final_handoff
        and capture.supplemental_evidence_source == "targeted_recapture"
    ):
        # 恢复 Capture 只用于证明原状态并派发 Probe；再次按普通页面发布会递归造请求。
        return False
    if not allow_web_final_handoff and capture.logical_state_previously_published:
        # 同一 H5 逻辑状态只执行一轮规则；原始 Capture 仍保留用于审计和证据归档。
        return False
    if str(capture.detection_status or "").strip() != "recognized":
        return False
    if not str(capture.page_type or "").strip():
        return False
    if not allow_web_final_handoff and not capture.rule_matching_allowed:
        # 不得绕过页面识别 Tool 的规则匹配门禁直接按底层页面类型发规则。
        return False
    status = str(capture.inspection_status or "").strip()
    if (
        status in _PUBLISHED_INSPECTION_STATUSES
        and not (
            allow_web_final_handoff
            and capture.task_finalization_state == "exploration_draining"
        )
    ):
        return False
    return True


def build_default_h5_live_inspection_publish_service() -> H5LiveInspectionPublishService:
    """Worker 唤醒消费组合根默认入口。"""
    return H5LiveInspectionPublishService(
        gate_reader=PostgresH5LiveInspectionTaskGateReader(),
        capture_reader=PostgresH5EvidenceCaptureReader(),
        publisher=build_default_app_capture_inspection_publisher(),
    )


def build_default_h5_active_session_inspection_drain_service(
    *,
    worker_instance_id: str,
    settings: Any,
    supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    evidence_request_repository: EvidenceRequestAdmissionRepository | None = None,
    capture_runtime: Any | None = None,
    detector: Any | None = None,
    assessment_repository: Any | None = None,
    recapture_budget: Any | None = None,
    cancellation: H5ActiveSessionCancellationPort | None = None,
    closed_session_checkpoint: Any | None = None,
    closed_session_deadline: datetime | None = None,
) -> H5ActiveSessionInspectionDrainService:
    """H5 生产 Runtime 的关闭前规则排空组合根。"""
    from services.api.app.agents.rule_judge.bootstrap import (
        build_h5_targeted_recapture_rule_judge_agent,
    )
    from services.api.app.agents.runtime.inspection_result_writer import (
        CurrentRuleDecisionWriter,
    )
    from services.api.app.agents.runtime.inspection_work_item_consumer import (
        InspectionWorkItemConsumer,
    )
    from services.api.app.application.evidence_request_admission import (
        EvidenceRequestAdmissionService,
    )
    from services.api.app.application.evidence_request_consumption import (
        EvidenceRequestConsumptionService,
    )
    from services.api.app.application.evidence_supplements import (
        EvidenceSupplementAttacher,
    )
    from services.api.app.application.explorer_turn import ExplorerTurnExecutor
    from services.api.app.application.h5_coverage_authority import (
        H5CoverageAuthorityReconciler,
    )
    from services.api.app.application.h5_probe_work import (
        H5ProbeCompletionService,
        H5ProbeDecisionAdapter,
        H5ProbeDrainService,
        H5ProbeWorkRunner,
    )
    from services.api.app.application.inspection_work_items import (
        InspectionWorkItemService,
    )
    from services.api.app.application.orchestration import QcOrchestrator
    from services.api.app.application.rule_result_reuse import (
        build_current_rule_reuse_planner,
        rule_result_reuse_enabled,
    )
    from services.api.app.application.supplement_input_set_reseal import (
        SupplementInputSetResealService,
    )
    from services.api.app.application.supplemental_evidence_policy import (
        build_current_supplemental_evidence_policy,
    )
    from services.api.app.infrastructure.db.evidence_input_set_repository import (
        PostgresEvidenceInputSetRepository,
    )
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.db.evidence_request_repository import (
        PostgresEvidenceRequestRepository,
    )
    from services.api.app.infrastructure.db.evidence_supplement_repository import (
        PostgresEvidenceSupplementRepository,
    )
    from services.api.app.infrastructure.db.explorer_work_repository import (
        PostgresExplorerWorkRepository,
        PostgresProbeWorkPublisher,
    )
    from services.api.app.infrastructure.db.h5_coverage_authority_repository import (
        PostgresH5CoverageAuthorityRepository,
    )
    from services.api.app.infrastructure.db.h5_probe_completion_repository import (
        PostgresH5ProbeCompletionRecoveryReader,
        PostgresH5ProbeEvidencePreparer,
    )
    from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
        PostgresH5ProbeTerminalSettlementRepository,
    )
    from services.api.app.infrastructure.db.h5_recapture_coalescing_repository import (
        PostgresH5RecaptureCoalescingRepository,
    )
    from services.api.app.infrastructure.db.h5_shared_probe_repository import (
        PostgresH5SharedProbeEvidenceReader,
    )
    from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
        H5ClaimedEvidenceRequestReference,
    )
    from services.api.app.infrastructure.db.inspection_work_item_repository import (
        PostgresInspectionWorkItemRepository,
    )

    capture_reader = PostgresH5EvidenceCaptureReader()
    work_item_repository = PostgresInspectionWorkItemRepository()
    work_item_service = InspectionWorkItemService(repository=work_item_repository)
    supplemental_policy = (
        supplemental_evidence_policy
        if supplemental_evidence_policy is not None
        else build_current_supplemental_evidence_policy(
            enabled=settings.supplemental_evidence.enabled
        )
    )
    request_repository = (
        evidence_request_repository
        if evidence_request_repository is not None
        else PostgresEvidenceRequestRepository(
            h5_snapshot_recovery=closed_session_checkpoint is not None,
        )
    )
    probe_queue = PostgresExplorerWorkRepository(
        supplemental_evidence_policy=supplemental_policy,
        evidence_request_repository=H5ClaimedEvidenceRequestReference(),
    )
    probe_orchestrator = QcOrchestrator(
        probe_publisher=PostgresProbeWorkPublisher(
            repository=probe_queue,
            supplemental_evidence_policy=supplemental_policy,
            emit_wakeup=False,
        )
    )
    probe_drain = None
    targeted_recapture_drain = None
    closed_session_cleanup = None
    coverage_authority = None
    if assessment_repository is not None:
        coverage_repository = PostgresH5CoverageAuthorityRepository()
        coverage_authority = H5CoverageAuthorityReconciler(
            candidates=coverage_repository,
            assessments=assessment_repository,
            snapshots=coverage_repository,
        )
    if (
        capture_runtime is not None
        and detector is not None
        and recapture_budget is not None
        and supplemental_policy.enabled
        and supplemental_policy.runtime_ready
    ):
        from services.api.app.agents.explorer.bootstrap import (
            build_default_h5_explorer_agent,
        )
        from services.api.app.application.app_evidence_manifest import (
            AppEvidenceManifestFinalizer,
        )
        from services.api.app.application.h5_evidence_manifest import (
            H5EvidenceManifestFinalizer,
        )
        from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
        from services.api.app.application.h5_targeted_recapture import (
            DEFAULT_H5_TARGETED_RECAPTURE_LEASE_SECONDS,
            H5TargetedRecaptureConsumer,
            H5TargetedRecaptureDrainService,
            H5TargetedRecaptureRuntimeFactory,
        )
        from services.api.app.infrastructure.browser.h5_evidence_recovery_runtime import (
            H5EvidenceRecoveryRuntime,
        )
        from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
            PostgresH5StateInvariantReader,
        )
        from services.api.app.infrastructure.db.page_graph_repository import (
            PostgresPageGraphRepository,
        )
        from services.api.app.infrastructure.db.restoration_attempt_repository import (
            PostgresRestorationAttemptRepository,
        )
        from services.api.app.infrastructure.storage.factory import (
            build_evidence_artifact_store,
        )

        page_graph = PostgresPageGraphRepository()
        recovery_runtime = H5EvidenceRecoveryRuntime(
            surface_sessions=capture_runtime.surface_sessions,
            capture_coordinator=capture_runtime.build_h5_probe_coordinator(
                supplemental_evidence_policy=supplemental_policy,
            ),
            detector=detector,
            page_graph=page_graph,
            captures=capture_runtime.h5_capture_recovery,
            logical_surface_effective=settings.workflow.h5_logical_surface_effective,
            logical_targets_effective=settings.workflow.h5_logical_targets_effective,
            h5_visual_coordinate_mode=settings.workflow.h5_visual_coordinate_runtime_mode,
        )
        if closed_session_checkpoint is not None:
            from services.api.app.infrastructure.browser.h5_new_session_recovery_runtime import (
                H5NewSessionRecoveryRuntime,
            )
            from services.api.app.infrastructure.db.h5_closed_session_context import (
                PostgresH5ClosedSessionContextReader,
            )
            if closed_session_deadline is None:
                raise ValueError("关闭后补采必须传入原 Work 的截止时间")
            recovery_runtime = H5NewSessionRecoveryRuntime(
                settle_interrupted_restorations=(
                    PostgresH5ClosedSessionContextReader().settle_interrupted_restorations
                ),
                capture_runtime=capture_runtime, checkpoint=closed_session_checkpoint,
                deadline=closed_session_deadline, surface_sessions=capture_runtime.surface_sessions,
                capture_coordinator=capture_runtime.build_h5_probe_coordinator(
                    supplemental_evidence_policy=supplemental_policy,
                    new_session_restoration=True,
                ), detector=detector, page_graph=page_graph,
                captures=capture_runtime.h5_capture_recovery,
                logical_surface_effective=settings.workflow.h5_logical_surface_effective,
                logical_targets_effective=settings.workflow.h5_logical_targets_effective,
                h5_visual_coordinate_mode=settings.workflow.h5_visual_coordinate_runtime_mode,
            )
            closed_session_cleanup = recovery_runtime.close_owned_sessions
        h5_explorer = build_default_h5_explorer_agent()
        goal_planner = H5ProbeGoalPlanner(
            delegate=h5_explorer, frontiers=recovery_runtime,
            snapshot_reader=capture_runtime.trusted_ui_snapshot_reader,
            h5_visual_coordinate_mode=settings.workflow.h5_visual_coordinate_runtime_mode,
        )

        manifests = PostgresEvidenceManifestRepository(
            permitted_task_statuses=("running", "review"),
        )
        manifest_finalizer = AppEvidenceManifestFinalizer(
            repository=manifests,
            artifact_store=build_evidence_artifact_store(settings.evidence_storage),
        )
        consumption = EvidenceRequestConsumptionService(
            requests=request_repository,
            manifests=manifests,
            attacher=EvidenceSupplementAttacher(
                repository=PostgresEvidenceSupplementRepository(
                    supplemental_evidence_policy=supplemental_policy,
                ),
                supplemental_evidence_policy=supplemental_policy,
            ),
            manifest_finalizer=manifest_finalizer,
            input_set_resealer=SupplementInputSetResealService(
                input_sets=PostgresEvidenceInputSetRepository(
                    permitted_task_statuses=("running", "review"),
                    permitted_finalization_states=(
                        "open",
                        "exploration_draining",
                        "session_draining",
                    ),
                ),
                manifests=manifests,
                work_items=work_item_repository,
                requests=request_repository,
            ),
        )
        completion = H5ProbeCompletionService(
            consumption=consumption,
            recovery=PostgresH5ProbeCompletionRecoveryReader(),
            evidence_preparation=PostgresH5ProbeEvidencePreparer(
                manifests=manifests,
                detector=detector,
                finalizer=H5EvidenceManifestFinalizer(
                    repository=manifests, finalizer=manifest_finalizer,
                ),
            ),
        )
        probe_runner = H5ProbeWorkRunner(
            session_command_gate=capture_runtime.session_command_gate,
            queue=probe_queue,
            turn_executor=ExplorerTurnExecutor(
                explorer=H5ProbeDecisionAdapter(
                    delegate=h5_explorer,
                    goal_planner=goal_planner,
                ),
                capture_coordinator=capture_runtime.build_h5_probe_coordinator(
                    supplemental_evidence_policy=supplemental_policy,
                    probe_action_roles=True,
                ),
                before_side_effect=(
                    _closed_session_before_side_effect(closed_session_checkpoint)
                    if closed_session_checkpoint is not None else None
                ),
            ),
            request_consumption=consumption,
            completion=completion,
            lease_owner=f"{worker_instance_id}:h5-probe",
            lease_seconds=settings.inspection_work_items.lease_seconds,
            retry_delay_seconds=settings.inspection_work_items.retry_base_seconds,
            policy=supplemental_policy,
            shared_evidence=(PostgresH5SharedProbeEvidenceReader()
                             if closed_session_checkpoint is None else None),
        )
        probe_drain = H5ProbeDrainService(
            runner=probe_runner,
            completion=completion,
            terminal_settlement=PostgresH5ProbeTerminalSettlementRepository(),
            inspection_wakeup=work_item_service,
            policy=supplemental_policy,
        )
        from services.api.app.application.h5_closed_session_recapture import (
            drain_h5_closed_session_snapshots,
        )
        from services.api.app.infrastructure.db.h5_closed_session_context import (
            PostgresH5ClosedSessionContextReader,
        )
        h5_recapture_repository = PostgresH5ClosedSessionContextReader()
        from services.api.app.infrastructure.db.h5_llm_budget_repository import (
            PostgresH5LLMBudgetRepository,
        )
        targeted_recapture_drain = H5TargetedRecaptureDrainService(
            consumer=H5TargetedRecaptureConsumer(
                session_command_gate=capture_runtime.session_command_gate,
                requests=request_repository,
                runtime_factory=H5TargetedRecaptureRuntimeFactory(
                    contexts=h5_recapture_repository,
                    page_graph=page_graph,
                    recovery_runtime=recovery_runtime,
                    captures=capture_runtime.h5_capture_recovery,
                    snapshots=PostgresH5StateInvariantReader(),
                    manifest_finalizer=H5EvidenceManifestFinalizer(
                        repository=manifests,
                        finalizer=manifest_finalizer,
                    ),
                    restoration_attempts=PostgresRestorationAttemptRepository(),
                    probe_orchestrator=probe_orchestrator,
                    closed_session_replay=drain_h5_closed_session_snapshots,
                ),
                policy=supplemental_policy,
                lease_owner=f"{worker_instance_id}:h5-recapture",
                lease_seconds=max(
                    settings.inspection_work_items.lease_seconds,
                    DEFAULT_H5_TARGETED_RECAPTURE_LEASE_SECONDS,
                ),
                retry_delay_seconds=settings.inspection_work_items.retry_base_seconds,
                gap_history=(PostgresH5RecaptureCoalescingRepository()
                             if closed_session_checkpoint is None else None),
                budget_reader=PostgresH5LLMBudgetRepository(),
            ),
            scope_reader=h5_recapture_repository,
            budget=recapture_budget,
            inspection_wakeup=work_item_service,
            observation_sharing=(recovery_runtime if closed_session_checkpoint is None else None),
        )
    publisher = H5LiveInspectionPublishService(
        gate_reader=PostgresH5LiveInspectionTaskGateReader(),
        capture_reader=capture_reader,
        publisher=build_default_app_capture_inspection_publisher(),
    )
    return H5ActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=InspectionWorkItemConsumer(
            service=work_item_service,
            lease_owner=f"{worker_instance_id}:h5-active-inspection",
            lease_seconds=settings.inspection_work_items.lease_seconds,
            retry_backoff_seconds=settings.inspection_work_items.retry_base_seconds,
            result_writer=CurrentRuleDecisionWriter(
                orchestrator=probe_orchestrator,
                request_admission=EvidenceRequestAdmissionService(
                    repository=request_repository,
                    policy=supplemental_policy,
                ),
                # H5 请求由上面的真实恢复链路派发，禁止走兼容的直接 Probe 路由。
                route_after_admission=False,
                supplemental_evidence_policy=supplemental_policy,
            ),
            supplemental_evidence_policy=supplemental_policy,
            reuse_planner=build_current_rule_reuse_planner(
                enabled=rule_result_reuse_enabled(settings)
            ),
            judge=(
                build_h5_targeted_recapture_rule_judge_agent()
                if supplemental_policy.enabled
                else None
            ),
        ),
        state_reader=PostgresH5ActiveSessionInspectionStateReader(
            capture_reader=capture_reader,
        ),
        # 排空窗口用尽时按证据不足结算再关会话，不把收口卡在「规则未排空」上。
        closure_settlement=work_item_service,
        targeted_recapture_drain=targeted_recapture_drain,
        probe_drain=probe_drain,
        cancellation=cancellation,
        evidence_wait=PostgresH5InspectionEvidenceWaitRepository(),
        coverage_authority=coverage_authority,
        closed_session_cleanup=closed_session_cleanup,
    )


def _closed_session_before_side_effect(checkpoint: Any) -> Any:
    async def check(**kwargs: Any) -> None:
        await checkpoint()
    return check
