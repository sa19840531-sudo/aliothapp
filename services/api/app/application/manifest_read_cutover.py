"""E24：清单读取主路径切换、零差异前置与回滚开关。

冻结依据：
- E24 验收：影子对账确认零差异后才切主路径；开关可退回旧读取路径
- E02 条款 1.4：判定侧退出附件直查，改为按清单读取

三档模式：

| 模式 | 判定读取来源 | 清单读取 |
|---|---|---|
| `off` | 旧读取（附件直查） | 不发生 |
| `shadow` | 旧读取（权威） | 并行执行，只对账（E23） |
| `on` | **清单读取（权威）** | 主路径 |

**切主前置是强制的**：请求 `on` 但缺少零差异对账凭证时，
决议器把有效模式**降级为 `shadow`** 并给出降级原因，
绝不在未验证的读取路径上直接跑生产判定——这比"配置写了就切"安全，
也正是验收要求的"确认零差异后才切主"。

**回滚**只需把模式改回 `shadow`／`off`：读取来源立即退回旧路径，
无需回滚数据，因为影子期与切主期都不改写任何原始证据（E02 条款 1.3）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.application.manifest_read_shadow import (
    ShadowComparisonOutcome,
    ShadowReadComparison,
)

logger = logging.getLogger(__name__)

CUTOVER_DECISION_SCHEMA_VERSION = "manifest-read-cutover.v1"


class ManifestReadMode(StrEnum):
    """判定侧证据读取模式。"""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


class CutoverBlockReason(StrEnum):
    """请求切主被降级的原因。"""

    NO_SHADOW_EVIDENCE = "no_shadow_evidence"
    MISMATCHES_PRESENT = "mismatches_present"
    MANIFEST_READ_FAILURES = "manifest_read_failures"
    INSUFFICIENT_SAMPLES = "insufficient_samples"


@dataclass(frozen=True, kw_only=True)
class ShadowReconciliationSummary:
    """影子期对账汇总，作为切主的唯一凭证。"""

    compared_captures: int = 0
    matched: int = 0
    mismatched: int = 0
    manifest_read_failed: int = 0
    skipped: int = 0
    first_compared_at: datetime | None = None
    last_compared_at: datetime | None = None

    @property
    def zero_difference(self) -> bool:
        """零差异：跑过对账，且既无差异也无清单读取失败。"""
        return (
            self.compared_captures > 0
            and self.mismatched == 0
            and self.manifest_read_failed == 0
        )

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "compared_captures": self.compared_captures,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "manifest_read_failed": self.manifest_read_failed,
            "skipped": self.skipped,
            "zero_difference": self.zero_difference,
            "first_compared_at": (
                self.first_compared_at.isoformat()
                if self.first_compared_at is not None
                else None
            ),
            "last_compared_at": (
                self.last_compared_at.isoformat()
                if self.last_compared_at is not None
                else None
            ),
        }


def summarize_comparisons(
    comparisons: Sequence[ShadowReadComparison],
) -> ShadowReconciliationSummary:
    """把影子对账明细汇总成切主凭证。

    `skipped`（按时间分流走旧读取）不计入 `compared_captures`——
    没真正比过的采集不能充当零差异证据。
    """
    matched = 0
    mismatched = 0
    failed = 0
    skipped = 0
    compared_at: list[datetime] = []
    for item in comparisons:
        if item.outcome is ShadowComparisonOutcome.SKIPPED:
            skipped += 1
            continue
        compared_at.append(item.compared_at)
        if item.outcome is ShadowComparisonOutcome.MATCHED:
            matched += 1
        elif item.outcome is ShadowComparisonOutcome.MANIFEST_READ_FAILED:
            failed += 1
        else:
            mismatched += 1
    return ShadowReconciliationSummary(
        compared_captures=len(compared_at),
        matched=matched,
        mismatched=mismatched,
        manifest_read_failed=failed,
        skipped=skipped,
        first_compared_at=min(compared_at) if compared_at else None,
        last_compared_at=max(compared_at) if compared_at else None,
    )


@dataclass(frozen=True, kw_only=True)
class ManifestReadModeDecision:
    """一次模式决议结果；降级时保留请求值与原因供审计。"""

    requested_mode: ManifestReadMode
    effective_mode: ManifestReadMode
    summary: ShadowReconciliationSummary | None = None
    block_reason: CutoverBlockReason | None = None

    @property
    def downgraded(self) -> bool:
        return self.effective_mode is not self.requested_mode

    def to_audit_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": CUTOVER_DECISION_SCHEMA_VERSION,
            "requested_mode": self.requested_mode.value,
            "effective_mode": self.effective_mode.value,
            "downgraded": self.downgraded,
        }
        if self.block_reason is not None:
            payload["block_reason"] = self.block_reason.value
        if self.summary is not None:
            payload["shadow_reconciliation"] = self.summary.to_audit_payload()
        return payload


class ManifestReadModeResolver:
    """按影子对账凭证决议有效读取模式。"""

    def __init__(self, *, minimum_compared_captures: int = 1) -> None:
        if (
            isinstance(minimum_compared_captures, bool)
            or not isinstance(minimum_compared_captures, int)
            or minimum_compared_captures <= 0
        ):
            raise ValueError("minimum_compared_captures 必须为正整数")
        self._minimum_compared_captures = minimum_compared_captures

    def resolve(
        self,
        *,
        requested_mode: ManifestReadMode,
        summary: ShadowReconciliationSummary | None = None,
    ) -> ManifestReadModeDecision:
        """off/shadow 直接生效；on 必须有零差异凭证，否则降级为 shadow。"""
        if requested_mode is not ManifestReadMode.ON:
            return ManifestReadModeDecision(
                requested_mode=requested_mode,
                effective_mode=requested_mode,
                summary=summary,
            )
        block_reason = self._block_reason(summary)
        if block_reason is None:
            return ManifestReadModeDecision(
                requested_mode=requested_mode,
                effective_mode=ManifestReadMode.ON,
                summary=summary,
            )
        logger.warning(
            "manifest_read_cutover_downgraded requested=on effective=shadow reason=%s",
            block_reason.value,
        )
        return ManifestReadModeDecision(
            requested_mode=requested_mode,
            effective_mode=ManifestReadMode.SHADOW,
            summary=summary,
            block_reason=block_reason,
        )

    def _block_reason(
        self,
        summary: ShadowReconciliationSummary | None,
    ) -> CutoverBlockReason | None:
        if summary is None or summary.compared_captures == 0:
            return CutoverBlockReason.NO_SHADOW_EVIDENCE
        if summary.mismatched > 0:
            return CutoverBlockReason.MISMATCHES_PRESENT
        if summary.manifest_read_failed > 0:
            return CutoverBlockReason.MANIFEST_READ_FAILURES
        if summary.compared_captures < self._minimum_compared_captures:
            return CutoverBlockReason.INSUFFICIENT_SAMPLES
        return None


def build_observation_source_reader(
    *,
    decision: ManifestReadModeDecision,
    legacy: Any,
    manifest_backed: Any,
    shadow_factory: Any,
) -> Any:
    """按有效模式装配判定侧证据读取来源。

    - `off`：旧读取；
    - `shadow`：`shadow_factory(legacy, manifest_backed)` 返回的双读对账件
      （旧读取仍权威）；
    - `on`：清单读取作为主路径。

    回滚＝把模式改回 `shadow`／`off` 后重新装配，读取来源立即退回旧路径；
    因为全程不改写原始证据，回滚无需任何数据修复。
    """
    if decision.effective_mode is ManifestReadMode.ON:
        return manifest_backed
    if decision.effective_mode is ManifestReadMode.SHADOW:
        return shadow_factory(legacy, manifest_backed)
    return legacy
