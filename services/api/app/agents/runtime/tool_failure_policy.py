"""质检 Tool 异常分级：区分规则、页面与全局运行故障。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ToolFailureScope(StrEnum):
    """故障影响范围。"""

    RULE = "rule"
    PAGE = "page"
    RUN = "run"


@dataclass(frozen=True)
class ScopedToolFailure:
    """可由运行时安全处理的工具故障。"""

    scope: ToolFailureScope
    stage: str
    code: str
    message: str
    retryable: bool

    def to_result(self, *, page_capture_id: str | None) -> dict[str, Any]:
        """转换为模型和页面记忆共同消费的稳定结构。"""
        return {
            "status": "error",
            "execution_status": "error",
            "handled": True,
            "failure_scope": self.scope.value,
            "failure_kind": "infrastructure" if self.retryable else "domain",
            "failure_stage": self.stage,
            "error_code": self.code,
            "error_message": self.message,
            "retryable": self.retryable,
            "retry_exhausted": True,
            "page_capture_id": page_capture_id,
        }


_TOOL_FAILURE_META: dict[str, tuple[ToolFailureScope, str, str]] = {
    "page_capture_tool": (
        ToolFailureScope.PAGE,
        "page_capture",
        "PAGE_CAPTURE_FAILED",
    ),
    "page_detection_tool": (
        ToolFailureScope.PAGE,
        "page_detection",
        "PAGE_DETECTION_FAILED",
    ),
    "candidate_rules_tool": (
        ToolFailureScope.PAGE,
        "rule_matching",
        "CANDIDATE_RULE_QUERY_FAILED",
    ),
    "rule_judge_tool": (
        ToolFailureScope.RULE,
        "rule_judgement",
        "RULE_JUDGEMENT_FAILED",
    ),
    "rule_judge_batch_tool": (
        ToolFailureScope.RULE,
        "rule_judgement",
        "RULE_JUDGEMENT_BATCH_FAILED",
    ),
    "record_rule_execution_tool": (
        ToolFailureScope.RULE,
        "rule_persistence",
        "RULE_EXECUTION_WRITE_FAILED",
    ),
    "record_rule_executions_batch_tool": (
        ToolFailureScope.RULE,
        "rule_persistence",
        "RULE_EXECUTION_BATCH_WRITE_FAILED",
    ),
}


def classify_tool_exception(tool_name: str, exc: Exception) -> ScopedToolFailure | None:
    """仅分级已知质检工具；未知工具返回 None 并由上层继续抛出。"""
    meta = _TOOL_FAILURE_META.get(tool_name)
    if meta is None:
        return None
    scope, stage, code = meta
    custom_code = getattr(exc, "error_code", None)
    return ScopedToolFailure(
        scope=scope,
        stage=stage,
        code=str(custom_code).strip() if custom_code else code,
        message=str(exc) or exc.__class__.__name__,
        retryable=is_infrastructure_exception(exc),
    )


def is_infrastructure_exception(exc: Exception) -> bool:
    """识别连接、超时和常见数据库/缓存驱动异常。"""
    if getattr(exc, "error_code", None) == "PAGE_CAPTURE_SESSION_CRASHED":
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    module = exc.__class__.__module__.lower()
    return module.startswith(("asyncpg", "redis", "psycopg"))
