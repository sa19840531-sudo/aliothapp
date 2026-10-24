"""活会话 Capture 小批发布的稳定进度合同。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiveInspectionPublicationBatchResult:
    """一次有界 Capture 扫描结果；新建数量不代表扫描进度。"""

    attempted_count: int
    created_count: int
    has_more: bool
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if self.attempted_count < 0 or self.created_count < 0:
            raise ValueError("attempted_count 和 created_count 必须为非负整数")
        if self.has_more and (self.attempted_count == 0 or not self.next_cursor):
            raise ValueError("has_more=True 时必须有已尝试 Capture 和 next_cursor")
        if not self.has_more and self.next_cursor is not None:
            raise ValueError("has_more=False 时 next_cursor 必须为 None")


@dataclass(frozen=True, slots=True)
class StablePublicationBatch[T]:
    """从稳定排序候选中选择的本轮 Capture。"""

    items: tuple[T, ...]
    has_more: bool
    next_cursor: str | None


def select_stable_publication_batch[T](
    items: Sequence[T],
    *,
    identity: Callable[[T], str],
    limit: int,
    cursor: str | None,
) -> StablePublicationBatch[T]:
    """从 cursor 后选择一批；cursor 已被 PG 过滤掉时从当前首项恢复。"""

    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    start = 0
    if cursor is not None:
        for index, item in enumerate(items):
            if identity(item) == cursor:
                start = index + 1
                break
    selected = tuple(items[start : start + limit])
    has_more = start + len(selected) < len(items)
    next_cursor = identity(selected[-1]) if selected and has_more else None
    return StablePublicationBatch(
        items=selected,
        has_more=has_more,
        next_cursor=next_cursor,
    )


__all__ = [
    "LiveInspectionPublicationBatchResult",
    "StablePublicationBatch",
    "select_stable_publication_batch",
]
