"""质检候选流水线影子抽样、差异摘要与准确率门禁。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Protocol


@dataclass(frozen=True)
class ShadowComparison:
    """仅包含结论、耗时、token 与证据引用的影子比较。"""

    task_id: str
    comparison_mode: str
    legacy_conclusion: str | None
    candidate_conclusion: str | None
    legacy_duration_ms: int | None = None
    candidate_duration_ms: int | None = None
    legacy_input_tokens: int | None = None
    candidate_input_tokens: int | None = None
    legacy_evidence_artifact_ids: tuple[str, ...] = ()
    candidate_evidence_artifact_ids: tuple[str, ...] = ()
    agent_run_id: str | None = None
    page_capture_id: str | None = None
    rule_id: str | None = None
    rule_code: str | None = None
    summary: dict[str, Any] | None = None


class PipelineShadowRepository(Protocol):
    async def save_comparison(
        self, *, comparison: ShadowComparison, difference_type: str
    ) -> str: ...

    async def list_gate_rows(self, *, limit: int) -> list[dict[str, Any]]: ...


class PipelineShadowService:
    """生成不含 DOM、截图或 Base64 的最小影子审计记录。"""

    def __init__(self, *, repository: PipelineShadowRepository) -> None:
        self._repository = repository

    async def record(self, comparison: ShadowComparison) -> str:
        safe_comparison = replace(
            comparison,
            summary=_sanitize_shadow_summary(comparison.summary or {}),
        )
        return await self._repository.save_comparison(
            comparison=safe_comparison,
            difference_type=_difference_type(safe_comparison),
        )

    async def evaluate_gate(self, *, limit: int = 1000) -> dict[str, Any]:
        """读取判定层回放摘要并执行最少 100 个样本的灰度门禁。"""
        rows = await self._repository.list_gate_rows(limit=max(limit, 100))
        return evaluate_shadow_gate(rows)


def should_sample_shadow(*, task_id: str, sample_rate: float) -> bool:
    """按任务 ID 稳定抽样，重启或重试不会改变样本归属。"""
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    bucket = int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16], 16)
    return bucket / 0xFFFFFFFFFFFFFFFF < sample_rate


def evaluate_shadow_gate(
    rows: list[dict[str, Any]], *, minimum_sample_count: int = 100
) -> dict[str, Any]:
    """按既定门禁计算是否允许继续扩大候选流程流量。"""
    total = len(rows)
    same = sum(
        1
        for row in rows
        if row.get("legacy_conclusion") == row.get("candidate_conclusion")
    )
    auto_pass_rows = [row for row in rows if row.get("legacy_conclusion") == "pass"]
    auto_pass_same = sum(
        1 for row in auto_pass_rows if row.get("candidate_conclusion") == "pass"
    )
    critical_high_false_pass = sum(
        1
        for row in rows
        if str(row.get("severity") or "").casefold() in {"critical", "high"}
        and row.get("legacy_conclusion") in {"fail", "warning", "manual_review"}
        and row.get("candidate_conclusion") == "pass"
    )
    legacy_insufficient = sum(
        1 for row in rows if row.get("legacy_conclusion") == "evidence_insufficient"
    )
    candidate_insufficient = sum(
        1 for row in rows if row.get("candidate_conclusion") == "evidence_insufficient"
    )
    overall_agreement = same / total if total else 0.0
    auto_pass_agreement = (
        auto_pass_same / len(auto_pass_rows) if auto_pass_rows else 1.0
    )
    insufficient_increase = (
        (candidate_insufficient - legacy_insufficient) / total if total else 0.0
    )
    passed = bool(
        total >= minimum_sample_count
        and critical_high_false_pass == 0
        and auto_pass_agreement >= 0.995
        and overall_agreement >= 0.98
        and insufficient_increase <= 0.01
    )
    return {
        "passed": passed,
        "sample_count": total,
        "minimum_sample_count": minimum_sample_count,
        "critical_high_false_pass": critical_high_false_pass,
        "auto_pass_agreement": auto_pass_agreement,
        "overall_agreement": overall_agreement,
        "evidence_insufficient_increase": insufficient_increase,
    }


def _difference_type(comparison: ShadowComparison) -> str:
    if (
        comparison.legacy_conclusion is None
        and comparison.candidate_conclusion is None
        and comparison.comparison_mode == "same_state_dual_capture"
    ):
        return "match"
    if comparison.legacy_conclusion is None:
        return "candidate_only"
    if comparison.candidate_conclusion is None:
        return "legacy_only"
    if comparison.legacy_conclusion != comparison.candidate_conclusion:
        return "conclusion_mismatch"
    if set(comparison.legacy_evidence_artifact_ids) != set(
        comparison.candidate_evidence_artifact_ids
    ):
        return "evidence_mismatch"
    return "match"


def _sanitize_shadow_summary(value: Any, *, depth: int = 0) -> Any:
    """影子摘要强制排除页面正文、图片和 Base64，避免误写敏感证据。"""
    if depth >= 4:
        return "[depth_truncated]"
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:50]:
            key = str(raw_key)[:100]
            normalized_key = key.casefold()
            if any(
                marker in normalized_key
                for marker in (
                    "dom",
                    "html",
                    "screenshot",
                    "base64",
                    "payload",
                    "page_content",
                )
            ):
                continue
            safe[key] = _sanitize_shadow_summary(item, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_shadow_summary(item, depth=depth + 1)
            for item in list(value)[:50]
        ]
    if isinstance(value, str):
        if value.strip().casefold().startswith("data:"):
            return "[data_url_redacted]"
        return value[:512]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:512]
