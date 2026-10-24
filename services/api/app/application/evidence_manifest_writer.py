"""E07：Web Capture 清单写入服务（EvidenceManifestWriter）。

冻结依据：
- E02 条款 1.1：证据齐全性由且仅由清单写入侧裁决一次；本服务调用 E14 的
  EvidenceCompletenessAdjudicator 产出结论，门禁与判定只读、不得重算
- E07 验收：采集提交后生成清单；清单含 manifest_hash、manifest_version 与
  quality_status，并支持 pending 到 pass/fail 的裁决
- E14 验收：门禁与清单裁决读取同一需求集（EvidenceExitRequirementRepository
  的最新 revision）

时序分两段：
1. Capture 提交（E08 钩子，PageCapturePersistenceService.persist）→
   build_pending_page_capture_manifest 生成 v1 pending 清单，随附件在同一
   数据库事务内落库（E09，save_capture 的 manifest 参数）；
2. 需求集就绪后（E13 publish_for_capture 已写库）→ adjudicate_for_capture
   追加 pass/fail 终态版本；需求集缺席时保持 pending，不伪造结论。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.application.evidence_completeness import (
    EvidenceCompletenessAdjudicator,
    RequirementShortfall,
)
from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    normalize_manifest_capture_role,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_evidence_exit_requirement_set,
)

# 采集未绑定 Surface Session 时清单 session 身份的哨兵前缀，
# 与 Web 链路既有的 "agent-entry:" 哨兵约定同族。
UNBOUND_SESSION_PREFIX = "agent-run:"

# 提交时 pending 清单的固定原因，供审计区分「已提交待裁决」与人工写入。
PENDING_QUALITY_REASON = "page_capture_committed"

# 裁决通过时的固定原因；失败原因由 E14 裁决器生成（含缺口明细）。
PASS_QUALITY_REASON = "evidence_requirements_satisfied"


class _PersistedArtifactLike(Protocol):
    artifact_id: str
    artifact_type: str
    storage_path: str
    checksum: str
    size_bytes: int
    content_type: str


class EvidenceExitRequirementReader(Protocol):
    """按 Capture 读取最新需求集 revision 的端口（E14 同源读取）。"""

    async def get_latest(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
    ) -> EvidenceExitRequirementSet | None: ...


class ManifestAdjudicationStatus(StrEnum):
    """一次裁决请求的处置结果。"""

    ADJUDICATED = "adjudicated"
    ALREADY_TERMINAL = "already_terminal"
    AWAITING_REQUIREMENT_SET = "awaiting_requirement_set"
    NO_MANIFEST = "no_manifest"


@dataclass(frozen=True, kw_only=True)
class ManifestAdjudicationOutcome:
    """pending → pass/fail 裁决的返回值；shortfalls 供 E15 阻断原因核对。"""

    status: ManifestAdjudicationStatus
    manifest: EvidenceManifestReference | None = None
    created: bool = False
    shortfalls: tuple[RequirementShortfall, ...] = ()


def manifest_session_id(
    surface_session_id: str | None,
    *,
    agent_run_id: str,
) -> str:
    """清单 session 身份：有 Surface Session 用真值，无绑定用运行哨兵。"""
    if isinstance(surface_session_id, str) and surface_session_id.strip():
        return surface_session_id.strip()
    return f"{UNBOUND_SESSION_PREFIX}{agent_run_id}"


def build_pending_page_capture_manifest(
    *,
    task_id: str,
    session_id: str,
    capture_id: str,
    capture_role: str,
    artifacts: Sequence[_PersistedArtifactLike],
    created_at: datetime | None = None,
    artifact_metadata: Mapping[str, Any] | None = None,
) -> EvidenceManifestReference:
    """从已落库附件身份生成 v1 pending 清单（提交时随附件同事务写入）。

    E01 条款 3.1：清单写入服务起，`ArtifactReference.role` 强制校验属于取值表；
    条款 3.4：附件不自带角色，恒等于所属 capture 的供给角色。
    `artifact_metadata` 是采集级来源事实（如展示层评估），随每份附件进入清单哈希，
    让规则结论能从清单逆向追到"当时最上层是谁、材料是否经过诊断操作"。
    """
    role = normalize_manifest_capture_role(capture_role)
    shared_metadata = dict(artifact_metadata or {})
    references = tuple(
        ArtifactReference(
            artifact_id=item.artifact_id,
            artifact_type=item.artifact_type,
            role=role,
            storage_path=item.storage_path,
            checksum=item.checksum,
            size_bytes=item.size_bytes,
            content_type=item.content_type,
            metadata=dict(shared_metadata),
        )
        for item in artifacts
    )
    return build_evidence_manifest_reference(
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        manifest_version=1,
        artifacts=references,
        quality_status=EvidenceQualityStatus.PENDING,
        quality_reason=PENDING_QUALITY_REASON,
        created_at=created_at or datetime.now(UTC),
    )


class EvidenceManifestWriter:
    """把最新 pending 清单与需求集收敛为 pass/fail 终态版本。"""

    def __init__(
        self,
        *,
        manifest_repository: EvidenceManifestRepository,
        requirement_repository: EvidenceExitRequirementReader | None = None,
        adjudicator: EvidenceCompletenessAdjudicator | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._manifests = manifest_repository
        self._requirements = requirement_repository
        self._adjudicator = adjudicator or EvidenceCompletenessAdjudicator()
        self._now = now or (lambda: datetime.now(UTC))

    async def adjudicate_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        session_id: str | None = None,
        requirement_set: EvidenceExitRequirementSet | None = None,
    ) -> ManifestAdjudicationOutcome:
        """按最新清单版本执行一次性齐全性裁决，追加终态版本。

        - 无清单：不写入，返回 no_manifest；
        - 最新版本已终态：幂等返回，不追加版本；
        - 需求集缺席（未传且仓储无最新 revision）：保持 pending，不伪造结论。
        """
        # 新 Web 阶段合同只由显式严格 Writer 消费，禁止旧 PASS 幂等分支认证新成员。
        if getattr(requirement_set, "protocol_version", None) == "web-payload-requirement-set.v1":
            raise ValueError("WEB_PAYLOAD_REQUIRES_STRICT_WRITER")
        latest = await self._manifests.get_latest_for_capture(
            task_id=task_id,
            capture_id=capture_id,
        )
        if latest is None:
            return ManifestAdjudicationOutcome(
                status=ManifestAdjudicationStatus.NO_MANIFEST,
            )
        if latest.quality_status is not EvidenceQualityStatus.PENDING:
            if latest.quality_status is EvidenceQualityStatus.PASS:
                # 幂等修复：清单已 pass 但需求可能还停在 pending（如上一次
                # 在两步写入之间中断），离场门禁只认需求终态。
                await self._satisfy_requirements(
                    manifest=latest,
                    session_id=session_id,
                )
            return ManifestAdjudicationOutcome(
                status=ManifestAdjudicationStatus.ALREADY_TERMINAL,
                manifest=latest,
            )
        resolved_set = requirement_set
        if resolved_set is None and self._requirements is not None:
            resolved_set = await self._requirements.get_latest(
                task_id=task_id,
                session_id=session_id or latest.session_id,
                capture_id=capture_id,
            )
        if resolved_set is None:
            return ManifestAdjudicationOutcome(
                status=ManifestAdjudicationStatus.AWAITING_REQUIREMENT_SET,
                manifest=latest,
            )
        if getattr(resolved_set, "protocol_version", None) == "web-payload-requirement-set.v1":
            raise ValueError("WEB_PAYLOAD_REQUIRES_STRICT_WRITER")
        if resolved_set.task_id != latest.task_id or resolved_set.capture_id != latest.capture_id:
            raise ValueError("需求集与清单的 Capture 归属不一致，禁止裁决")
        adjudication = self._adjudicator.adjudicate(
            requirement_set=resolved_set,
            artifacts=latest.artifacts,
        )
        adjudicated = build_evidence_manifest_reference(
            task_id=latest.task_id,
            session_id=latest.session_id,
            capture_id=latest.capture_id,
            manifest_version=latest.manifest_version + 1,
            artifacts=latest.artifacts,
            observation_packet_ids=latest.observation_packet_ids,
            quality_status=adjudication.quality_status,
            quality_reason=adjudication.quality_reason or PASS_QUALITY_REASON,
            created_at=self._now(),
        )
        persisted = await self._manifests.add(manifest=adjudicated)
        if adjudication.quality_status is EvidenceQualityStatus.PASS:
            # E14 裁决即需求终态的唯一裁定点：pass 意味着本清单满足了全部
            # 非终态需求，必须同步把它们推到 satisfied——否则离场门禁按
            # 需求非终态永远阻断，任何有规则命中的页面都离不了场。
            await self._satisfy_requirements(
                manifest=persisted.manifest,
                session_id=session_id,
                requirement_set=resolved_set,
            )
        return ManifestAdjudicationOutcome(
            status=ManifestAdjudicationStatus.ADJUDICATED,
            manifest=persisted.manifest,
            created=persisted.created,
            shortfalls=adjudication.shortfalls,
        )

    async def _satisfy_requirements(
        self,
        *,
        manifest: EvidenceManifestReference,
        session_id: str | None,
        requirement_set: EvidenceExitRequirementSet | None = None,
    ) -> None:
        """把当前 pass 清单覆盖的 pending 需求追加为 satisfied 版本。

        失败不回滚清单终态：下一次对同 Capture 的裁决会走 ALREADY_TERMINAL
        幂等修复，最终一致由该路径兜底。
        """
        if self._requirements is None:
            return
        appender = getattr(self._requirements, "add", None)
        if not callable(appender):
            return
        resolved = requirement_set
        if resolved is None:
            resolved = await self._requirements.get_latest(
                task_id=manifest.task_id,
                session_id=session_id or manifest.session_id,
                capture_id=manifest.capture_id,
            )
        if getattr(resolved, "protocol_version", None) == "web-payload-requirement-set.v1":
            raise ValueError("WEB_PAYLOAD_REQUIRES_STRICT_WRITER")
        if resolved is None or resolved.nonterminal_count == 0:
            return
        members = tuple(
            replace(
                member,
                status=EvidenceExitRequirementStatus.SATISFIED,
                resolution_manifest_id=manifest.manifest_id,
            )
            if member.status is EvidenceExitRequirementStatus.PENDING
            else member
            for member in resolved.requirements
        )
        await appender(
            requirement_set=build_evidence_exit_requirement_set(
                revision=resolved.revision + 1,
                task_id=resolved.task_id,
                session_id=resolved.session_id,
                capture_id=resolved.capture_id,
                matcher_snapshot_hash=resolved.matcher_snapshot_hash,
                requirements=members,
                created_at=self._now(),
            )
        )
