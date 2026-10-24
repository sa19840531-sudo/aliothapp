"""短流程检查点合同：重现单计划冲突，不用元数据冒充正式执行。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.api.app.application.journey_definitions import (
    JourneyDefinitionService,
    JourneyVersionConflictError,
)
from services.api.app.domain.journey import (
    JourneyValidationError,
    parse_journey_definition,
    parse_journey_execution_plan,
)
from services.api.tests.test_journey_definitions import (
    FakeJourneyDefinitionRepository,
    _definition,
)


def checkpoint_definition(definition_id="player"):
    payload = {
        "schema_version": "journey-dsl.v2",
        "definition_id": definition_id,
        "version": 1,
        "name": "播放器短流程" if definition_id == "player" else "支付展示短流程",
        "surface_kinds": ["web"],
        "checkpoints": [
            {
                "id": "ready",
                "entry_condition": {"surface": "video_detail"},
                "expected": {"player_visible": True, "media_ready": True},
                "evidence_requirements": [
                    {"role": "baseline", "artifact_types": ["screenshot", "dom", "media_state"]}
                ],
                "depends_on": [],
                "exit_condition": {"stable_frame": True},
                "recovery_policy": "same_session",
                "side_effect_policy": "read_only",
            },
            {
                "id": "paused",
                "entry_condition": {"playing": True},
                "expected": {"paused": True, "media_time_stopped": True},
                "evidence_requirements": [
                    {
                        "role": "after_pause",
                        "artifact_types": ["action_receipt", "media_state"],
                        "minimum_count": 2,
                    }
                ],
                "depends_on": ["ready"],
                "exit_condition": {"observation_complete": True},
                "recovery_policy": "new_instance",
                "side_effect_policy": "read_only",
            },
        ],
    }
    if definition_id == "payment":
        payload["checkpoints"] = [
            {
                "id": "display",
                "entry_condition": {"recharge_entry_visible": True},
                "expected": {"amount_displayed": True, "order_created": False},
                "evidence_requirements": [
                    {"role": "display_panel", "artifact_types": ["screenshot", "dom"]}
                ],
                "depends_on": [],
                "exit_condition": {"display_only": True},
                "recovery_policy": "blocked",
                "side_effect_policy": "read_only",
            }
        ]
    return payload


class ScopedRepository(FakeJourneyDefinitionRepository):
    async def get_execution_plan_for_scope(
        self, *, task_id, agent_run_id, definition_id, scenario_key
    ):
        return next(
            (
                plan
                for plan in self.execution_plans.values()
                if (
                    plan.task_id,
                    plan.agent_run_id,
                    plan.journey_version.definition_id,
                    plan.scenario_key,
                )
                == (task_id, agent_run_id, definition_id, scenario_key)
            ),
            None,
        )

    async def add_execution_plan(self, *, plan):
        if plan.agent_run_id is None:
            return await super().add_execution_plan(plan=plan)
        existing = await self.get_execution_plan_for_scope(
            task_id=plan.task_id,
            agent_run_id=plan.agent_run_id,
            definition_id=plan.journey_version.definition_id,
            scenario_key=plan.scenario_key,
        )
        if existing:
            return False
        self.execution_plans[plan.execution_plan_id] = plan
        return True


def test_checkpoint_contract_round_trip_and_hash_includes_evidence_obligations():
    payload = checkpoint_definition()
    definition = parse_journey_definition(payload)
    assert definition.steps == ()
    assert definition.to_dict() == payload
    assert parse_journey_definition(definition.to_dict()) == definition
    modified = deepcopy(payload)
    modified["checkpoints"][1]["evidence_requirements"][0]["minimum_count"] = 3
    assert parse_journey_definition(modified).content_hash != definition.content_hash
    with pytest.raises(TypeError):
        definition.checkpoints[0].expected["player_visible"] = False


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["checkpoints"][1].update(depends_on=["missing"]),
        lambda p: p["checkpoints"][0].update(depends_on=["paused"]),
        lambda p: p["checkpoints"][1].update(id="ready"),
        lambda p: p["checkpoints"][0].update(expected={}),
        lambda p: p["checkpoints"][0].update(exit_condition={}),
        lambda p: p["checkpoints"][0].update(evidence_requirements=[]),
        lambda p: p["checkpoints"][0].update(
            evidence_requirements=[
                {"role": "baseline", "artifact_types": ["dom"]},
                {"role": "baseline", "artifact_types": ["screenshot"]},
            ]
        ),
        lambda p: p["checkpoints"][0].update(side_effect_policy="create_order"),
        lambda p: p["checkpoints"][0].update(recovery_policy="replay_payment"),
        lambda p: p["checkpoints"][0].update(selector=".pause"),
        lambda p: p["checkpoints"][0]["expected"].update(driver="runtime"),
        lambda p: p["checkpoints"][0]["evidence_requirements"][0].update(minimum_count=True),
        lambda p: p["checkpoints"][0]["evidence_requirements"][0].update(
            artifact_types=["unknown"]
        ),
        lambda p: p.update(
            steps=[{"id": "tap", "type": "action", "operation": "tap", "semantic_target": "暂停"}]
        ),
    ],
)
def test_invalid_contract_rejected_before_planning(change):
    payload = checkpoint_definition()
    change(payload)
    with pytest.raises(JourneyValidationError):
        parse_journey_definition(payload)


async def test_player_and_payment_freeze_independently_in_one_run_without_overwriting_history():
    repository = ScopedRepository()
    service = JourneyDefinitionService(repository=repository)
    await service.publish(_definition())
    legacy = await service.create_execution_plan(task_id="task", definition_id="login", version=1)
    plans = []
    for definition_id in ("player", "payment"):
        definition = parse_journey_definition(checkpoint_definition(definition_id))
        await service.publish(definition)
        plans.append(
            await service.create_run_execution_plan(
                task_id="task",
                agent_run_id="run",
                definition_id=definition_id,
                version=1,
                scenario_key="manual",
                parameters={"media_kind": "video"},
            )
        )
    assert plans[0].execution_plan_id != plans[1].execution_plan_id
    assert repository.execution_plans[legacy.execution_plan_id] == legacy
    for plan in plans:
        assert parse_journey_execution_plan(plan.to_dict()) == plan
        assert plan.checkpoints
        assert plan.steps == ()
    repeated = await service.create_run_execution_plan(
        task_id="task",
        agent_run_id="run",
        definition_id="player",
        version=1,
        scenario_key="manual",
        parameters={"media_kind": "video"},
    )
    assert repeated == plans[0]
    with pytest.raises(JourneyVersionConflictError, match="锁定"):
        await service.create_run_execution_plan(
            task_id="task",
            agent_run_id="run",
            definition_id="player",
            version=1,
            scenario_key="manual",
            parameters={"media_kind": "audio"},
        )
    other_run = await service.create_run_execution_plan(
        task_id="task",
        agent_run_id="run-2",
        definition_id="player",
        version=1,
        scenario_key="manual",
        parameters={"media_kind": "audio"},
    )
    assert other_run.execution_plan_id != plans[0].execution_plan_id


async def test_checkpoint_definition_cannot_use_legacy_task_plan_entry():
    service = JourneyDefinitionService(repository=ScopedRepository())
    await service.publish(parse_journey_definition(checkpoint_definition()))
    with pytest.raises(JourneyValidationError, match="Run"):
        await service.create_execution_plan(task_id="task", definition_id="player", version=1)


def test_v1_definition_hash_and_shape_remain_unchanged():
    definition = _definition()
    assert "checkpoints" not in definition.to_dict()
    assert (
        definition.content_hash
        == "adfab42e20582d0171d42e8e20f2a2f6dd5c0b76c7855d4ac9b7e174e758cdbe"
    )


async def test_legacy_runner_rejects_checkpoint_plan_before_start_or_action():
    from services.api.app.application.journey_runner import JourneyRunner
    from services.api.app.domain.journey.definitions import build_run_journey_execution_plan

    plan = build_run_journey_execution_plan(
        parse_journey_definition(checkpoint_definition()),
        task_id="task",
        agent_run_id="run",
        scenario_key="manual",
        parameters={},
    )
    repository = SimpleNamespace()
    runner = JourneyRunner(repository=repository, router=None)
    with pytest.raises(JourneyValidationError, match="检查点"):
        await runner.run(
            plan=plan,
            spec=SimpleNamespace(
                task_id="task",
                execution_plan_id=plan.execution_plan_id,
            ),
            policy=None,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_run_id", None),
        ("scenario_key", None),
        ("schema_version", "journey-execution-plan.v1"),
        ("steps", []),
        ("parameters", {"selector": ".pay"}),
    ],
)
async def test_scoped_payload_cannot_strip_scope_or_smuggle_runtime_locator(field, value):
    service = JourneyDefinitionService(repository=ScopedRepository())
    await service.publish(parse_journey_definition(checkpoint_definition()))
    plan = await service.create_run_execution_plan(
        task_id="task",
        agent_run_id="run",
        definition_id="player",
        version=1,
        scenario_key="manual",
        parameters={},
    )
    payload = plan.to_dict()
    payload[field] = value
    with pytest.raises(JourneyValidationError):
        parse_journey_execution_plan(payload)


async def test_persisted_contract_cannot_change_checkpoints_without_changing_content_identity():
    service = JourneyDefinitionService(repository=ScopedRepository())
    await service.publish(parse_journey_definition(checkpoint_definition()))
    plan = await service.create_run_execution_plan(
        task_id="task",
        agent_run_id="run",
        definition_id="player",
        version=1,
        scenario_key="manual",
        parameters={},
    )
    payload = plan.to_dict()
    payload["checkpoints"][0]["expected"]["media_ready"] = False
    with pytest.raises(JourneyValidationError, match="内容身份"):
        parse_journey_execution_plan(payload)
