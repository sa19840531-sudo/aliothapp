"""动作留痕：记录 Agent 循环内每一次工具调用尝试，支撑审计回放。

本模块通过可替换的 ``ActionTraceSink`` 输出动作轨迹；Worker 使用实时 sink
写入 ``qc_agent_action_trace``，内存态与日志态 sink 用于测试和本地调试。
"""

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.middleware_context import RETRY_COUNT_METADATA_KEY
from services.api.app.agents.runtime.working_memory import WorkingMemory, normalize_arguments

logger = logging.getLogger(__name__)


def normalize_result(result: Any, *, limit: int | None = None) -> str | None:
    """将工具返回值归一化为可留痕的 JSON 字符串，超长截断。

    返回 None 表示本次调用没有可记录的结果（未执行到工具或工具无返回）。
    """
    if result is None:
        return None
    value = result
    # agent_framework 会把工具返回包装成单元素 Content 列表；逐层拆开取原始文本，
    # 避免落库后变成 {"value": ["<转义 JSON 字符串>"]} 的嵌套转义。
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        value = text_attr
    if isinstance(value, str):
        serialized = value
    else:
        try:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = repr(value)
    # 框架序列化工具结果时常用 ensure_ascii=True，中文变成 \uXXXX 转义（可读性差且
    # 每个中文占 6 字符预算）；能解析的 JSON 先重编码为真实字符再截断。
    try:
        serialized = json.dumps(json.loads(serialized), ensure_ascii=False, default=str)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    max_chars = limit if limit is not None else RESULT_TRACE_CHAR_LIMIT
    if len(serialized) > max_chars:
        return serialized[:max_chars] + f"…(截断，共 {len(serialized)} 字符)"
    return serialized


RESULT_TRACE_CHAR_LIMIT = 4000
"""留痕中工具返回值的截断上限：足够回放判定依据，又不至于撑爆 Redis 记录与审计表。"""


