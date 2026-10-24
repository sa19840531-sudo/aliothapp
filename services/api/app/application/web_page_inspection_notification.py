"""Web 单 Capture 的可靠正式质检通知。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
    select_stable_publication_batch,
)
from services.api.app.application.outbox import OutboxRelayResult
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.domain.protocol import build_event_message_id
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    FinalizationRecoveryFenceError,
)

logger = logging.getLogger(__name__)

_REQUIRED_AFTER_ARTIFACTS = frozenset({"screenshot", "dom", "visible_text"})
_REQUIRED_BEFORE_ARTIFACTS = frozenset({"screenshot", "dom"})
# 通知按 assessment 版本读取冻结闭集；生命周期与 unknown 永不进入业务发布。
_PUBLISHED_NOTIFICATION_PAGE_TYPES_BY_VERSION = {
    "web-page-type-assessment.v3": frozenset({
        "home", "content_list", "content_detail", "video_list", "video_detail",
    }),
    "web-page-type-assessment.v4": frozenset({
        "home", "content_list", "content_detail", "video_list", "video_detail",
        "activity", "search_page", "collection_page",
    }),
}


def _recovery_fence_kwargs(
    token: str | None,
    generation: int | None,
    expires_at: datetime | None,
) -> dict[str, object]:
    if token is None:
        return {}
    return {
        "recovery_lease_token": token,
        "recovery_generation": generation,
        "recovery_lease_expires_at": expires_at,
    }


class WebPageInspectionInventoryStatus(StrEnum):
    """Web 通知写入共享库存时使用的本地稳定状态。"""

    PASS = "pass"


@dataclass(frozen=True, slots=True)
class WebPageInspectionInventoryItem:
    """Web 通知向共享库存端口提交的最小证据事实。"""

    action_execution_id: str
    before_capture_id: str | None
    after_capture_id: str | None
    after_capture: object | None
    status: WebPageInspectionInventoryStatus
    reason_codes: tuple[str, ...]
    verification_summary: dict[str, object]
    task_id: str
    agent_run_id: str
    session_id: str
    page_type: str | None
    page_state: str | None
    required_artifacts: tuple[str, ...]
    found_artifacts: tuple[str, ...]
    published_inspection_count: int = 0
    before_manifest_id: str | None = None
    before_manifest_hash: str | None = None
    after_manifest_id: str | None = None
    after_manifest_hash: str | None = None
    authority_source: str = "pass_manifest"


@dataclass(frozen=True, kw_only=True)
class WebPageInspectionEvidence:
    """通知门禁所需的单个 Web Capture 持久化事实。"""

    task_id: str
    agent_run_id: str
    session_id: str
    session_owner: str | None
    session_status: str | None
    task_status: str | None
    task_finalization_state: str | None
    task_finalization_epoch: int | None
    session_finalization_epoch: int | None
    action_finalization_epoch: int | None
    run_status: str | None
    current_run: bool
    capture_id: str
    capture_status: str | None
    capture_role: str | None
    action_execution_id: str | None
    parent_capture_id: str | None
    page_type: str | None
    page_state: str | None
    detection_status: str | None
    assessment_record_id: str | None
    assessment_hash: str | None
    assessment_schema_version: str | None
    artifact_types: tuple[str, ...]
    before_artifact_types: tuple[str, ...]
    manifest_id: str | None
    manifest_hash: str | None
    manifest_quality_status: str | None
    before_manifest_id: str | None
    before_manifest_hash: str | None
    before_manifest_quality_status: str | None
    execution_mode: str | None
    notification_mode: str | None
    inspection_evidence_role: str | None = None
    logical_page_id: str | None = None
    content_fingerprint: str | None = None


@dataclass(frozen=True, kw_only=True)
class WebPageInspectionNotificationResult:
    """通知结论；Redis 故障不得抹掉已提交的 Publication。"""

    status: str
    reason_code: str
    publication_id: str | None = None
    publication_created: bool = False
    retryable: bool = False
    diagnostic_type: str | None = None


class WebPageInspectionEvidenceReader(Protocol):
    """按 Web 实际 Session 身份读取单 Capture 门禁事实。"""

    async def read_for_notification(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
    ) -> WebPageInspectionEvidence | None: ...

    async def list_ready_capture_ids(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[str, ...]: ...

    async def list_ready_capture_ids_for_final_handoff(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> tuple[str, ...]: ...

    async def record_notification_diagnostic(self, **kwargs: object) -> None: ...


class WebPageInspectionInventory(Protocol):
    """复用现有证据库存的最小写入端口。"""

    async def upsert_inventory_item(
        self,
        *,
        item: WebPageInspectionInventoryItem,
    ) -> object: ...


class WebInspectionPublicationWriter(Protocol):
    """复用现有 Publication 与 Outbox 原子写入端口。"""

    async def create_for_capture(self, **kwargs: object) -> object: ...


class WebInspectionPublicationWakeup(Protocol):
    """Publication 提交后的尽力唤醒端口。"""

    async def wake(
        self,
        *,
        task_id: str,
        publication_id: str,
        outbox_message_id: str,
        aggregate_version: int,
    ) -> None: ...


class WebEvidenceManifestFinalizer(Protocol):
    """复用 Manifest 裁决能力，不在通知器内解释对象正文。"""

    async def finalize_capture(self, *, task_id: str, capture_id: str) -> None: ...

    async def finalize_session(
        self,
        *,
        task_id: str,
        session_id: str,
        agent_run_id: str,
    ) -> int: ...


class WebPublicationWakeupNotConfirmedError(RuntimeError):
    """当前 Web Publication 的 Outbox 消息未获发布回执。"""


class ReliableDeliveryWebPublicationWakeup:
    """用现有 Outbox Relay 做一次低延迟尽力唤醒。"""

    def __init__(
        self,
        relay_once: Callable[[], Awaitable[tuple[OutboxRelayResult, ...]]],
    ) -> None:
        if not callable(relay_once):
            raise TypeError("relay_once 必须可调用")
        self._relay_once = relay_once

    async def wake(
        self,
        *,
        task_id: str,
        publication_id: str,
        outbox_message_id: str,
        aggregate_version: int,
    ) -> None:
        expected_message_id = build_event_message_id(
            message_type=H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
            task_id=task_id,
            aggregate_type="h5_inspection_publication",
            aggregate_id=publication_id,
            aggregate_version=aggregate_version,
        )
        if outbox_message_id != expected_message_id:
            raise WebPublicationWakeupNotConfirmedError(
                "Web Publication Outbox 消息身份与持久化版本不一致"
            )

        relay_results = await self._relay_once()
        current_result = next(
            (
                result
                for result in relay_results
                if isinstance(result, OutboxRelayResult)
                and result.claimed
                and result.message_id == outbox_message_id
            ),
            None,
        )
        if current_result is None:
            raise WebPublicationWakeupNotConfirmedError(
                "本轮 Relay 未确认当前 Web Publication Outbox 消息"
            )
        if (
            current_result.rescheduled
            or current_result.error is not None
            or current_result.receipt is None
        ):
            detail = current_result.error or "当前消息缺少 Redis 发布回执"
            raise WebPublicationWakeupNotConfirmedError(
                f"当前 Web Publication Outbox 消息未发布：{detail}"
            )


class WebPageInspectionNotification:
    """门禁、建库存、原子建 Publication/Outbox，再尽力唤醒消费者。"""

    def __init__(
        self,
        *,
        reader: WebPageInspectionEvidenceReader,
        inventory: WebPageInspectionInventory,
        publication_writer: WebInspectionPublicationWriter,
        wakeup: WebInspectionPublicationWakeup | None = None,
        manifest_finalizer: WebEvidenceManifestFinalizer | None = None,
        sequence_repository: object | None = None,
        capability_revision_repository: object | None = None,
    ) -> None:
        self._reader = reader
        self._inventory = inventory
        self._publication_writer = publication_writer
        self._wakeup = wakeup
        self._manifest_finalizer = manifest_finalizer
        self._sequence_repository = sequence_repository
        self._capability_revision_repository = capability_revision_repository

    async def notify_readiness_failure(self, *, task_id, agent_run_id, session_id,
                                      source_capture_id, page_role, result):
        """只封存已有可信自然 Capture；失败序列不制造稳定分类或补拍。"""
        if self._sequence_repository is None or self._manifest_finalizer is None:
            return {"status": "evidence_insufficient", "reason_code": "WEB_SEQUENCE_COMPOSITION_MISSING"}
        captures = await self._sequence_repository.capture_ids_for_failure(
            task_id=task_id, agent_run_id=agent_run_id, session_id=session_id,
            evidence_capture_ids=result.evidence_capture_ids,
        )
        if len(captures) < 4:
            return {"status": "evidence_insufficient", "reason_code": "WEB_SEQUENCE_SAMPLES_INSUFFICIENT"}
        for capture_id in captures:
            await self._manifest_finalizer.finalize_capture(task_id=task_id, capture_id=capture_id)
        return await self._sequence_repository.register_failure(
            task_id=task_id, agent_run_id=agent_run_id, session_id=session_id,
            source_capture_id=source_capture_id, evidence_capture_ids=result.evidence_capture_ids,
            page_role=page_role, readiness_status=result.status.value,
        )

    async def notify_readiness_interaction(self, *, task_id,agent_run_id,session_id,
                                          source_capture_id,page_role,result):
        """成功 readiness 的动作闭包独立发布，不接管原 stable 分类通知。"""
        from dataclasses import asdict

        if (not result.is_ready or not result.interaction_outcomes or not result.interaction_action_refs
            or self._sequence_repository is None or self._manifest_finalizer is None):
            return {"status":"evidence_insufficient","reason_code":"WEB_INTERACTION_ORIGINAL_ACTION_MISSING"}
        captures = await self._sequence_repository.capture_ids_for_failure(task_id=task_id,agent_run_id=agent_run_id,
            session_id=session_id,evidence_capture_ids=result.evidence_capture_ids)
        if len(captures) < 4:
            return {"status":"evidence_insufficient","reason_code":"WEB_SEQUENCE_SAMPLES_INSUFFICIENT"}
        for capture_id in captures:
            await self._manifest_finalizer.finalize_capture(task_id=task_id,capture_id=capture_id)
        if self._capability_revision_repository is not None:
            await self._capability_revision_repository.record_from_capture(task_id=task_id,agent_run_id=agent_run_id,
                session_id=session_id,capture_id=result.ready_capture_id)
        return await self._sequence_repository.register_interaction(task_id=task_id,agent_run_id=agent_run_id,
            session_id=session_id,source_capture_id=source_capture_id,evidence_capture_ids=result.evidence_capture_ids,
            page_role=page_role,readiness_status=result.status.value,
            interaction_outcomes=tuple(asdict(item) for item in result.interaction_outcomes),
            interaction_action_refs=result.interaction_action_refs)

    async def notify_ready_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        final_handoff_epoch: int | None = None,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> WebPageInspectionNotificationResult:
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        session = _require_text(session_id, "session_id")
        capture = _require_text(capture_id, "capture_id")
        if final_handoff_epoch is not None and (
            isinstance(final_handoff_epoch, bool)
            or not isinstance(final_handoff_epoch, int)
            or final_handoff_epoch < 0
        ):
            raise ValueError("final_handoff_epoch 必须为非负整数或 None")
        try:
            evidence = await self._reader.read_for_notification(
                task_id=task,
                agent_run_id=run_id,
                session_id=session,
                capture_id=capture,
            )
        except Exception as exc:  # noqa: BLE001 - 读取失败必须转为可补偿诊断
            return await self._retryable_result(
                task_id=task,
                agent_run_id=run_id,
                capture_id=capture,
                reason_code="WEB_INSPECTION_EVIDENCE_READ_RETRYABLE",
                error=exc,
            )
        if evidence is None:
            return _blocked("WEB_CAPTURE_MISSING")

        if evidence.execution_mode != "full_inspection" or evidence.notification_mode != "enforced":
            return _blocked("WEB_INSPECTION_NOTIFICATION_DISABLED")

        wake_sequences = getattr(self._sequence_repository, "wake_after_classification", None)
        if callable(wake_sequences):
            await wake_sequences(task_id=task,agent_run_id=run_id,session_id=session,capture_id=capture)

        if _context_reason(
            evidence,
            task_id=task,
            agent_run_id=run_id,
            session_id=session,
            capture_id=capture,
            final_handoff_epoch=final_handoff_epoch,
        ) is None:
            evidence = await self._finalize_manifests(evidence)

        reason_code = _gate_reason(
            evidence,
            task_id=task,
            agent_run_id=run_id,
            session_id=session,
            capture_id=capture,
            final_handoff_epoch=final_handoff_epoch,
        )
        if reason_code is not None:
            return _blocked(reason_code)

        try:
            inventory_snapshot = await self._inventory.upsert_inventory_item(
                item=_inventory_item(evidence),
                **_recovery_fence_kwargs(
                    recovery_lease_token,
                    recovery_generation,
                    recovery_lease_expires_at,
                ),
            )
            inventory_status = getattr(inventory_snapshot, "status", None)
            inventory_value = getattr(inventory_status, "value", inventory_status)
            if inventory_value != WebPageInspectionInventoryStatus.PASS.value:
                raise RuntimeError("Web Capture 库存未进入 pass")
            publication = await self._publication_writer.create_for_capture(
                task_id=task,
                source_agent_run_id=run_id,
                capture_id=capture,
                page_type_assessment_id=_require_text(
                    evidence.assessment_record_id or "",
                    "assessment_record_id",
                ),
                assessment_hash=_require_text(
                    evidence.assessment_hash or "",
                    "assessment_hash",
                ),
                page_type=_require_text(evidence.page_type or "", "page_type"),
                page_state=_optional_text(evidence.page_state) or "stable",
                strict_web_gate=True,
                strict_web_final_handoff_epoch=final_handoff_epoch,
                logical_page_id=evidence.logical_page_id,
                content_fingerprint=evidence.content_fingerprint,
                **_recovery_fence_kwargs(
                    recovery_lease_token,
                    recovery_generation,
                    recovery_lease_expires_at,
                ),
            )
            publication_id = _require_text(
                getattr(publication, "publication_id", ""),
                "publication_id",
            )
            created = bool(getattr(publication, "created", False))
            row_version = getattr(publication, "row_version", None)
            if (
                isinstance(row_version, bool)
                or not isinstance(row_version, int)
                or row_version < 0
            ):
                raise RuntimeError("Web Publication 缺少有效 row_version")
            outbox_message_id = _require_text(
                getattr(publication, "outbox_message_id", ""),
                "outbox_message_id",
            )
            outbox_status = _require_text(
                getattr(publication, "outbox_status", ""),
                "outbox_status",
            )
        except Exception as exc:  # noqa: BLE001 - Capture 已提交，发布失败留待重投
            if isinstance(exc, FinalizationRecoveryFenceError):
                raise
            return await self._retryable_result(
                task_id=task,
                agent_run_id=run_id,
                capture_id=capture,
                reason_code="WEB_INSPECTION_PUBLICATION_RETRYABLE",
                error=exc,
            )

        if self._wakeup is not None and outbox_status != "published":
            try:
                await self._wakeup.wake(
                    task_id=task,
                    publication_id=publication_id,
                    outbox_message_id=outbox_message_id,
                    aggregate_version=row_version,
                )
            except Exception as exc:  # noqa: BLE001 - Outbox 已提交，Redis 仅影响唤醒延迟
                await self._record_diagnostic(
                    task_id=task,
                    agent_run_id=run_id,
                    capture_id=capture,
                    reason_code="WEB_INSPECTION_WAKEUP_RETRYABLE",
                    error=exc,
                    publication_id=publication_id,
                )
                return WebPageInspectionNotificationResult(
                    status="published_wakeup_pending",
                    reason_code="WEB_INSPECTION_WAKEUP_RETRYABLE",
                    publication_id=publication_id,
                    publication_created=created,
                    retryable=True,
                    diagnostic_type=type(exc).__name__,
                )

        return WebPageInspectionNotificationResult(
            status="published",
            reason_code=(
                "WEB_INSPECTION_PUBLICATION_CREATED"
                if created
                else "WEB_INSPECTION_PUBLICATION_EXISTS"
            ),
            publication_id=publication_id,
            publication_created=created,
        )

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int:
        """供通用 Capture/Evidence 唤醒链路补扫当前 Web Run。"""

        if limit is not None:
            raise ValueError("有界发布必须调用 publish_pending_batch")
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        capture_ids = await self._reader.list_ready_capture_ids(
            task_id=task,
            agent_run_id=run_id,
        )
        return await self._publish_capture_ids(
            task=task,
            run_id=run_id,
            capture_ids=capture_ids,
        )

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult:
        """按稳定 Capture ID 有界补扫，单 Capture 失败不阻塞尾部。"""

        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        capture_ids = await self._reader.list_ready_capture_ids(
            task_id=task,
            agent_run_id=run_id,
        )
        batch = select_stable_publication_batch(
            capture_ids,
            identity=str,
            limit=limit,
            cursor=cursor,
        )
        created = await self._publish_capture_ids(
            task=task,
            run_id=run_id,
            capture_ids=batch.items,
        )
        return LiveInspectionPublicationBatchResult(
            attempted_count=len(batch.items),
            created_count=created,
            has_more=batch.has_more,
            next_cursor=batch.next_cursor,
        )

    async def _publish_capture_ids(
        self,
        *,
        task: str,
        run_id: str,
        capture_ids: tuple[str, ...],
    ) -> int:
        created = 0
        for capture_id in capture_ids:
            result = await self.notify_ready_capture(
                task_id=task,
                agent_run_id=run_id,
                session_id=f"web:{run_id}",
                capture_id=capture_id,
            )
            created += int(result.publication_created)
        return created

    async def publish_pending_for_final_handoff(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> int:
        """在停止新探索后按当前收口 owner/epoch 幂等补发遗漏 Publication。"""

        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if self._manifest_finalizer is not None:
            await self._manifest_finalizer.finalize_session(
                task_id=task,
                session_id=f"web:{run_id}",
                agent_run_id=run_id,
                **_recovery_fence_kwargs(
                    recovery_lease_token,
                    recovery_generation,
                    recovery_lease_expires_at,
                ),
            )
        capture_ids = await self._reader.list_ready_capture_ids_for_final_handoff(
            task_id=task,
            agent_run_id=run_id,
            finalization_epoch=finalization_epoch,
        )
        created = 0
        for capture_id in capture_ids:
            result = await self.notify_ready_capture(
                task_id=task,
                agent_run_id=run_id,
                session_id=f"web:{run_id}",
                capture_id=capture_id,
                final_handoff_epoch=finalization_epoch,
                recovery_lease_token=recovery_lease_token,
                recovery_generation=recovery_generation,
                recovery_lease_expires_at=recovery_lease_expires_at,
            )
            created += int(result.publication_created)
        return created

    async def _finalize_manifests(
        self,
        evidence: WebPageInspectionEvidence,
    ) -> WebPageInspectionEvidence:
        if self._manifest_finalizer is None:
            return evidence
        if (
            evidence.manifest_quality_status == "pass"
            and evidence.before_manifest_quality_status == "pass"
        ):
            return evidence
        capture_ids = tuple(
            sorted(
                {
                    value
                    for value in (evidence.parent_capture_id, evidence.capture_id)
                    if _optional_text(value) is not None
                }
            )
        )
        try:
            for capture_id in capture_ids:
                await self._manifest_finalizer.finalize_capture(
                    task_id=evidence.task_id,
                    capture_id=capture_id,
                )
            refreshed = await self._reader.read_for_notification(
                task_id=evidence.task_id,
                agent_run_id=evidence.agent_run_id,
                session_id=evidence.session_id,
                capture_id=evidence.capture_id,
            )
            return refreshed or evidence
        except Exception:  # noqa: BLE001 - 门禁会把未形成 pass Manifest 稳定阻断
            logger.warning(
                "task_id=%s capture_id=%s Web Manifest 裁决失败。",
                evidence.task_id,
                evidence.capture_id,
                exc_info=True,
            )
            return evidence

    async def _retryable_result(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        capture_id: str,
        reason_code: str,
        error: Exception,
    ) -> WebPageInspectionNotificationResult:
        await self._record_diagnostic(
            task_id=task_id,
            agent_run_id=agent_run_id,
            capture_id=capture_id,
            reason_code=reason_code,
            error=error,
            publication_id=None,
        )
        return WebPageInspectionNotificationResult(
            status="retryable",
            reason_code=reason_code,
            retryable=True,
            diagnostic_type=type(error).__name__,
        )

    async def record_result(
        self, *, task_id: str, agent_run_id: str, capture_id: str,
        result: WebPageInspectionNotificationResult,
    ) -> None:
        """分类提交后保存公开状态；诊断失败不能回滚分类事实。"""
        try:
            await self._reader.record_notification_diagnostic(
                task_id=task_id, agent_run_id=agent_run_id, capture_id=capture_id,
                status=result.status, reason_code=result.reason_code,
                diagnostic_type=result.diagnostic_type,
                publication_id=result.publication_id, retryable=result.retryable,
            )
        except Exception:  # noqa: BLE001 - 可观测状态不得改变分类结果
            logger.warning("Web 分类后通知状态写入失败。", exc_info=True)

    async def _record_diagnostic(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        capture_id: str,
        reason_code: str,
        error: Exception,
        publication_id: str | None,
    ) -> None:
        try:
            await self._reader.record_notification_diagnostic(
                task_id=task_id,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                reason_code=reason_code,
                diagnostic_type=type(error).__name__,
                publication_id=publication_id,
                retryable=True,
            )
        except Exception:  # noqa: BLE001 - 诊断写入不能覆盖原始补偿结论
            logger.warning(
                "task_id=%s capture_id=%s Web 通知诊断写入失败。",
                task_id,
                capture_id,
                exc_info=True,
            )


class WebPageInspectionNotifyingClassifier:
    """在页型桥接事务返回后通知，不把 Judge 结论纳入分类调用。"""

    def __init__(
        self,
        *,
        classifier: object,
        notification: WebPageInspectionNotification,
        task_id: str,
        agent_run_id: str,
        session_id: str,
    ) -> None:
        self._classifier = classifier
        self._notification = notification
        self._task_id = _require_text(task_id, "task_id")
        self._agent_run_id = _require_text(agent_run_id, "agent_run_id")
        self._session_id = _require_text(session_id, "session_id")

    async def classify(self, capture: object) -> object:
        classify = getattr(self._classifier, "classify", None)
        if not callable(classify):
            raise TypeError("Web 页型桥接分类器必须提供 classify")
        assessment = await classify(capture)
        capture_id = _require_text(getattr(capture, "capture_id", ""), "capture_id")
        try:
            result = await self._notification.notify_ready_capture(
                task_id=self._task_id,
                agent_run_id=self._agent_run_id,
                session_id=self._session_id,
                capture_id=capture_id,
            )
            await self._notification.record_result(
                task_id=self._task_id, agent_run_id=self._agent_run_id,
                capture_id=capture_id, result=result,
            )
        except Exception:  # noqa: BLE001 - 通知异常不得把已提交分类降级为导航失败
            logger.exception(
                "task_id=%s capture_id=%s Web 增量质检通知异常，探索继续。",
                self._task_id,
                capture_id,
            )
        return assessment


def _gate_reason(
    evidence: WebPageInspectionEvidence,
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture_id: str,
    final_handoff_epoch: int | None = None,
) -> str | None:
    if evidence.inspection_evidence_role != "inspection":
        return "WEB_CAPTURE_INSPECTION_ROLE_MISSING"
    if not _REQUIRED_AFTER_ARTIFACTS.issubset(set(evidence.artifact_types)) or not (
        _REQUIRED_BEFORE_ARTIFACTS.issubset(set(evidence.before_artifact_types))
    ):
        return "WEB_CAPTURE_ARTIFACT_MISSING"
    if (
        evidence.manifest_quality_status != "pass"
        or evidence.before_manifest_quality_status != "pass"
        or not _optional_text(evidence.manifest_id)
        or not _optional_text(evidence.manifest_hash)
        or not _optional_text(evidence.before_manifest_id)
        or not _optional_text(evidence.before_manifest_hash)
    ):
        return "WEB_CAPTURE_MANIFEST_NOT_PASS"
    page_types = _PUBLISHED_NOTIFICATION_PAGE_TYPES_BY_VERSION.get(
        evidence.assessment_schema_version or ""
    )
    if (
        evidence.detection_status != "recognized"
        or page_types is None
        or not _optional_text(evidence.assessment_record_id)
        or not _optional_text(evidence.assessment_hash)
    ):
        return "WEB_PAGE_TYPE_ASSESSMENT_NOT_RECOGNIZED"
    if evidence.page_type not in page_types:
        return "WEB_PAGE_TYPE_NOT_CANONICAL"
    return _context_reason(
        evidence,
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        capture_id=capture_id,
        final_handoff_epoch=final_handoff_epoch,
    )


def _context_reason(
    evidence: WebPageInspectionEvidence,
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture_id: str,
    final_handoff_epoch: int | None = None,
) -> str | None:
    expected_session_id = f"web:{agent_run_id}"
    epochs = (
        evidence.task_finalization_epoch,
        evidence.session_finalization_epoch,
        evidence.action_finalization_epoch,
    )
    expected_finalization_state = (
        "exploration_draining" if final_handoff_epoch is not None else "open"
    )
    accepted_run_statuses = (
        {"pending", "running", "done", "failed", "aborted"}
        if final_handoff_epoch is not None
        else {"pending", "running"}
    )
    ready = (
        evidence.task_id == task_id
        and evidence.agent_run_id == agent_run_id
        and evidence.capture_id == capture_id
        and session_id == expected_session_id
        and evidence.session_id == expected_session_id
        and evidence.session_owner == "web-v2-workflow"
        and evidence.session_status == "active"
        and evidence.task_status == "running"
        and evidence.task_finalization_state == expected_finalization_state
        and (
            final_handoff_epoch is None
            or evidence.task_finalization_epoch == final_handoff_epoch
        )
        and evidence.run_status in accepted_run_statuses
        and evidence.current_run
        and evidence.capture_status == "captured"
        and evidence.capture_role == "after_action"
        and _optional_text(evidence.action_execution_id) is not None
        and _optional_text(evidence.parent_capture_id) is not None
        and all(value is not None for value in epochs)
        and len(set(epochs)) == 1
    )
    return None if ready else "WEB_CAPTURE_CONTEXT_NOT_CURRENT"


def _inventory_item(
    evidence: WebPageInspectionEvidence,
) -> WebPageInspectionInventoryItem:
    found = tuple(
        sorted(
            {
                *(f"after:{item}" for item in evidence.artifact_types),
                *(f"before:{item}" for item in evidence.before_artifact_types),
            }
        )
    )
    return WebPageInspectionInventoryItem(
        action_execution_id=_require_text(
            evidence.action_execution_id or "",
            "action_execution_id",
        ),
        before_capture_id=_require_text(
            evidence.parent_capture_id or "",
            "parent_capture_id",
        ),
        after_capture_id=evidence.capture_id,
        after_capture=None,
        status=WebPageInspectionInventoryStatus.PASS,
        reason_codes=(),
        verification_summary={
            "source": "web_page_inspection_notification",
            "assessment_schema_version": evidence.assessment_schema_version,
            "manifest_quality_status": evidence.manifest_quality_status,
            "before_manifest_quality_status": evidence.before_manifest_quality_status,
        },
        task_id=evidence.task_id,
        agent_run_id=evidence.agent_run_id,
        session_id=evidence.session_id,
        page_type=evidence.page_type,
        page_state=_optional_text(evidence.page_state) or "stable",
        required_artifacts=(
            "before:dom",
            "before:screenshot",
            "after:dom",
            "after:screenshot",
            "after:visible_text",
        ),
        found_artifacts=found,
        before_manifest_id=evidence.before_manifest_id,
        before_manifest_hash=evidence.before_manifest_hash,
        after_manifest_id=evidence.manifest_id,
        after_manifest_hash=evidence.manifest_hash,
        authority_source="pass_manifest",
    )


def _blocked(reason_code: str) -> WebPageInspectionNotificationResult:
    return WebPageInspectionNotificationResult(
        status="blocked",
        reason_code=reason_code,
    )


def _require_text(value: object, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} 不能为空")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "ReliableDeliveryWebPublicationWakeup",
    "WebInspectionPublicationWakeup",
    "WebPageInspectionEvidence",
    "WebPageInspectionInventoryItem",
    "WebPageInspectionInventoryStatus",
    "WebPageInspectionNotification",
    "WebPageInspectionNotificationResult",
    "WebPageInspectionNotifyingClassifier",
]
