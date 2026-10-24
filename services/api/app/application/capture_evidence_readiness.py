"""E16：采集证据就绪收口——发布需求集并推进清单裁决。

冻结依据：
- E02 条款 1.1：齐全性由清单写入侧裁决一次
- E14：门禁与清单裁决读取同一需求集
- E16 验收：Web 生产路径使用持久化门禁；证据不全时离场动作被真实拒绝

**为什么必须有本服务**：离场门禁对 Web 的放行条件是
`baseline_pass_manifest_count > 0` 且 `requirement_set_id` 非空
（`domain/exploration/evidence_exit_gate.py` 四项合取式）。
在此之前，需求集发布（E13）与清单裁决（E07/E14）都没有生产调用方——
清单永远停在 pending、需求集永远为空，一旦启用真门禁将拒绝**所有**离场动作。
本服务把这两个生产者接进采集流程，让门禁具备可放行的事实基础。

**触发时机**：页面识别完成、`page_type` 已知之后。
需求集由命中规则收敛而来，没有页面类型就取不到候选规则。

**失败结构化返回**：证据就绪收口失败不得抛出未处理异常；
调用方必须依据稳定错误码关闭当前页面的规则匹配，禁止把控制面失败伪装成识别成功。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from services.api.app.application.evidence_manifest_writer import (
    ManifestAdjudicationOutcome,
    ManifestAdjudicationStatus,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
)

logger = logging.getLogger(__name__)


class RequirementSetPublisher(Protocol):
    """E13：按 Capture 发布需求集。"""

    async def publish_for_capture(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        page_type: str,
        capture_role: str | None = None,
    ): ...


class ManifestAdjudicator(Protocol):
    """E07/E14：按需求集推进清单裁决。"""

    async def adjudicate_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        session_id: str | None = None,
        requirement_set: EvidenceExitRequirementSet | None = None,
    ) -> ManifestAdjudicationOutcome: ...


@dataclass(frozen=True, kw_only=True)
class CaptureEvidenceReadiness:
    """一次证据就绪收口的结果，供审计与测试核对。"""

    capture_id: str
    requirement_set_id: str | None = None
    requirement_set_revision: int | None = None
    adjudication_status: ManifestAdjudicationStatus | None = None
    manifest_quality_status: str | None = None
    failure_reason: str | None = None
    failure_code: str | None = None
    retryable: bool | None = None

    @property
    def gate_ready(self) -> bool:
        """门禁可放行的两项前置是否都已具备。"""
        return (
            self.requirement_set_id is not None
            and self.manifest_quality_status == "pass"
        )


class CaptureEvidenceReadinessService:
    """页面识别完成后，发布需求集并推进清单裁决。"""

    def __init__(
        self,
        *,
        requirement_publisher: RequirementSetPublisher,
        manifest_adjudicator: ManifestAdjudicator,
    ) -> None:
        self._requirements = requirement_publisher
        self._manifests = manifest_adjudicator

    async def finalize_for_capture(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        page_type: str,
        capture_role: str | None = None,
    ) -> CaptureEvidenceReadiness:
        """发布需求集 → 按同一需求集裁决清单；失败返回稳定控制面合同。"""
        try:
            published = await self._requirements.publish_for_capture(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
                page_type=page_type,
                capture_role=capture_role,
            )
        except Exception as exc:  # noqa: BLE001 - 证据就绪失败不得阻断探索
            logger.warning(
                "需求集发布失败 task_id=%s capture_id=%s：%s",
                task_id,
                capture_id,
                exc,
                exc_info=True,
            )
            return CaptureEvidenceReadiness(
                capture_id=capture_id,
                failure_reason="requirement_publish_failed",
                failure_code="EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
                retryable=False,
            )
        requirement_set = published.requirement_set
        try:
            outcome = await self._manifests.adjudicate_for_capture(
                task_id=task_id,
                capture_id=capture_id,
                session_id=session_id,
                requirement_set=requirement_set,
            )
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.warning(
                "清单裁决失败 task_id=%s capture_id=%s：%s",
                task_id,
                capture_id,
                exc,
                exc_info=True,
            )
            return CaptureEvidenceReadiness(
                capture_id=capture_id,
                requirement_set_id=requirement_set.requirement_set_id,
                requirement_set_revision=requirement_set.revision,
                failure_reason=f"manifest_adjudication_failed: {exc}"[:200],
            )
        return CaptureEvidenceReadiness(
            capture_id=capture_id,
            requirement_set_id=requirement_set.requirement_set_id,
            requirement_set_revision=requirement_set.revision,
            adjudication_status=outcome.status,
            manifest_quality_status=(
                outcome.manifest.quality_status.value
                if outcome.manifest is not None
                else None
            ),
        )
