"""E14：以需求集为唯一输入的证据齐全性裁决器（清单侧唯一裁决点）。

冻结依据：
- E02 条款 1.1：齐全性由且仅由清单写入侧裁决一次（E07 Writer 落地时调用本裁决器），
  门禁与判定只读结论、不得重算
- E02 条款 1.6：规则级 quality_gate 两键（min_screenshot_count/allow_empty_visible_text）
  作为需求声明在此评定
- E14 验收：门禁与清单裁决读取同一需求集——门禁经
  qc_evidence_exit_requirement_set 最新 revision 读取（_EVALUATE_GATE_SQL），
  本裁决器消费同一仓储 get_latest 返回的同型 EvidenceExitRequirementSet

裁决只覆盖非终态成员：已终态的需求（如经补证 satisfied）不因当前清单缺其证据而翻案。
输出只有 pass/fail 两态；pending 是写入侧的在途状态，不由裁决产生。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.api.app.domain.evidence.contracts import (
    ArtifactReference,
    EvidenceQualityStatus,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
)

_SCREENSHOT_ARTIFACT = "screenshot"
_VISIBLE_TEXT_ARTIFACT = "visible_text"

# 失败原因里最多逐条列出的缺口数，超出部分只报数量
_MAX_REASON_SHORTFALLS = 20


@dataclass(frozen=True, kw_only=True)
class RequirementShortfall:
    """单条需求的缺口明细，供阻断原因核对（E15 按证据不足落裁决时引用）。"""

    requirement_id: str
    rule_code: str
    missing_artifact_types: tuple[str, ...]
    missing_capture_roles: tuple[str, ...]
    screenshot_shortfall: bool
    visible_text_empty: bool

    def describe(self) -> str:
        parts: list[str] = []
        if self.missing_artifact_types:
            parts.append("缺少证据类型 " + "、".join(self.missing_artifact_types))
        if self.missing_capture_roles:
            parts.append("缺少采集角色 " + "、".join(self.missing_capture_roles))
        if self.screenshot_shortfall:
            parts.append("截图数量不足")
        if self.visible_text_empty:
            parts.append("可见文本为空")
        return f"{self.rule_code}：" + "，".join(parts)


@dataclass(frozen=True, kw_only=True)
class EvidenceCompletenessAdjudication:
    """一次性的齐全性结论，可直接写入清单 quality_status/quality_reason。"""

    quality_status: EvidenceQualityStatus
    quality_reason: str | None
    shortfalls: tuple[RequirementShortfall, ...]


class EvidenceCompletenessAdjudicator:
    """把需求集与附件引用收敛为一次性的齐全性结论。"""

    def adjudicate(
        self,
        *,
        requirement_set: EvidenceExitRequirementSet,
        artifacts: Sequence[ArtifactReference],
    ) -> EvidenceCompletenessAdjudication:
        if not isinstance(requirement_set, EvidenceExitRequirementSet):
            raise TypeError("requirement_set 必须为 EvidenceExitRequirementSet")
        references = tuple(artifacts)
        if any(not isinstance(item, ArtifactReference) for item in references):
            raise TypeError("artifacts 必须为 ArtifactReference 序列")
        artifact_types = {item.artifact_type for item in references}
        artifact_roles = {item.role for item in references}
        screenshot_count = sum(
            1 for item in references if item.artifact_type == _SCREENSHOT_ARTIFACT
        )
        has_non_empty_visible_text = any(
            item.artifact_type == _VISIBLE_TEXT_ARTIFACT and item.size_bytes > 0
            for item in references
        )
        shortfalls: list[RequirementShortfall] = []
        for requirement in requirement_set.requirements:
            if requirement.status.is_terminal:
                continue
            missing_types = tuple(sorted(set(requirement.required_artifact_types) - artifact_types))
            missing_roles = tuple(sorted(set(requirement.required_capture_roles) - artifact_roles))
            screenshot_shortfall = screenshot_count < requirement.min_screenshot_count
            visible_text_empty = (
                not requirement.allow_empty_visible_text
            ) and not has_non_empty_visible_text
            if missing_types or missing_roles or screenshot_shortfall or visible_text_empty:
                shortfalls.append(
                    RequirementShortfall(
                        requirement_id=requirement.requirement_id,
                        rule_code=requirement.rule_code,
                        missing_artifact_types=missing_types,
                        missing_capture_roles=missing_roles,
                        screenshot_shortfall=screenshot_shortfall,
                        visible_text_empty=visible_text_empty,
                    )
                )
        if not shortfalls:
            return EvidenceCompletenessAdjudication(
                quality_status=EvidenceQualityStatus.PASS,
                quality_reason=None,
                shortfalls=(),
            )
        described = [item.describe() for item in shortfalls[:_MAX_REASON_SHORTFALLS]]
        overflow = len(shortfalls) - _MAX_REASON_SHORTFALLS
        if overflow > 0:
            described.append(f"其余 {overflow} 条缺口略")
        return EvidenceCompletenessAdjudication(
            quality_status=EvidenceQualityStatus.FAIL,
            quality_reason="证据齐全性裁决未通过：" + "；".join(described),
            shortfalls=tuple(shortfalls),
        )
