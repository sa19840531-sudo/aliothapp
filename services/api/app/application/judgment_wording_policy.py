"""判定文案硬约束：系统层面禁止对被检产品输出黑灰产、违法类业务定性。

提示词层（rule_judge/instructions.py 输出纪律章节）已禁止模型输出此类定性词；
本模块是 Result Writer 侧的确定性兜底，保证即使模型违反指令，落库与报告
可见文案也不会出现这类言论。只中性化措辞，不改变判定结论、置信度与证据。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.api.app.application.semantic_rule_judge import (
        SemanticJudgeModelOutput,
    )

# 顺序敏感：长词在前，避免"黑灰产"被"灰产"先替换截断出残词。
FORBIDDEN_BUSINESS_CHARACTERIZATIONS: tuple[tuple[str, str], ...] = (
    ("黑灰产业链", "违规内容"),
    ("黑灰产", "违规内容"),
    ("灰产", "违规内容"),
    ("黑产", "违规内容"),
    ("违法犯罪", "违规"),
    ("违法", "违规"),
    ("非法", "违规"),
    ("犯罪", "违规"),
    ("涉黄", "违规内容"),
    ("涉赌", "违规内容"),
    ("涉毒", "违规内容"),
    ("诈骗", "风险内容"),
    ("传销", "风险内容"),
    ("洗钱", "风险内容"),
)


def contains_business_characterization(text: str | None) -> bool:
    """判断文本是否包含禁止的业务定性词。"""
    if not text:
        return False
    return any(forbidden in text for forbidden, _ in FORBIDDEN_BUSINESS_CHARACTERIZATIONS)


def neutralize_business_characterizations(text: str | None) -> str | None:
    """把业务定性词替换为规则口径内的中性表述；None 与干净文本原样返回。"""
    if not text or not contains_business_characterization(text):
        return text
    result = text
    for forbidden, replacement in FORBIDDEN_BUSINESS_CHARACTERIZATIONS:
        if forbidden in result:
            result = result.replace(forbidden, replacement)
    return result


# 运营可见文案必须是中文大白话：提示词层（rule_judge/instructions.py 输出纪律章节）
# 已要求中文并禁止内部工具名；下面两个函数是写回侧的确定性兜底。
_INTERNAL_TOOL_NAME_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*_tool\b")
_SCRUBBED_TOOL_SEQUENCE_PATTERN = re.compile(r"检测工具(?:\s*[、，,/与和]\s*检测工具)+")
_CJK_CHARACTER_PATTERN = re.compile(r"[一-鿿]")


def has_cjk_text(text: str | None) -> bool:
    """判断文本是否含有中文汉字。"""
    return bool(text) and _CJK_CHARACTER_PATTERN.search(text) is not None


def enforce_plain_display_text(text: str | None, *, fallback: str) -> str | None:
    """运营可见文案兜底：内部工具名收敛为「检测工具」，整段无中文时退回标准文案。"""
    if not text:
        return text
    result = _INTERNAL_TOOL_NAME_PATTERN.sub("检测工具", text)
    result = _SCRUBBED_TOOL_SEQUENCE_PATTERN.sub("检测工具", result)
    if not has_cjk_text(result):
        return fallback
    return result


def sanitize_semantic_judge_output(
    model_output: SemanticJudgeModelOutput,
) -> SemanticJudgeModelOutput:
    """返回文案已中性化的副本；结论、置信度与证据引用一律不动。

    干净输出直接原样返回，避免无谓的模型对象重建。
    """
    from services.api.app.application.semantic_rule_judge import SemanticJudgeFinding

    dirty = (
        contains_business_characterization(model_output.reasoning_summary)
        or contains_business_characterization(model_output.review_reason)
        or any(
            contains_business_characterization(finding.summary)
            for finding in model_output.findings
        )
        or any(
            contains_business_characterization(item)
            for item in model_output.missing_evidence
        )
        or any(contains_business_characterization(item) for item in model_output.conflicts)
    )
    if not dirty:
        return model_output
    return model_output.model_copy(
        update={
            "reasoning_summary": neutralize_business_characterizations(
                model_output.reasoning_summary
            ),
            "review_reason": neutralize_business_characterizations(
                model_output.review_reason
            ),
            "findings": [
                SemanticJudgeFinding(
                    summary=neutralize_business_characterizations(finding.summary) or "",
                    evidence_refs=list(finding.evidence_refs),
                    element_refs=list(finding.element_refs),
                )
                for finding in model_output.findings
            ],
            "missing_evidence": [
                neutralize_business_characterizations(item) or ""
                for item in model_output.missing_evidence
            ],
            "conflicts": [
                neutralize_business_characterizations(item) or ""
                for item in model_output.conflicts
            ],
        }
    )
