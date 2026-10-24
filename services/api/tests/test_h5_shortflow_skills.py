"""短流程按冻结 Skill 单独加载，拒绝串流程和静默漂移。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.api.app.application.h5_checkpoint_planning import H5CheckpointPlanner
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.domain.journey.definitions import build_run_journey_execution_plan
from services.api.tests.test_h5_adaptive_journey import selected_work
from services.api.tests.test_h5_shortflow_activation import definition_payload


@pytest.mark.parametrize("name", ["h5-player-workflow", "h5-payment-workflow"])
async def test_skill_provider_contains_only_selected_shortflow_and_base_explorer(name):
    from services.api.app.agents.skills.loader import build_h5_shortflow_context_provider

    provider = build_h5_shortflow_context_provider(name)
    skills, _, _ = await provider._get_or_create_context()
    assert {skill.frontmatter.name for skill in skills} == {"h5-explorer-workflow", name}
    assert await provider.load_skill(name)


def test_unknown_skill_cannot_read_arbitrary_directory():
    from services.api.app.agents.skills.loader import h5_shortflow_skill_hash

    with pytest.raises(ValueError):
        h5_shortflow_skill_hash("../../rule/payment-rule")


async def test_planner_routes_frozen_skill_without_loading_other_workflows():
    from services.api.app.agents.skills.loader import h5_shortflow_skill_hash

    work = selected_work()
    plan = build_run_journey_execution_plan(
        parse_journey_definition(definition_payload()),
        task_id=work.task_id,
        agent_run_id="run",
        scenario_key="pause",
        parameters={
            "workflow_skill": "h5-player-workflow",
            "workflow_skill_hash": h5_shortflow_skill_hash("h5-player-workflow"),
        },
    )
    legacy = Mock(side_effect=AssertionError("不能调用普通探索 Agent"))
    agent = SimpleNamespace(decide_next=AsyncMock(return_value=None))
    factory = Mock(return_value=agent)
    planner = H5CheckpointPlanner(delegate_factory=legacy, shortflow_delegate_factory=factory)
    assert await planner.prepare(work=work, plan=plan, checkpoint=plan.checkpoints[0]) is None
    factory.assert_called_once_with("h5-player-workflow", plan.parameters["workflow_skill_hash"])
    assert "paused" in agent.decide_next.await_args.args[0].objective
    legacy.assert_not_called()


async def test_drifted_skill_blocks_before_any_model_request():
    work = selected_work()
    plan = build_run_journey_execution_plan(
        parse_journey_definition(definition_payload()),
        task_id=work.task_id,
        agent_run_id="run",
        scenario_key="pause",
        parameters={"workflow_skill": "h5-player-workflow", "workflow_skill_hash": "0" * 64},
    )
    factory = Mock()
    planner = H5CheckpointPlanner(delegate_factory=Mock(), shortflow_delegate_factory=factory)
    with pytest.raises(ValueError, match="漂移"):
        await planner.prepare(work=work, plan=plan, checkpoint=plan.checkpoints[0])
    factory.assert_not_called()


def test_base_explorer_boundary_changes_also_invalidate_frozen_skill(tmp_path, monkeypatch):
    import shutil

    from services.api.app.agents.skills import loader

    before = loader.h5_shortflow_skill_hash("h5-player-workflow")
    for name in ("h5-player-workflow", "h5-explorer-workflow"):
        shutil.copytree(loader._SKILLS_ROOT / name, tmp_path / name)
    monkeypatch.setattr(loader, "_SKILLS_ROOT", tmp_path)
    assert loader.h5_shortflow_skill_hash("h5-player-workflow") == before
    base = tmp_path / "h5-explorer-workflow/SKILL.md"
    base.write_text(base.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert loader.h5_shortflow_skill_hash("h5-player-workflow") != before
