"""Agent Run 编排层的输出模型：终止原因分类与运行结果封装。"""

from dataclasses import dataclass
from enum import StrEnum

from services.api.app.agents.runtime.action_trace import ActionTraceSink
from services.api.app.agents.runtime.working_memory import WorkingMemory


class TerminationReason(StrEnum):
    """Agent Run 的终止原因分类，供上层任务编排/审计使用。"""

    COMPLETED = "completed"
    """模型正常返回，且结构化工作记忆确认页面质检已经完整收口。"""

    INCOMPLETE = "incomplete"
    """模型正常返回，但工作记忆中仍存在未完成的页面质检。"""

    MAX_STEPS_REACHED = "max_steps_reached"
    """命中硬性步数上限，被 LoopGuardMiddleware 强制终止。"""

    STUCK_DETECTED = "stuck_detected"
    """检测到连续重复调用、判定无进展，被 LoopGuardMiddleware 强制终止。"""

    TIMEOUT = "timeout"
    """命中墙钟超时，被编排层 asyncio.wait_for 强制中断。"""

    CANCELED = "canceled"
    """收到外部取消请求，被 CancellationCheckMiddleware 强制终止（详见 cancellation.py）。"""

    ERROR = "error"
    """执行过程中出现未预期异常。"""

    INFRASTRUCTURE_UNAVAILABLE = "infrastructure_unavailable"
    """连续页面或规则基础设施故障达到熔断阈值，无法继续可靠执行。"""


REVIEW_TERMINATION_REASONS = frozenset(
    {
        TerminationReason.INCOMPLETE,
        TerminationReason.MAX_STEPS_REACHED,
        TerminationReason.STUCK_DETECTED,
        TerminationReason.TIMEOUT,
    }
)
"""应将业务任务转入人工审核的非异常收敛终止原因。"""


def is_review_termination_reason(reason: TerminationReason | str | None) -> bool:
    """判断终止原因是否表示任务未完成但不属于基础设施或模型异常。"""
    return reason in REVIEW_TERMINATION_REASONS


class ErrorCategory(StrEnum):
    """``RunResult.error_message`` 的粗粒度分类，供上层监控/告警区分处理优先级。"""

    LLM_PROVIDER = "llm_provider"
    """大模型供应商 API 调用异常（网络、鉴权、限流、超时等），通常与外部依赖相关。"""

    UNKNOWN = "unknown"
    """其余未预期异常（框架内部错误、编排代码自身缺陷等），需要人工排查。"""

    INFRASTRUCTURE = "infrastructure"
    """数据库、缓存或证据存储等运行基础设施持续不可用。"""


class ExecutionOutcome(StrEnum):
    """页面探索收敛后的业务执行结果。"""

    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"


@dataclass
class UsageSnapshot:
    """一次 Run 的 token 用量快照，来自 Agent Framework 累计返回的 usage_details。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    billable_input_tokens: int | None = None
    total_tokens: int | None = None
    ai_calls: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    cache_hit_cost: float | None = None
    total_cost: float | None = None
    cost_currency: str | None = None
    pricing_source: str | None = None
    pricing_configured: bool | None = None
    applied_pricing_tiers: tuple[str, ...] = ()
    pricing_breakdown: tuple[dict[str, object], ...] = ()
    provider: str | None = None
    model: str | None = None


@dataclass
class RunResult:
    """一次 Main Agent Run 的编排层结果，供 API 层/上层任务调度消费。"""

    termination_reason: TerminationReason
    working_memory: WorkingMemory
    trace_sink: ActionTraceSink
    final_text: str | None = None
    error_message: str | None = None
    error_category: ErrorCategory | None = None
    usage: UsageSnapshot | None = None
    execution_outcome: ExecutionOutcome = ExecutionOutcome.SUCCESS
    evidence_index_id: str | None = None
    exploration_status: str | None = None
    evidence_gap: tuple[str, ...] = ()
    page_timeline: tuple[str, ...] = ()
    web_exploration_summary: dict[str, object] | None = None
