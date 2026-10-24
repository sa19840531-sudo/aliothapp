"""页面状态图用例。

这个用例只处理图投影的查询和按权威 Capture/Action 重建，不参与设备操作、
覆盖计算或报告发布。页面图不可用时显式返回 ``unavailable``，避免前端把
空列表理解成“任务没有探索页面”。
"""

from __future__ import annotations

from typing import Any

from services.api.app.application.page_graph_contracts import (
    PageGraphRepository,
    PageGraphUnavailableError,
)
from services.api.app.application.page_graph_projection import (
    build_exploration_tree,
    build_recovery_routes,
)


class PageGraphService:
    """页面图查询与重建用例。"""

    def __init__(self, *, repository: PageGraphRepository | None = None) -> None:
        self._repository = repository

    async def get_graph(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        finalization_epoch: int | None,
    ) -> dict[str, Any]:
        if self._repository is None:
            return _unavailable(task_id, "graph_repository_not_configured")
        graph = await self._repository.get_graph(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        if graph.get("status") == "unavailable":
            return graph
        graph["exploration_tree"] = build_exploration_tree(
            graph,
            progress_facts=graph.pop("progress_facts", []),
            candidates=graph.pop("candidates", []),
        )
        return graph

    async def get_recovery_route(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        finalization_epoch: int | None,
        source_state_id: str | None = None,
        target_identity: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if self._repository is None:
            return _unavailable(task_id, "graph_repository_not_configured")
        graph = await self._repository.get_graph(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        if graph.get("status") != "available":
            return {
                **_unavailable(task_id, "graph_projection_not_complete"),
                "graph_status": graph.get("status"),
                "routes": [],
            }
        return {
            "protocol_version": "page_graph_recovery.v1",
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "finalization_epoch": finalization_epoch,
            "target_platform": graph.get("target_platform"),
            **build_recovery_routes(
                graph,
                source_state_id=source_state_id,
                target_identity=target_identity,
                limit=limit,
            ),
        }

    async def rebuild_run(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int | None = None,
    ) -> Any:
        if self._repository is None:
            raise PageGraphUnavailableError("graph_repository_not_configured")
        return await self._repository.rebuild_run(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )


def _unavailable(task_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "protocol_version": "page_graph.v1",
        "status": "unavailable",
        "task_id": task_id,
        "reason_code": reason_code,
        "states": [],
        "transitions": [],
        "recovery_policy": {
            "eligible_only": "completed_low_risk_trusted",
            "requires_stepwise_reverification": True,
        },
    }
