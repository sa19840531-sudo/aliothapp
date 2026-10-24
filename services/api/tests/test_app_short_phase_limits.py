"""业务阶段在十二／二十四轮边界后局部恢复，不能无限等待。"""

import pytest

from services.api.app.application import app_three_contract_driver as driver
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.domain.interaction import TimedObservationIntent
from services.api.tests.test_app_three_contract_driver import work_for


@pytest.mark.parametrize("phase,limit,stage", [
    ("video_entry", 12, "home_video"),
    ("payment_alipay", 24, "payment_alipay"),
])
def test_stalled_phase_recovers_immediately_after_limit(monkeypatch, phase, limit, stage):
    # 隔离业务动作，模拟连续画面没有进展；保留真实预算与恢复逻辑。
    monkeypatch.setattr(driver, "try_tangguo_flow_driver", lambda work: None)
    state = TangguoFlowState(phase=phase)
    for index in range(limit):
        result = driver.decide_app_three_contracts(work_for(
            state=state, page_state="stable", capture_id=f"limit-{index}",
        ))
        assert isinstance(result, TimedObservationIntent)
        state = TangguoFlowState.from_dict(result.workflow_state)
        assert state.phase == phase
        assert not state.recovery_events
    result = driver.decide_app_three_contracts(work_for(state=state, page_state="stable"))
    assert isinstance(result, TimedObservationIntent)
    state = TangguoFlowState.from_dict(result.workflow_state)
    assert state.stage_results[stage] == "blocked"
    assert f"phase_round_limit:{phase}" in state.recovery_events
    assert state.phase != phase
