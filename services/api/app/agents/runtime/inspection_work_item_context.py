"""持久化质检 Attempt 与统一 Judge 之间的运行时交接上下文。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Protocol


class InspectionJudgementSink(Protocol):
    """在正式规则写回前持久化完整且有界的 Judge 结果。"""

    async def persist_judgement(self, result: Mapping[str, Any]) -> None:
        """保存 Judge 结果；失败必须阻止后续正式写回。"""


_CURRENT_SINK: ContextVar[InspectionJudgementSink | None] = ContextVar(
    "alioth_inspection_judgement_sink",
    default=None,
)


@asynccontextmanager
async def bind_inspection_judgement_sink(
    sink: InspectionJudgementSink,
) -> AsyncIterator[None]:
    """把当前 Tool 调用绑定到一个持久化工作项 Attempt。"""
    token = _CURRENT_SINK.set(sink)
    try:
        yield
    finally:
        _CURRENT_SINK.reset(token)


async def persist_inspection_judgement(result: Mapping[str, Any]) -> None:
    """若当前处于 Enforced 消费上下文，则在规则写回前落地 Judge 结果。"""
    sink = _CURRENT_SINK.get()
    if sink is not None:
        await sink.persist_judgement(result)