def _normalize_trace_arguments(tool_name: str, arguments: Any) -> str:
    """归一化审计参数，并移除受控探针和故障正则原文。"""
    normalized = normalize_arguments(arguments)
    if tool_name != "page_capture_tool":
        return normalized
    try:
        payload = json.loads(normalized)
    except (json.JSONDecodeError, TypeError):
        return normalized
    if not isinstance(payload, dict):
        return normalized
    for field in ("probe_text", "fault_url_pattern"):
        value = payload.get(field)
        if not isinstance(value, str):
            continue
        payload[field] = {
            "redacted": True,
            "length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


@dataclass
class ActionTraceEntry:
    """单次工具调用尝试的留痕记录。"""

    step: int
    tool_name: str
    arguments: str
    status: str
    """ok（正常完成） | error（工具异常） | terminated（循环护栏） | denied（业务门禁）。"""
    duration_seconds: float
    error_message: str | None = None
    retry_count: int = 0
    """本次调用尝试实际发生的重试次数（由 RetryMiddleware 执行，0 表示首次即成功或
    根本未到达 RetryMiddleware，如被更外层的护栏/取消检查拦截）。"""
    result: str | None = None
    """工具返回值的归一化 JSON 字符串（超过 RESULT_TRACE_CHAR_LIMIT 截断），
    调用未产生结果（异常/被拦截）时为 None。"""
    source_execution_ids: tuple[str, ...] = ()
    """本次规则写回 Tool 成功生成的执行记录 ID，用于关联动作轨迹。"""


class ActionTraceSink(Protocol):
    """动作留痕的写入目的地契约，可使用内存态、日志态或数据库实现。"""

    def record(self, entry: ActionTraceEntry) -> None: ...


class InMemoryActionTraceSink:
    """默认的内存态留痕实现：仅在当次 Run 生命周期内保留，进程重启即丢失。"""

    def __init__(self) -> None:
        self.entries: list[ActionTraceEntry] = []

    def record(self, entry: ActionTraceEntry) -> None:
        self.entries.append(entry)


class LoggingActionTraceSink:
    """将留痕记录写入标准日志的 sink，便于本地调试观察循环过程；可包装另一个 sink 一并写入。"""

    def __init__(self, *, delegate: ActionTraceSink | None = None) -> None:
        self._delegate = delegate

    def record(self, entry: ActionTraceEntry) -> None:
        logger.info(
            "agent action trace: step=%s tool=%s status=%s duration=%.3fs error=%s",
            entry.step,
            entry.tool_name,
            entry.status,
            entry.duration_seconds,
            entry.error_message,
        )
        if self._delegate is not None:
            self._delegate.record(entry)


class ActionTraceMiddleware(FunctionMiddleware):
    """无条件记录每一次工具调用尝试，包括被 ``LoopGuardMiddleware`` 拦截的尝试。

    必须作为"最外层"FunctionMiddleware 使用，即在 ``agent.run(middleware=[...])``
    列表中排在 ``LoopGuardMiddleware`` 之前：只有这样，当内层护栏直接
    ``raise MiddlewareTermination`` 而不调用真正的工具时，本中间件的 ``finally``
    分支才能补记一条 ``status="terminated"`` 的留痕，否则该次被拦截的尝试会完全
    不出现在审计链路里。已通过实测确认，详见仓库记忆。
    """

    def __init__(self, working_memory: WorkingMemory, sink: ActionTraceSink) -> None:
        self._working_memory = working_memory
        self._sink = sink

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        step_index = self._working_memory.allocate_action_sequence()
        started_at = time.monotonic()
        status = "ok"
        error_message: str | None = None
        try:
            await call_next()
            if failure_message := _handled_error_message(context.result):
                status = "error"
                error_message = failure_message
            elif _is_blocked_result(context.result):
                status = "denied"
        except MiddlewareTermination:
            status = "terminated"
            raise
        except Exception as exc:  # noqa: BLE001 - 工具异常需要完整记录留痕，随后必须继续向上传播
            status = "error"
            error_message = str(exc)
            raise
        finally:
            duration = time.monotonic() - started_at
            metadata = getattr(context, "metadata", {})
            self._sink.record(
                ActionTraceEntry(
                    step=step_index,
                    tool_name=context.function.name,
                    arguments=_normalize_trace_arguments(
                        context.function.name,
                        context.arguments,
                    ),
                    status=status,
                    duration_seconds=duration,
                    error_message=error_message,
                    retry_count=int(metadata.get(RETRY_COUNT_METADATA_KEY, 0)),
                    result=normalize_result(context.result),
                    source_execution_ids=_extract_rule_execution_ids(
                        context.function.name,
                        context.result,
                    ),
                )
            )


def _is_blocked_result(result: Any) -> bool:
    """识别业务中间件返回的结构化阻断结果。"""
    value = result
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        value = text_attr
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    return isinstance(value, Mapping) and value.get("execution_status") == "blocked"


def _handled_error_message(result: Any) -> str | None:
    """提取已被局部故障中间件处理的异常信息。"""
    value = _result_mapping(result)
    if value is None or value.get("handled") is not True:
        return None
    message = value.get("error_message")
    return str(message) if message else "工具执行失败，已按局部故障处理。"


def _extract_rule_execution_ids(tool_name: str, result: Any) -> tuple[str, ...]:
    """从写回 Tool 原始结果提取执行 ID，避免依赖已截断的审计文本。"""
    if tool_name not in {
        "record_rule_execution_tool",
        "record_rule_executions_batch_tool",
    }:
        return ()
    payload = _result_mapping(result)
    if payload is None:
        return ()
    if tool_name == "record_rule_execution_tool":
        execution_id = payload.get("execution_id")
        return (str(execution_id),) if execution_id else ()

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return ()
    return tuple(
        str(item["execution_id"])
        for item in raw_items
        if isinstance(item, Mapping) and item.get("status") == "ok" and item.get("execution_id")
    )


def _result_mapping(result: Any) -> Mapping[str, Any] | None:
    value = result
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        value = text_attr
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, Mapping) else None
