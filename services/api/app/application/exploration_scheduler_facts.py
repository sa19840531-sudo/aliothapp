"""探索调度所需的持久化事实读取端口。

调度器只消费已经由 Coverage、Evidence Index 或规则证据请求写入的事实。
本模块定义应用层端口和一个小的合并辅助函数，数据库查询放在 infrastructure
层，避免 H5/App 工作流直接依赖 PostgreSQL。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

SchedulerSurface = Literal["h5", "app"]


class ExplorationSchedulerFactsReader(Protocol):
    """读取当前任务运行面真实证据事实的最小端口。"""

    async def read_h5_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None = None,
    ) -> Mapping[str, object]: ...

    async def read_app_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None = None,
    ) -> Mapping[str, object]: ...


def merge_exploration_scheduler_facts(
    metadata: Mapping[str, object] | None,
    facts: Mapping[str, object] | None,
    *,
    surface: SchedulerSurface | None = None,
) -> dict[str, object]:
    """把本轮数据库事实合并到 Frontier metadata，不丢失既有累计信息。"""

    result = dict(metadata) if isinstance(metadata, Mapping) else {}
    if not isinstance(facts, Mapping):
        return result
    if surface == "h5":
        stale_keys = ("h5_coverage_snapshot_fact",)
    elif surface == "app":
        stale_keys = ("app_evidence_index", "app_rule_inventory_coverage")
    else:
        stale_keys = ()
    for key in stale_keys:
        if key not in facts:
            result.pop(key, None)
    for key, value in facts.items():
        if isinstance(key, str) and key.strip():
            result[key] = value
    return result


__all__ = [
    "ExplorationSchedulerFactsReader",
    "SchedulerSurface",
    "merge_exploration_scheduler_facts",
]
