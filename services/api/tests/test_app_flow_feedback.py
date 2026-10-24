"""未执行点按、底栏验证及业务终态封存合同回归。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.app_flow_action_feedback import reconcile_unexecuted_flow_action
from services.api.app.application.app_flow_decisions import flow_passive_decision
from services.api.app.application.app_flow_navigation import tab_destination_confirmed
from services.api.app.application.app_runtime_composition import _business_outcome
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.tests.test_app_three_contract_driver import candidate, work_for


def test_video_preexecution_rejection_relocates_once_without_resetting_budget():
    state = TangguoFlowState(phase="video_verify", video_target="立即看", video_attempts=1,
                            attempted_video_labels=["立即看"], device_actions_used=5)
    lineage = {"tangguo_flow_state": state.to_dict()}
    reconcile_unexecuted_flow_action(lineage, {"tangguo_flow_phase": "video_entry"})
    saved = TangguoFlowState.from_dict(lineage["tangguo_flow_state"])
    assert saved.phase == "video_entry"
    assert saved.video_attempts == 0
    assert saved.attempted_video_labels == []
    assert saved.device_actions_used == 5
    reconcile_unexecuted_flow_action(lineage, {"tangguo_flow_phase": "video_entry"})
    saved = TangguoFlowState.from_dict(lineage["tangguo_flow_state"])
    assert saved.phase == "top_tabs"
    assert saved.stage_results["home_video"] == "blocked"


def test_bottom_preexecution_rejection_never_counts_as_clicked():
    state = TangguoFlowState(phase="bottom_tabs", discovered_bottom_tabs=["推荐", "我的"],
                            tab_results={"推荐": {"clicked": True, "awaiting_verify": True}})
    lineage = {"tangguo_flow_state": state.to_dict()}
    params = {"tangguo_flow_phase": "bottom_tabs", "tangguo_flow_tab": "推荐"}
    reconcile_unexecuted_flow_action(lineage, params)
    assert lineage["tangguo_flow_state"]["tab_results"]["推荐"]["clicked"] is False
    reconcile_unexecuted_flow_action(lineage, params)
    saved = TangguoFlowState.from_dict(lineage["tangguo_flow_state"])
    assert saved.blocked_bottom_tabs == ["推荐"]
    assert saved.pending_bottom_tabs == ["我的"]


@pytest.mark.parametrize("label,page", [
    ("首页", "home"), ("剧库", "content_list"), ("我的", "profile"),
])
def test_page_type_alone_does_not_prove_tab_selected(label, page):
    work = work_for(page_type=page, candidates=(candidate(label, bottom_navigation_tab_seek=True),))
    assert not tab_destination_confirmed(work, label)


def test_blocked_flow_has_business_outcome_for_evidence_closure():
    state = TangguoFlowState(phase="done", stop_reason="video_target_revalidation_exhausted")
    terminal = flow_passive_decision(work_for(), state)
    assert terminal.outcome == "evidence_insufficient"
    assert terminal.reason_code == state.stop_reason
    assert _business_outcome(terminal).value == "evidence_insufficient"


@pytest.mark.parametrize("code,retry_expected", [
    ("POST_SCROLL_REBIND_REQUIRED", True),
    ("STALE_VISUAL_CANDIDATE", True),
    ("APP_CANDIDATE_REVALIDATION_REQUIRED", True),
    ("DRIVER_TIMEOUT", False),
])
def test_real_successor_lineage_reconciles_only_preexecution_rejection(code, retry_expected):
    from services.api.app.application.app_screenshot_exploration import _surface_phase_lineage
    from services.api.app.application.app_three_contract_driver import decide_app_three_contracts
    from services.api.app.domain.interaction import InteractionResultStatus

    selected = candidate("立即看", expected_state_hint="video_detail")
    work = work_for(candidates=(selected,))
    decision = decide_app_three_contracts(work)
    turn = SimpleNamespace(decision=decision, coordination=SimpleNamespace(
        interaction=SimpleNamespace(status=InteractionResultStatus.FAILED,
                                    retryable=True, error_code=code),
    ))
    lineage = _surface_phase_lineage(
        prior_work=work, selected=selected, turn=turn, next_analysis=None,
    )
    saved = TangguoFlowState.from_dict(lineage["tangguo_flow_state"])
    assert saved.phase == ("video_entry" if retry_expected else "video_verify")
    assert saved.video_attempts == (0 if retry_expected else 1)
