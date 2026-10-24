"""Capture 围栏上下文：供 Official Driver 在帧内诊断微手势透传 execution_context。

Player wake 等 Capture 侧微触必须带 Coordinator 租约上下文，否则星语上游
会返回 EXECUTION_CONTEXT_REQUIRED。上下文仅在 AppCaptureWriter.capture
调用栈内有效，不写入日志或动作轨迹。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CAPTURE_LEASE_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "app_capture_lease_context",
    default=None,
)


def get_capture_lease_context() -> Mapping[str, Any] | None:
    """读取当前 Capture 调用栈上的租约上下文；无则返回 None。"""

    ctx = _CAPTURE_LEASE_CONTEXT.get()
    if not isinstance(ctx, Mapping) or not ctx:
        return None
    return ctx


@contextmanager
def bind_capture_lease_context(context: Mapping[str, Any] | None) -> Iterator[None]:
    """在 Driver.capture 期间绑定租约上下文。"""

    if not isinstance(context, Mapping) or not context:
        yield
        return
    token = _CAPTURE_LEASE_CONTEXT.set(dict(context))
    try:
        yield
    finally:
        _CAPTURE_LEASE_CONTEXT.reset(token)
