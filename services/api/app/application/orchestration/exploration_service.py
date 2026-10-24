"""只构造与发布 Exploration Work 的应用编排入口。"""

from __future__ import annotations

from services.api.app.application.orchestration.commands import StartExplorationCommand
from services.api.app.application.orchestration.maf_workflow import (
    build_start_exploration_workflow,
    prepare_exploration_work,
    require_exploration_work_output,
)
from services.api.app.application.orchestration.ports import ExplorationWorkPublisher
from services.api.app.domain.exploration import ExplorationWork


class ExplorationOrchestrator:
    """H5 专用的纯页面探索工作编排器。"""

    def __init__(self, *, exploration_publisher: ExplorationWorkPublisher) -> None:
        self._exploration_publisher = exploration_publisher

    async def start_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork:
        workflow = build_start_exploration_workflow(self._exploration_publisher)
        result = await workflow.run(command, include_status_events=True)
        return require_exploration_work_output(result)

    async def prepare_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork:
        """只构造后继 Work，由父 Work 租约事务负责提交。"""
        return prepare_exploration_work(command)
