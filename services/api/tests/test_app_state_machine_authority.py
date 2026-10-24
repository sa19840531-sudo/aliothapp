"""状态机没有合法决策时必须阻塞，不得让模型或旧调度接管路径。"""

from dataclasses import replace

import pytest

from services.api.app.agents.explorer import MafExplorerAgent
from services.api.app.application import app_three_contract_driver as driver
from services.api.app.domain.exploration import ExplorationTerminal, ExplorationTerminalStatus
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_app_three_contract_driver import NoModel, work_for


@pytest.mark.parametrize("method", ["decide_next", "decide_deterministic"])
@pytest.mark.parametrize("surface", [SurfaceKind.ANDROID, SurfaceKind.IOS])
@pytest.mark.parametrize("output", [None, {"next_phase": "payment_alipay"}])
async def test_missing_or_invalid_decision_cannot_fall_back(monkeypatch, method, surface, output):
    monkeypatch.setattr(driver, "decide_app_three_contracts", lambda *a, **k: output)
    work = work_for()
    observation = replace(work.frontier.observation, surface_kind=surface)
    work = replace(work, surface_kind=surface,
                   frontier=replace(work.frontier, observation=observation))
    decision = await getattr(MafExplorerAgent(agent=NoModel()), method)(work)
    assert isinstance(decision, ExplorationTerminal)
    assert decision.status == ExplorationTerminalStatus.BLOCKED
    assert decision.reason == "app_state_machine_decision_missing_or_invalid"
