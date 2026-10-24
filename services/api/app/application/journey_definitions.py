"""Journey Definition/Version 发布与解析端口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.domain.journey import (
    JourneyDefinition,
    JourneyExecutionPlan,
    build_journey_execution_plan,
)
from services.api.app.domain.journey.definitions import build_run_journey_execution_plan


class JourneyVersionConflictError(RuntimeError):
    """同一版本内容不同，或新版本没有向前递增。"""


class JourneyDefinitionNotFoundError(LookupError):
    """请求的 Journey Definition 版本不存在。"""


class JourneyDefinitionRepository(Protocol):
    """持久化适配器需要实现的最小仓储端口。"""

    async def get(
        self,
        *,
        definition_id: str,
        version: int,
    ) -> JourneyDefinition | None: ...

    async def get_latest(self, *, definition_id: str) -> JourneyDefinition | None: ...

    async def add(self, *, definition: JourneyDefinition) -> bool:
        """原子插入版本；身份已存在时返回 False，禁止覆盖。"""
        ...

    async def get_execution_plan(
        self,
        *,
        execution_plan_id: str,
    ) -> JourneyExecutionPlan | None: ...

    async def get_execution_plan_for_task(
        self,
        *,
        task_id: str,
    ) -> JourneyExecutionPlan | None: ...

    async def add_execution_plan(self, *, plan: JourneyExecutionPlan) -> bool:
        """旧计划按任务冻结，检查点计划按 Run、流程与场景冻结。"""
        ...

    async def get_execution_plan_for_scope(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        definition_id: str,
        scenario_key: str,
    ) -> JourneyExecutionPlan | None: ...


@dataclass(frozen=True, kw_only=True)
class PublishedJourneyDefinition:
    """发布结果同时表达版本身份和是否新建。"""

    definition: JourneyDefinition
    created: bool


class JourneyDefinitionService:
    """保证 Journey 版本不可覆盖，并生成锁定执行计划。"""

    def __init__(self, *, repository: JourneyDefinitionRepository) -> None:
        self._repository = repository

    async def publish(self, definition: JourneyDefinition) -> PublishedJourneyDefinition:
        existing = await self._repository.get(
            definition_id=definition.definition_id,
            version=definition.version,
        )
        if existing is not None:
            if existing.content_hash != definition.content_hash:
                raise JourneyVersionConflictError("同一 Journey 版本不能覆盖不同内容")
            return PublishedJourneyDefinition(definition=existing, created=False)

        latest = await self._repository.get_latest(definition_id=definition.definition_id)
        if latest is not None and definition.version <= latest.version:
            raise JourneyVersionConflictError("Journey 新版本必须大于当前最新版本")
        created = await self._repository.add(definition=definition)
        if created:
            return PublishedJourneyDefinition(definition=definition, created=True)
        winner = await self._repository.get(
            definition_id=definition.definition_id,
            version=definition.version,
        )
        if winner is not None and winner.content_hash == definition.content_hash:
            return PublishedJourneyDefinition(definition=winner, created=False)
        raise JourneyVersionConflictError("同一 Journey 版本已被其他内容占用")

    async def resolve(
        self,
        *,
        definition_id: str,
        version: int,
        expected_content_hash: str | None = None,
    ) -> JourneyDefinition:
        definition = await self._repository.get(
            definition_id=definition_id,
            version=version,
        )
        if definition is None:
            raise JourneyDefinitionNotFoundError("Journey Definition 版本不存在")
        if (
            expected_content_hash is not None
            and definition.content_hash != expected_content_hash
        ):
            raise JourneyVersionConflictError("Journey Definition 内容哈希已漂移")
        return definition

    async def create_execution_plan(
        self,
        *,
        task_id: str,
        definition_id: str,
        version: int,
        expected_content_hash: str | None = None,
        rule_contract_hashes: tuple[str, ...] = (),
        capture_plan_id: str | None = None,
    ) -> JourneyExecutionPlan:
        definition = await self.resolve(
            definition_id=definition_id,
            version=version,
            expected_content_hash=expected_content_hash,
        )
        plan = build_journey_execution_plan(
            definition,
            task_id=task_id,
            rule_contract_hashes=rule_contract_hashes,
            capture_plan_id=capture_plan_id,
        )
        existing = await self._repository.get_execution_plan_for_task(task_id=task_id)
        if existing is not None:
            if existing.execution_plan_id != plan.execution_plan_id:
                raise JourneyVersionConflictError("任务已经锁定另一份 Journey 执行计划")
            return existing
        if await self._repository.add_execution_plan(plan=plan):
            return plan
        winner = await self._repository.get_execution_plan_for_task(task_id=task_id)
        if winner is not None and winner.execution_plan_id == plan.execution_plan_id:
            return winner
        raise JourneyVersionConflictError("任务 Journey 执行计划被并发占用")

    async def resolve_execution_plan(
        self,
        *,
        execution_plan_id: str,
    ) -> JourneyExecutionPlan:
        """按不可变身份读取任务执行快照。"""
        plan = await self._repository.get_execution_plan(
            execution_plan_id=execution_plan_id
        )
        if plan is None:
            raise JourneyDefinitionNotFoundError("Journey 执行计划不存在")
        return plan

    async def create_run_execution_plan(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        definition_id: str,
        version: int,
        scenario_key: str,
        parameters: Mapping[str, Any],
        expected_content_hash: str | None = None,
        rule_contract_hashes: Sequence[str] = (),
        capture_plan_id: str | None = None,
    ) -> JourneyExecutionPlan:
        """冻结一个 Run 场景；并发只能复用同内容，不能覆盖已有版本。"""
        definition = await self.resolve(
            definition_id=definition_id,
            version=version,
            expected_content_hash=expected_content_hash,
        )
        plan = build_run_journey_execution_plan(
            definition,
            task_id=task_id,
            agent_run_id=agent_run_id,
            scenario_key=scenario_key,
            parameters=parameters,
            rule_contract_hashes=rule_contract_hashes,
            capture_plan_id=capture_plan_id,
        )
        scope = dict(
            task_id=task_id,
            agent_run_id=agent_run_id,
            definition_id=definition_id,
            scenario_key=scenario_key,
        )
        existing = await self._repository.get_execution_plan_for_scope(**scope)
        if existing is not None:
            if existing != plan:
                raise JourneyVersionConflictError("Run 场景已经锁定另一份 Journey 执行计划")
            return existing
        if await self._repository.add_execution_plan(plan=plan):
            return plan
        winner = await self._repository.get_execution_plan_for_scope(**scope)
        if winner != plan:
            raise JourneyVersionConflictError("Run 归属无效或并发锁定了不同 Journey 执行计划")
        return winner
