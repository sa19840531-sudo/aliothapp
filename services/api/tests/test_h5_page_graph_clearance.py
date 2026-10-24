"""H5 清障状态变化的图证明不能失真或伪造业务到达。"""

from copy import deepcopy

import pytest

from services.api.app.domain.exploration.h5_logical_surface import (
    build_h5_logical_surface_contract,
    compare_h5_logical_surfaces,
)
from services.api.app.domain.exploration.page_graph import (
    build_page_state_snapshot,
    build_transition_decision,
)


def _surface(popup):
    return build_h5_logical_surface_contract(
        render_profile={"kind": "dom_document", "status": "confirmed"},
        document_identity_material=["same-document"],
        identity_signals={"main_structure": "popup-layout" if popup else "page-layout"},
        state_signals={"obstruction_count": "1" if popup else "0", "loading": False},
        evidence_sources=["dom"], evidence_quality="high", degraded_reasons=[],
    )


@pytest.mark.parametrize(
    "fault", [None, "web", "forged_state", "still_blocked", "document_changed"],
)
def test_h5_clearance_graph_revalidates_captured_surfaces(fault):
    before, after = _surface(True), _surface(False)
    transition = compare_h5_logical_surfaces(before=before, after=after)
    assert transition["reason_code"] == "trusted_obstruction_clearance"
    assert before["identity_fingerprint"] != after["identity_fingerprint"]
    if fault == "forged_state":
        transition["after_state_fingerprint"] = "sha256:" + "f" * 64
    if fault == "still_blocked":
        after = deepcopy(after)
        after["state_signals"]["obstruction_count"] = "1"
    if fault == "document_changed":
        after = deepcopy(after)
        after["document_identity"] = "sha256:" + "f" * 64
    common = dict(task_id="task", agent_run_id="run", surface_kind="web",
                  session_id="web:run" if fault == "web" else "h5:run",
                  finalization_epoch=0)
    source = build_page_state_snapshot(
        capture_id="before", command_sequence=1, capture_role="before_action",
        page_state="loaded", metadata={"logical_surface": before}, **common,
    )
    target = build_page_state_snapshot(
        capture_id="after", command_sequence=2, capture_role="after_action",
        page_state="stable", metadata={"logical_surface": after}, **common,
    )
    if fault != "web":
        assert source.is_overlay is True
        assert source.counts_as_business_page is False
    decision = build_transition_decision(
        action_status="completed", source=source, target=target,
        result_payload={"logical_surface_transition": transition},
    )
    assert decision.proof == ("trusted" if fault is None else "insufficient")
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False
