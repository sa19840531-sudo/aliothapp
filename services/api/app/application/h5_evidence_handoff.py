"""H5 探索证据向仅留证或正式质检阶段交接的门禁。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from services.api.app.application.h5_evidence_inventory import (
    H5EvidenceArtifactRef,
    H5EvidenceInventoryItem,
    H5EvidenceInventorySnapshot,
    H5EvidenceInventoryStatus,
)
from services.api.app.domain.task import H5InspectionNotificationMode, TaskExecutionMode

_JUDGEABLE_CAPTURE_ROLES = frozenset({"after_action"})
_REQUIRED_ARTIFACT_TYPES = frozenset({"screenshot", "dom"})


@dataclass(frozen=True, kw_only=True)
class H5EvidenceCapture:
    """当前任务和 Run 下用于页面判定与规则质检的不可变 Capture 摘要。"""

    capture_id: str
    page_type: str | None
    page_state: str | None
    detection_status: str | None
    capture_role: str
    url: str
    action_execution_id: str | None
    parent_capture_id: str | None
    before_url: str
    inspection_status: str | None = None
    artifact_types: tuple[str, ...] = ()
    before_artifact_types: tuple[str, ...] = ()
    task_id: str | None = None
    agent_run_id: str | None = None
    session_id: str | None = None
    session_owner: str | None = None
    session_status: str | None = None
    task_finalization_state: str | None = None
    session_finalization_epoch: int | None = None
    task_finalization_epoch: int | None = None
    supplemental_evidence_source: str | None = None
    artifact_refs: tuple[H5EvidenceArtifactRef, ...] = ()
    before_artifact_refs: tuple[H5EvidenceArtifactRef, ...] = ()
    page_type_assessment_id: str | None = None
    assessment_hash: str | None = None
    before_manifest_id: str | None = None
    before_manifest_hash: str | None = None
    before_manifest_quality_status: str | None = None
    after_manifest_id: str | None = None
    after_manifest_hash: str | None = None
    after_manifest_quality_status: str | None = None
    logical_identity_fingerprint: str | None = None
    logical_state_fingerprint: str | None = None
    logical_state_previously_published: bool = False
    rule_matching_allowed: bool = True
    inspection_ready: bool | None = None


@dataclass(frozen=True, kw_only=True)
class H5EvidenceHandoffSummary:
    """证据交接结果；不包含 DOM、截图或其他敏感原文。"""

    evidence_ready: bool
    judgeable_capture_count: int
    page_types: tuple[str, ...]
    published_work_item_count: int
    excluded_active_session_rule_count: int = 0
    total_count: int = 0
    pass_count: int = 0
    insufficient_count: int = 0
    failed_count: int = 0
    publication_count: int = 0


@dataclass(frozen=True, kw_only=True)
class _ObjectVerification:
    """逐侧制品校验结果；摘要不包含对象正文。"""

    status: str
    reason_codes: tuple[str, ...]
    artifact_summary: tuple[dict[str, object], ...]
    found_artifacts: tuple[str, ...]


class H5EvidenceNotReadyError(RuntimeError):
    """H5 已持久化证据未满足交接门禁。"""


class H5EvidenceCaptureReader(Protocol):
    """按任务和 Run 读取 H5 可判定 Capture 库存。"""

    async def list_judgeable_captures(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[H5EvidenceCapture]: ...


class H5EvidenceManifestFinalizerPort(Protocol):
    """探索收口后逐 Capture 裁决 Manifest；不参与探索动作门禁。"""

    async def finalize_capture(self, *, task_id: str, capture_id: str) -> None: ...


class CaptureInspectionPublishResult(Protocol):
    """正式质检发布结果的最小读取合同。"""

    published_count: int
    excluded_active_session_rule_count: int
    prepared_commands: tuple[object, ...]
    excluded_active_session_rules: tuple[dict[str, object], ...]


class CaptureInspectionPublisher(Protocol):
    """把已判定 Capture 发布为正式快照质检工作。"""

    async def publish_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        visual_page_type: str | None,
        page_state: str = "stable",
        agent_run_id: str | None = None,
        snapshot_only: bool = False,
        active_session_only: bool = False,
        prepare_only: bool = False,
    ) -> CaptureInspectionPublishResult: ...


class H5InspectionPublicationWriteResult(Protocol):
    """权威 Publication 创建结果的最小合同。"""

    publication_id: str
    created: bool


class H5InspectionPublicationWriter(Protocol):
    """把合格 Capture 转为持久化通知待办。"""

    async def create_for_capture(self, **kwargs: object) -> H5InspectionPublicationWriteResult: ...

    async def record_shadow_for_capture(
        self, **kwargs: object
    ) -> H5InspectionPublicationWriteResult: ...


class H5ActiveSessionCoverageReader(Protocol):
    """读取 legacy off 路径已有活会话规则终态，避免把成功执行误报为缺口。"""

    async def count_unresolved_active_session_rules_for_capture(
        self, **kwargs: object
    ) -> int: ...


class H5EvidenceObjectVerifierPort(Protocol):
    """对象存储完整性校验的应用层端口。"""

    async def verify(self, **kwargs: object) -> object: ...


class InventoryPersistencePort(Protocol):
    """逐预期动作持久化库存事实的端口。"""

    async def upsert_inventory_item(
        self, *, item: H5EvidenceInventoryItem
    ) -> H5EvidenceInventorySnapshot: ...

    async def mark_published(
        self, *, action_execution_id: str, published_count: int
    ) -> H5EvidenceInventorySnapshot: ...


class H5EvidenceHandoffPort(Protocol):
    """Worker 使用的 H5 证据交接端口。"""

    async def prepare(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        execution_mode: TaskExecutionMode,
        notification_mode: H5InspectionNotificationMode = H5InspectionNotificationMode.OFF,
    ) -> H5EvidenceHandoffSummary: ...


class H5EvidenceHandoffService:
    """验证 H5 证据库存，并按执行模式决定是否发布正式质检工作。"""

    def __init__(
        self,
        *,
        reader: H5EvidenceCaptureReader,
        publisher: CaptureInspectionPublisher,
        object_verifier: H5EvidenceObjectVerifierPort,
        inventory: InventoryPersistencePort,
        publication_writer: H5InspectionPublicationWriter | None = None,
        active_session_coverage_reader: H5ActiveSessionCoverageReader | None = None,
        manifest_finalizer: H5EvidenceManifestFinalizerPort | None = None,
    ) -> None:
        self._reader = reader
        self._publisher = publisher
        self._object_verifier = object_verifier
        self._inventory = inventory
        self._publication_writer = publication_writer
        self._active_session_coverage_reader = active_session_coverage_reader
        self._manifest_finalizer = manifest_finalizer

    async def prepare(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        execution_mode: TaskExecutionMode,
        notification_mode: H5InspectionNotificationMode = H5InspectionNotificationMode.OFF,
    ) -> H5EvidenceHandoffSummary:
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        if not isinstance(execution_mode, TaskExecutionMode):
            raise TypeError("execution_mode 必须为 TaskExecutionMode")
        if not isinstance(notification_mode, H5InspectionNotificationMode):
            raise TypeError("notification_mode 必须为 H5InspectionNotificationMode")
        captures = await self._reader.list_judgeable_captures(
            task_id=task,
            agent_run_id=run_id,
        )
        if self._manifest_finalizer is not None:
            capture_ids = {
                capture_id
                for capture in captures
                for capture_id in (capture.parent_capture_id, capture.capture_id)
                # 未提交动作 Capture 的 capture_id 由读取器用 action-execution
                # 逻辑串占位，没有 Manifest 行且无法按 uuid 绑定查询参数，
                # 仅裁决真实 Capture（真实回归：任务 ff5b5446 收口 DataError）。
                if capture_id and _is_capture_uuid(capture_id)
            }
            for capture_id in sorted(capture_ids):
                await self._manifest_finalizer.finalize_capture(
                    task_id=task,
                    capture_id=capture_id,
                )
            captures = await self._reader.list_judgeable_captures(
                task_id=task,
                agent_run_id=run_id,
            )
        # 库存逐项判定；缺证据是业务不足，不能抹掉已经合格的快照。
        persisted: list[tuple[H5EvidenceCapture, H5EvidenceInventorySnapshot]] = []
        for capture in captures:
            status = H5EvidenceInventoryStatus.PASS
            reasons: tuple[str, ...] = ()
            verification = _ObjectVerification(
                status="pass",
                reason_codes=(),
                artifact_summary=(),
                found_artifacts=(),
            )
            if not _has_expected_identity(capture, task, run_id):
                status = H5EvidenceInventoryStatus.FAILED
                reasons = ("capture_identity_mismatch",)
            else:
                try:
                    _validate_capture(capture)
                except H5EvidenceNotReadyError:
                    status = H5EvidenceInventoryStatus.INSUFFICIENT
                    reasons = ("capture_evidence_insufficient",)
                verification = await self._verify_object_refs(
                    capture=capture, task_id=task, agent_run_id=run_id
                )
                status = H5EvidenceInventoryStatus(
                    _combine_verification_status(status.value, verification.status)
                )
                if verification.status == "failed":
                    reasons = _reason_codes(
                        "evidence_integrity_failed", *reasons, *verification.reason_codes
                    )
                elif verification.status == "insufficient":
                    reasons = _reason_codes(*reasons, *verification.reason_codes)
            snapshot = await self._persist_item(
                capture=capture,
                status=status,
                reasons=reasons,
                verification=verification,
            )
            persisted.append((capture, snapshot))
        valid = [
            (capture, snapshot)
            for capture, snapshot in persisted
            if snapshot.status is H5EvidenceInventoryStatus.PASS
        ]
        insufficient_count = sum(
            snapshot.status is H5EvidenceInventoryStatus.INSUFFICIENT for _, snapshot in persisted
        )
        failed_count = sum(
            snapshot.status is H5EvidenceInventoryStatus.FAILED for _, snapshot in persisted
        )
        page_types = tuple(sorted({str(capture.page_type).strip() for capture, _ in valid}))
        published_count = 0
        publication_count = 0
        excluded_active_session_rule_count = 0
        if execution_mode is TaskExecutionMode.FULL_INSPECTION:
            for capture, _snapshot in valid:
                if not capture.rule_matching_allowed:
                    # 页面识别权威禁止规则匹配时，只保留探索证据和覆盖事实。
                    continue
                if _is_targeted_recapture_capture(capture):
                    # 恢复快照只用于等价证明和后续 Probe，不能再次进入普通规则质检。
                    continue
                if capture.logical_state_previously_published:
                    # 活会话已按同一逻辑状态发布过规则；收口只保留重复 Capture 的证据，
                    # 不得再创建第二份 Publication 并把已完成规则误报成会话缺口。
                    continue
                if notification_mode is H5InspectionNotificationMode.ENFORCED:
                    if self._publication_writer is None:
                        raise RuntimeError("H5 enforced 通知模式未配置 Publication Writer")
                    await self._publication_writer.create_for_capture(
                        task_id=task,
                        source_agent_run_id=run_id,
                        capture_id=capture.capture_id,
                        page_type_assessment_id=_require_text(
                            capture.page_type_assessment_id or "",
                            "capture.page_type_assessment_id",
                        ),
                        assessment_hash=_require_text(
                            capture.assessment_hash or "",
                            "capture.assessment_hash",
                        ),
                        page_type=_require_text(capture.page_type or "", "capture.page_type"),
                        page_state=(capture.page_state or "stable").strip() or "stable",
                    )
                    publication_count += 1
                    continue
                if notification_mode is H5InspectionNotificationMode.SHADOW:
                    if self._publication_writer is None:
                        raise RuntimeError("H5 shadow 通知模式未配置 Publication Writer")
                    plan = await self._publisher.publish_for_capture(
                        task_id=task,
                        capture_id=capture.capture_id,
                        visual_page_type=capture.page_type,
                        page_state=(capture.page_state or "stable").strip() or "stable",
                        agent_run_id=run_id,
                        snapshot_only=True,
                        prepare_only=True,
                    )
                    await self._publication_writer.record_shadow_for_capture(
                        task_id=task,
                        source_agent_run_id=run_id,
                        capture_id=capture.capture_id,
                        page_type_assessment_id=_require_text(
                            capture.page_type_assessment_id or "",
                            "capture.page_type_assessment_id",
                        ),
                        assessment_hash=_require_text(
                            capture.assessment_hash or "",
                            "capture.assessment_hash",
                        ),
                        page_type=_require_text(capture.page_type or "", "capture.page_type"),
                        page_state=(capture.page_state or "stable").strip() or "stable",
                        result=plan,
                    )
                    publication_count += 1
                    continue
                action_id = capture.action_execution_id or capture.capture_id
                result = await self._publisher.publish_for_capture(
                    task_id=task,
                    capture_id=capture.capture_id,
                    visual_page_type=capture.page_type,
                    page_state=(capture.page_state or "stable").strip() or "stable",
                    agent_run_id=run_id,
                    snapshot_only=True,
                )
                created_count = sum(
                    bool(item.get("created"))
                    for item in getattr(result, "items", ())
                    if isinstance(item, dict)
                )
                published_count += (
                    created_count if getattr(result, "items", ()) else int(result.published_count)
                )
                if int(result.published_count) > 0:
                    await self._mark_published(
                        action_execution_id=action_id,
                        published_count=int(result.published_count),
                    )
                excluded_count = int(
                    getattr(result, "excluded_active_session_rule_count", 0)
                )
                excluded_rules = tuple(
                    item
                    for item in getattr(result, "excluded_active_session_rules", ())
                    if isinstance(item, dict)
                )
                if excluded_count != len(excluded_rules):
                    raise RuntimeError("H5 off active-session 排除计数与规则快照不一致")
                coverage_reader = self._active_session_coverage_reader
                if excluded_count > 0 and coverage_reader is not None:
                    excluded_count = await (
                        coverage_reader.count_unresolved_active_session_rules_for_capture(
                            task_id=task,
                            source_agent_run_id=run_id,
                            capture_id=capture.capture_id,
                            excluded_rules=excluded_rules,
                        )
                    )
                excluded_active_session_rule_count += excluded_count

        return H5EvidenceHandoffSummary(
            evidence_ready=(
                insufficient_count == 0
                and failed_count == 0
                and bool(captures)
                and len(valid) == len(persisted)
            ),
            judgeable_capture_count=len(valid),
            page_types=page_types,
            published_work_item_count=published_count,
            excluded_active_session_rule_count=(excluded_active_session_rule_count),
            total_count=len(captures),
            pass_count=len(valid),
            insufficient_count=insufficient_count,
            failed_count=failed_count,
            publication_count=publication_count,
        )

    async def _persist_item(
        self,
        *,
        capture: H5EvidenceCapture,
        status: H5EvidenceInventoryStatus,
        reasons: tuple[str, ...],
        verification: _ObjectVerification,
    ) -> H5EvidenceInventorySnapshot:
        return await self._inventory.upsert_inventory_item(
            item=H5EvidenceInventoryItem(
                action_execution_id=capture.action_execution_id or capture.capture_id,
                before_capture_id=capture.parent_capture_id,
                after_capture_id=(
                    capture.capture_id if capture.capture_role == "after_action" else None
                ),
                after_capture=None,
                status=status,
                reason_codes=reasons,
                verification_summary={
                    "page_type": capture.page_type,
                    "page_state": capture.page_state,
                    "artifacts": list(verification.artifact_summary),
                },
                task_id=_require_text(capture.task_id or "", "capture.task_id"),
                agent_run_id=_require_text(capture.agent_run_id or "", "capture.agent_run_id"),
                session_id=_require_text(capture.session_id or "", "capture.session_id"),
                page_type=capture.page_type,
                page_state=capture.page_state,
                required_artifacts=(
                    "before:dom",
                    "before:screenshot",
                    "after:dom",
                    "after:screenshot",
                ),
                found_artifacts=verification.found_artifacts,
                before_manifest_id=capture.before_manifest_id,
                before_manifest_hash=capture.before_manifest_hash,
                after_manifest_id=capture.after_manifest_id,
                after_manifest_hash=capture.after_manifest_hash,
                authority_source=(
                    "pass_manifest"
                    if capture.before_manifest_quality_status == "pass"
                    and capture.after_manifest_quality_status == "pass"
                    else "manifest_unverified"
                ),
            )
        )

    async def _mark_published(
        self, *, action_execution_id: str, published_count: int
    ) -> H5EvidenceInventorySnapshot:
        return await self._inventory.mark_published(
            action_execution_id=action_execution_id,
            published_count=published_count,
        )

    async def _verify_object_refs(
        self, *, capture: H5EvidenceCapture, task_id: str, agent_run_id: str
    ) -> _ObjectVerification:
        """读取截图和 DOM 的实际字节；未接入旧记录仍由字段完整性兼容判断。"""
        sides = (
            (
                "before",
                capture.before_artifact_refs,
                capture.before_artifact_types,
                capture.parent_capture_id,
            ),
            (
                "after",
                capture.artifact_refs,
                capture.artifact_types,
                capture.capture_id,
            ),
        )
        reasons: list[str] = []
        summaries: list[dict[str, object]] = []
        found: list[str] = []
        status = "pass"
        manifest_facts = (
            (
                "before",
                capture.before_manifest_id,
                capture.before_manifest_hash,
                capture.before_manifest_quality_status,
            ),
            (
                "after",
                capture.after_manifest_id,
                capture.after_manifest_hash,
                capture.after_manifest_quality_status,
            ),
        )
        for side, manifest_id, manifest_hash, quality_status in manifest_facts:
            if not manifest_id or not manifest_hash:
                reasons.append(f"{side}:manifest_missing")
                status = _combine_verification_status(status, "insufficient")
            elif quality_status != "pass":
                reasons.append(f"{side}:manifest_not_pass")
                status = _combine_verification_status(status, "insufficient")
        for side, refs, declared_types, expected_capture_id in sides:
            if not expected_capture_id:
                reasons.append(f"{side}:capture_missing")
                for artifact_type in sorted(_REQUIRED_ARTIFACT_TYPES):
                    reasons.append(f"{side}:{artifact_type}_missing")
                status = _combine_verification_status(status, "insufficient")
                continue
            types = {artifact.artifact_type for artifact in refs}
            declared = {str(item).strip() for item in declared_types if str(item).strip()}
            for artifact_type in sorted(_REQUIRED_ARTIFACT_TYPES):
                qualified = f"{side}:{artifact_type}"
                if artifact_type not in types or artifact_type not in declared:
                    reasons.append(f"{qualified}_missing")
                    status = _combine_verification_status(status, "insufficient")
            for artifact in sorted(
                refs, key=lambda value: (value.artifact_type, value.artifact_id)
            ):
                found.append(f"{side}:{artifact.artifact_type}")
                result = await self._object_verifier.verify(
                    artifact=artifact,
                    expected_task_id=task_id,
                    expected_agent_run_id=agent_run_id,
                    expected_capture_id=expected_capture_id,
                )
                result_status = str(getattr(getattr(result, "status", None), "value", "failed"))
                raw_reason_code = getattr(result, "reason_code", None)
                reason_code = (
                    None
                    if result_status == "pass"
                    else str(raw_reason_code or "verification_unknown")
                )
                summaries.append(
                    {
                        "side": side,
                        "artifact_type": artifact.artifact_type,
                        "artifact_id": artifact.artifact_id,
                        "capture_id": artifact.capture_id,
                        "size_bytes": artifact.size_bytes,
                        "checksum": artifact.checksum,
                        "status": result_status,
                        "reason_code": reason_code,
                        "verification": dict(getattr(result, "summary", {}) or {}),
                    }
                )
                if result_status != "pass":
                    reasons.append(reason_code)
                    status = _combine_verification_status(status, result_status)
        return _ObjectVerification(
            status=status,
            reason_codes=_reason_codes(*reasons),
            artifact_summary=tuple(summaries),
            found_artifacts=tuple(sorted(set(found))),
        )


def _validate_capture(capture: H5EvidenceCapture) -> None:
    capture_id = _require_text(capture.capture_id, "capture_id")
    if str(capture.session_id or "").startswith("h5:") and capture.inspection_ready is not True:
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 未证明本帧正文就绪")
    role = str(capture.capture_role or "").strip()
    if role not in _JUDGEABLE_CAPTURE_ROLES:
        raise H5EvidenceNotReadyError(
            f"Capture {capture_id} 的角色 {role or '<empty>'} 不能作为可判定证据"
        )
    if str(capture.detection_status or "").strip() != "recognized":
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 的页面判定尚未成功")
    if not str(capture.page_type or "").strip():
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 缺少 page_type")
    if not str(capture.url or "").strip():
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 缺少动作后 URL")
    if not str(capture.action_execution_id or "").strip():
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 缺少动作执行身份")
    if not str(capture.parent_capture_id or "").strip():
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 缺少动作前 Capture 身份")
    if not str(capture.before_url or "").strip():
        raise H5EvidenceNotReadyError(f"Capture {capture_id} 缺少动作前 URL")


def _require_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    return text


def _is_capture_uuid(value: str | None) -> bool:
    """真实 Capture 主键必为 uuid；逻辑占位串（action-execution:…）不参与裁决。"""
    if not value:
        return False
    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def _has_expected_identity(capture: H5EvidenceCapture, task_id: str, run_id: str) -> bool:
    canonical = f"h5:{run_id}"
    web_canonical = f"web-v2:{run_id}"
    recovery_pattern = re.compile(rf"^h5:{re.escape(run_id)}:recovery:[0-9a-f]{{20}}$")
    canonical_identity = capture.session_id == canonical and capture.session_owner == "h5-workflow"
    web_identity = (
        capture.session_id == web_canonical and capture.session_owner == "web-workflow"
    )
    recovery_identity = (
        isinstance(capture.session_id, str)
        and recovery_pattern.fullmatch(capture.session_id) is not None
        and capture.session_owner == f"h5-browser-recovery:{run_id}"
    )
    return (
        capture.task_id == task_id
        and capture.agent_run_id == run_id
        and (canonical_identity or web_identity or recovery_identity)
    )


def _is_targeted_recapture_capture(capture: H5EvidenceCapture) -> bool:
    return (
        str(capture.supplemental_evidence_source or "").strip().casefold()
        == "targeted_recapture"
    )


def _combine_verification_status(current: str, candidate: str) -> str:
    """完整性失败优先于业务证据不足，保证聚合结论稳定。"""
    if "failed" in {current, candidate}:
        return "failed"
    if "insufficient" in {current, candidate}:
        return "insufficient"
    return "pass"


def _reason_codes(*codes: str) -> tuple[str, ...]:
    """去重并排序原因码，避免输入顺序造成库存版本漂移。"""
    return tuple(sorted({str(code).strip() for code in codes if str(code).strip()}))
