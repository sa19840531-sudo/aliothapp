"""V1 Main Agent 派发的稳定兼容入口。"""

from __future__ import annotations

from services.api.app.agents.runtime.legacy_dispatch import (
    LegacyV1WorkDispatcher,
    WorkDispatch,
)
from services.api.app.agents.runtime.legacy_dispatch import (
    prepare_page_rule_dispatch as _prepare_page_rule_dispatch,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory

__all__ = ["WorkDispatch", "prepare_page_rule_dispatch", "prepare_work_dispatch"]

_LEGACY_DISPATCHER = LegacyV1WorkDispatcher()


def prepare_work_dispatch(working_memory: WorkingMemory) -> WorkDispatch | None:
    """保留 V1 调用方式；目标 V2 Explorer Agent 不经过本入口。"""
    return _LEGACY_DISPATCHER.prepare_next(working_memory)


def prepare_page_rule_dispatch(
    working_memory: WorkingMemory,
    *,
    page_capture_id: str,
) -> WorkDispatch | None:
    """保留后台静态页面规则派发入口。"""
    return _prepare_page_rule_dispatch(
        working_memory,
        page_capture_id=page_capture_id,
    )
