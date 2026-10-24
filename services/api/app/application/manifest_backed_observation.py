"""E20：清单→附件列表→checksum 比对→ObservationPacket 的校验读取链路。

冻结依据：
- E20 验收：每次读取完成 checksum 比对，并生成只进入审计载荷的 ManifestReadDecision
- E02 条款 1.1：判定侧只读清单结论，不重算齐全性

**只进入审计载荷**的边界（本模块的核心约束）：
`ObservationPacket.packet_payload` 会被整体序列化进模型提示词
（semantic_rule_judge 直接 json.dumps 整个 packet_payload），
因此读取裁决**绝不写入 packet_payload**；它只经
`attach_manifest_read_decision` 进入 `judge_input` 审计投影
（落 `qc_rule_execution.judge_input`，不进模型输入）。

链路组成：
1. 页面上下文（URL/标题/页面类型/动作日志/视觉观察）仍取自 Capture 事实；
2. **证据附件改由清单加载**（E19 ManifestBackedPacketLoader），
   附件表不再参与判定侧证据读取——E22 移除附件直查后内层只余上下文查询；
3. 每份附件在加载时完成大小与 sha256 比对（E19 已实施），
   本模块把比对结果固化成可审计的 ManifestReadDecision。

过渡期说明：内层仓储当前仍会查一次附件表，这与 E23 双读影子对账的形态一致；
主读取路径切换与附件直查移除分别属 E24 与 E22。
无清单与校验失败的拒判与告警接线属 E21：本模块记录裁决后原样抛出异常。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.application.manifest_backed_packet import (
    ManifestBackedPacketLoader,
    ManifestBackedReadError,
    ManifestFactPacket,
    ManifestIntegrityError,
    ManifestUnavailableError,
)
from services.api.app.application.observation_packet import (
    ManifestQualityProjection,
    ObservationArtifact,
    ObservationJourneyStep,
    ObservationSource,
)
from services.api.app.domain.evidence import EvidenceQualityStatus

MANIFEST_READ_DECISION_SCHEMA_VERSION = "manifest-read-decision.v1"

# judge_input 审计投影中承载读取裁决的键
MANIFEST_READ_AUDIT_KEY = "manifest_read"

# 审计投影中逐条列出的附件上限，超出只报数量（审计载荷必须有界）
_MAX_AUDITED_ARTIFACTS = 50

# 同时保留裁决的 Capture 数上限，避免 Worker 长跑无界增长
_MAX_TRACKED_DECISIONS = 32


class ManifestReadOutcome(StrEnum):
    """一次清单化证据读取的审计结论。

    E02 条款 2.1：pending、控制面未就绪与齐全性不通过必须**分别**表达，
    避免把技术故障伪装成证据不足；取值与门禁侧
    `EvidenceExitBusinessOutcome` 同名同义（E02 条款 1.7 禁止词汇互串）。
    E02 条款 1.5：载荷预算超限也在此表达，且不得报「证据不足」。
    """

    VERIFIED = "verified"
    # 清单裁决为 fail：齐全性确实不通过（唯一可报证据不足的分支）
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    # 清单裁决为 pending：证据提交在途
    EVIDENCE_PENDING = "evidence_pending"
    # 无清单或清单归属不符：控制面未就绪
    CONTROL_PLANE_INCOMPLETE = "control_plane_incomplete"
    # 证据缺失、不可读或与清单校验和不一致
    EVIDENCE_INTEGRITY_FAILED = "evidence_integrity_failed"
    # 判定侧载荷预算超限（E02 条款 1.5，与齐全性无关）
    PAYLOAD_BUDGET_EXCEEDED = "payload_budget_exceeded"


class CaptureContextSource(Protocol):
    """页面上下文读取端口（既有 ObservationPacketRepository 结构性满足）。

    E22：优先使用 `load_capture_context`（不查附件表）；
    仅当实现未提供该方法时才回退 `load_source`。
    """

    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None: ...

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
    ) -> str: ...


@dataclass(frozen=True, kw_only=True)
class ManifestArtifactReadRecord:
    """单份附件的清单比对留痕（不含原文，只留身份与结论）。"""

    artifact_id: str
    artifact_type: str
    role: str
    checksum: str
    size_bytes: int
    verified: bool

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "role": self.role,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "verified": self.verified,
        }


@dataclass(frozen=True, kw_only=True)
class ManifestReadDecision:
    """一次清单化读取的审计裁决；只进审计载荷，不进事实包与模型输入。"""

    task_id: str
    capture_id: str
    outcome: ManifestReadOutcome
    read_at: datetime
    manifest_id: str | None = None
    manifest_version: int | None = None
    manifest_hash: str | None = None
    quality_status: EvidenceQualityStatus | None = None
    artifacts: tuple[ManifestArtifactReadRecord, ...] = ()
    reason: str | None = None

    @property
    def verified(self) -> bool:
        return self.outcome is ManifestReadOutcome.VERIFIED

    def to_audit_payload(self) -> dict[str, Any]:
        """有界审计投影：只含身份、比对结论与原因，不含证据原文。"""
        recorded = self.artifacts[:_MAX_AUDITED_ARTIFACTS]
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_READ_DECISION_SCHEMA_VERSION,
            "outcome": self.outcome.value,
            "task_id": self.task_id,
            "capture_id": self.capture_id,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "quality_status": (
                self.quality_status.value if self.quality_status is not None else None
            ),
            "read_at": self.read_at.isoformat(),
            "checksum_verified_count": sum(1 for item in self.artifacts if item.verified),
            "artifact_count": len(self.artifacts),
            "artifacts": [item.to_audit_payload() for item in recorded],
        }
        if len(self.artifacts) > len(recorded):
            payload["artifacts_truncated"] = len(self.artifacts) - len(recorded)
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def attach_manifest_read_decision(
    audit_payload: dict[str, Any],
    decision: ManifestReadDecision | None,
) -> dict[str, Any]:
    """把读取裁决挂进 judge_input 审计投影；不得用于 packet_payload。"""
    if decision is None:
        return audit_payload
    return {**audit_payload, MANIFEST_READ_AUDIT_KEY: decision.to_audit_payload()}


def build_read_decision(
    packet: ManifestFactPacket,
    *,
    read_at: datetime,
) -> ManifestReadDecision:
    """从已通过校验的事实包固化读取裁决。"""
    return ManifestReadDecision(
        task_id=packet.task_id,
        capture_id=packet.capture_id,
        outcome=ManifestReadOutcome.VERIFIED,
        read_at=read_at,
        manifest_id=packet.manifest_id,
        manifest_version=packet.manifest_version,
        manifest_hash=packet.manifest_hash,
        quality_status=packet.quality_status,
        artifacts=tuple(
            ManifestArtifactReadRecord(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                role=item.role,
                checksum=item.checksum,
                size_bytes=item.size_bytes,
                verified=True,
            )
            for item in packet.artifacts
        ),
    )


class ManifestBackedObservationRepository:
    """以清单为证据来源的 ObservationPacketRepository 装配件。

    可直接注入 `ObservationPacketService(repository=...)`：
    页面上下文沿用内层仓储，证据附件改由清单加载并完成 checksum 比对。
    """

    def __init__(
        self,
        *,
        inner: CaptureContextSource,
        packet_loader: ManifestBackedPacketLoader,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._packet_loader = packet_loader
        self._now = now or (lambda: datetime.now(UTC))
        self._decisions: dict[tuple[str, str], ManifestReadDecision] = {}

    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None:
        """读取页面上下文，并用清单声明的证据替换附件列表。

        E22：上下文走 `load_capture_context`（不查 `qc_evidence_artifact`），
        当前采集与各旅程步骤的附件一律由清单加载。
        """
        context = await self._load_context(
            task_id=task_id,
            page_capture_id=page_capture_id,
        )
        if context is None:
            return None
        try:
            packet = await self._packet_loader.load_for_capture(
                task_id=task_id,
                capture_id=page_capture_id,
            )
        except ManifestBackedReadError as exc:
            # 记录裁决后原样抛出：拒判与告警由 E21 在上层接线
            self._remember(_failure_decision(
                task_id=task_id,
                capture_id=page_capture_id,
                error=exc,
                read_at=self._now(),
            ))
            raise
        self._remember(build_read_decision(packet, read_at=self._now()))
        return await self._project_source(
            task_id=task_id,
            context=context,
            packet=packet,
        )

    async def load_source_for_manifest(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        manifest_id: str,
    ) -> ObservationSource | None:
        """按补采绑定的精确 Manifest 读取，不允许回退到 Capture 最新版本。"""
        context = await self._load_context(
            task_id=task_id,
            page_capture_id=page_capture_id,
        )
        if context is None:
            return None
        try:
            packet = await self._packet_loader.load_for_manifest(
                task_id=task_id,
                manifest_id=manifest_id,
            )
        except ManifestBackedReadError as exc:
            self._remember(_failure_decision(
                task_id=task_id,
                capture_id=page_capture_id,
                error=exc,
                read_at=self._now(),
            ))
            raise
        if packet.capture_id != page_capture_id:
            raise ManifestUnavailableError(
                "指定 Manifest 不属于补采 Capture，拒绝读取",
                quality_status=None,
            )
        self._remember(build_read_decision(packet, read_at=self._now()))
        return await self._project_source(
            task_id=task_id,
            context=context,
            packet=packet,
        )

    async def _project_source(
        self,
        *,
        task_id: str,
        context: ObservationSource,
        packet: ManifestFactPacket,
    ) -> ObservationSource:
        return ObservationSource(
            task_id=context.task_id,
            page_capture_id=context.page_capture_id,
            agent_run_id=context.agent_run_id,
            page_url=context.page_url,
            page_title=context.page_title,
            page_type=context.page_type,
            page_state=context.page_state,
            action_log=context.action_log,
            artifacts=_observation_artifacts(packet),
            journey_steps=await self._load_journey_steps(
                task_id=task_id,
                steps=context.journey_steps,
            ),
            surface_kind=context.surface_kind,
            visual_observation=context.visual_observation,
            app_capture_facts=context.app_capture_facts,
            capture_role=context.capture_role,
            parent_capture_id=context.parent_capture_id,
            surface_phase_run_id=context.surface_phase_run_id,
            captured_at=context.captured_at,
            presentation=context.presentation,
            declared_flags=context.declared_flags,
            web_document_facts=context.web_document_facts,
            # H5 补采必须保留任务身份，才能重新绑定缺口到本次精确证据。
            target_platform=("h5" if context.target_platform == "h5" else None),
            manifest_quality=ManifestQualityProjection(
                manifest_id=packet.manifest_id,
                manifest_version=packet.manifest_version,
                quality_status=packet.quality_status.value,
            ),
        )

    async def _load_context(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None:
        loader = getattr(self._inner, "load_capture_context", None)
        if loader is not None:
            return await loader(task_id=task_id, page_capture_id=page_capture_id)
        return await self._inner.load_source(
            task_id=task_id,
            page_capture_id=page_capture_id,
        )

    async def _load_journey_steps(
        self,
        *,
        task_id: str,
        steps: tuple[ObservationJourneyStep, ...],
    ) -> tuple[ObservationJourneyStep, ...]:
        """旅程步骤的证据同样按各自清单加载；无清单或校验失败的步骤留空证据。

        旅程只作为判定的上下文佐证，单个历史步骤缺清单不应否决当前页判定——
        当前页的可信性由本次读取裁决单独把关。
        """
        resolved: list[ObservationJourneyStep] = []
        for step in steps:
            try:
                packet = await self._packet_loader.load_for_capture(
                    task_id=task_id,
                    capture_id=step.page_capture_id,
                )
            except ManifestBackedReadError:
                resolved.append(replace(step, artifacts=()))
                continue
            resolved.append(replace(step, artifacts=_observation_artifacts(packet)))
        return tuple(resolved)

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
        """原样委派：读取裁决不得写入 packet_payload（模型会看到它）。"""
        return await self._inner.save_packet(
            task_id=task_id,
            page_capture_id=page_capture_id,
            rule_id=rule_id,
            evidence_artifact_ids=evidence_artifact_ids,
            packet_payload=packet_payload,
            quality_gate_status=quality_gate_status,
            quality_gate_reason=quality_gate_reason,
            agent_run_id=agent_run_id,
        )

    def decision_for(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> ManifestReadDecision | None:
        """取最近一次读取裁决，供上层挂进审计载荷。"""
        return self._decisions.get((task_id, capture_id))

    def _remember(self, decision: ManifestReadDecision) -> None:
        key = (decision.task_id, decision.capture_id)
        if len(self._decisions) >= _MAX_TRACKED_DECISIONS and key not in self._decisions:
            self._decisions.pop(next(iter(self._decisions)))
        self._decisions[key] = decision


def _observation_artifacts(packet: ManifestFactPacket) -> tuple[ObservationArtifact, ...]:
    """把清单事实包投影成判定侧证据（只搬运，不改写）。"""
    return tuple(
        ObservationArtifact(
            artifact_id=item.artifact_id,
            artifact_type=item.artifact_type,
            content_type=item.content_type or "",
            payload=item.payload,
        )
        for item in packet.artifacts
    )


def build_budget_exceeded_decision(
    *,
    task_id: str,
    capture_id: str,
    read_at: datetime,
    reason: str,
    manifest_id: str | None = None,
    manifest_version: int | None = None,
) -> ManifestReadDecision:
    """E02 条款 1.5：载荷预算超限只进审计，独立于齐全性结论表达。"""
    return ManifestReadDecision(
        task_id=task_id,
        capture_id=capture_id,
        outcome=ManifestReadOutcome.PAYLOAD_BUDGET_EXCEEDED,
        read_at=read_at,
        manifest_id=manifest_id,
        manifest_version=manifest_version,
        reason=reason,
    )


def _failure_decision(
    *,
    task_id: str,
    capture_id: str,
    error: ManifestBackedReadError,
    read_at: datetime,
) -> ManifestReadDecision:
    """按 E02 条款 2.1 分型：清单裁决结论、在途、控制面未就绪、完整性故障各自独立。"""
    quality_status: EvidenceQualityStatus | None = None
    if isinstance(error, ManifestIntegrityError):
        outcome = ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED
    elif isinstance(error, ManifestUnavailableError):
        quality_status = error.quality_status
        if quality_status is EvidenceQualityStatus.FAIL:
            outcome = ManifestReadOutcome.EVIDENCE_INSUFFICIENT
        elif quality_status is EvidenceQualityStatus.PENDING:
            outcome = ManifestReadOutcome.EVIDENCE_PENDING
        else:
            outcome = ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE
    else:  # pragma: no cover - 基类不单独实例化
        outcome = ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED
    return ManifestReadDecision(
        task_id=task_id,
        capture_id=capture_id,
        outcome=outcome,
        read_at=read_at,
        quality_status=quality_status,
        reason=str(error),
    )
