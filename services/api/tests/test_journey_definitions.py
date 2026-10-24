"""Journey Definition/Version 应用服务测试。"""

import pytest

from services.api.app.application.journey_definitions import (
    JourneyDefinitionNotFoundError,
    JourneyDefinitionService,
    JourneyVersionConflictError,
)
from services.api.app.domain.journey import (
    parse_journey_definition,
    parse_journey_execution_plan,
)


class FakeJourneyDefinitionRepository:
    def __init__(self) -> None:
        self.definitions = {}
        self.execution_plans = {}

    async def get(self, *, definition_id, version):
        return self.definitions.get((definition_id, version))

    async def get_latest(self, *, definition_id):
        matches = [
            value
            for (stored_id, _), value in self.definitions.items()
            if stored_id == definition_id
        ]
        return max(matches, key=lambda item: item.version, default=None)

    async def add(self, *, definition):
        if (definition.definition_id, definition.version) in self.definitions:
            return False
        self.definitions[(definition.definition_id, definition.version)] = definition
        return True

    async def get_execution_plan(self, *, execution_plan_id):
        return self.execution_plans.get(execution_plan_id)

    async def get_execution_plan_for_task(self, *, task_id):
        return next(
            (
                plan
                for plan in self.execution_plans.values()
                if plan.task_id == task_id
            ),
            None,
        )

    async def add_execution_plan(self, *, plan):
        if await self.get_execution_plan_for_task(task_id=plan.task_id) is not None:
            return False
        self.execution_plans[plan.execution_plan_id] = plan
        return True


def _definition(*, version: int = 1, operation: str = "tap"):
    return parse_journey_definition(
        {
            "schema_version": "journey-dsl.v1",
            "definition_id": "login",
            "version": version,
            "name": "登录",
            "surface_kinds": ["web"],
            "steps": [
                {
                    "id": "open-login",
                    "type": "action",
                    "operation": operation,
                    "semantic_target": "登录按钮",
                }
            ],
        }
    )


async def test_publish_is_idempotent_for_same_version_and_content() -> None:
    repository = FakeJourneyDefinitionRepository()
    service = JourneyDefinitionService(repository=repository)

    first = await service.publish(_definition())
    second = await service.publish(_definition())

    assert first.created is True
    assert second.created is False
    assert second.definition.content_hash == first.definition.content_hash


async def test_publish_rejects_same_version_with_different_content() -> None:
    service = JourneyDefinitionService(repository=FakeJourneyDefinitionRepository())
    await service.publish(_definition())

    with pytest.raises(JourneyVersionConflictError, match="不能覆盖"):
        await service.publish(_definition(operation="click"))


async def test_publish_rejects_version_older_than_latest() -> None:
    repository = FakeJourneyDefinitionRepository()
    service = JourneyDefinitionService(repository=repository)
    repository.definitions[("login", 2)] = _definition(version=2)

    with pytest.raises(JourneyVersionConflictError, match="大于"):
        await service.publish(_definition(version=1))


async def test_execution_plan_requires_exact_published_hash() -> None:
    service = JourneyDefinitionService(repository=FakeJourneyDefinitionRepository())
    published = await service.publish(_definition())

    plan = await service.create_execution_plan(
        task_id="task-1",
        definition_id="login",
        version=1,
        expected_content_hash=published.definition.content_hash,
        rule_contract_hashes=("rule-1",),
    )
    assert plan.journey_version == published.definition.version_ref

    with pytest.raises(JourneyVersionConflictError, match="漂移"):
        await service.create_execution_plan(
            task_id="task-1",
            definition_id="login",
            version=1,
            expected_content_hash="stale-hash",
        )


async def test_execution_plan_is_immutable_per_task() -> None:
    repository = FakeJourneyDefinitionRepository()
    service = JourneyDefinitionService(repository=repository)
    await service.publish(_definition())
    await service.publish(_definition(version=2, operation="click"))

    first = await service.create_execution_plan(
        task_id="task-1",
        definition_id="login",
        version=1,
    )
    repeated = await service.create_execution_plan(
        task_id="task-1",
        definition_id="login",
        version=1,
    )
    assert repeated == first

    with pytest.raises(JourneyVersionConflictError, match="已经锁定"):
        await service.create_execution_plan(
            task_id="task-1",
            definition_id="login",
            version=2,
        )


async def test_resolve_reports_missing_version() -> None:
    service = JourneyDefinitionService(repository=FakeJourneyDefinitionRepository())

    with pytest.raises(JourneyDefinitionNotFoundError):
        await service.resolve(definition_id="missing", version=1)


async def test_execution_plan_round_trips_as_persisted_json() -> None:
    service = JourneyDefinitionService(repository=FakeJourneyDefinitionRepository())
    await service.publish(_definition())
    plan = await service.create_execution_plan(
        task_id="task-1",
        definition_id="login",
        version=1,
        rule_contract_hashes=("contract-1",),
    )

    assert parse_journey_execution_plan(plan.to_dict()) == plan
