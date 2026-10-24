"""232739 实测导航签名与覆盖饥饿；反例不得放宽风险或弹窗优先级。"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.api.app.application.h5_auth_page_analyzer import H5AuthPageAnalyzer
from services.api.app.application.h5_exploration_recovery import (
    h5_global_bottom_navigation_index,
)
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.exploration.h5_frontier import (
    H5TaskFrontierSnapshot,
    select_next_h5_entry,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.tests.test_h5_auth_page_analyzer import _detection, _snapshot
from services.api.tests.test_h5_exploration_turn import _turn_module, _work

CASES = json.loads(
    (Path(__file__).parent / "fixtures/h5_batch_20261011_232739.json").read_text()
)


@pytest.mark.parametrize("signature", ["measured", None, "", "a" * 63, "A" * 64, 7])
def test_yc003_recovery_signature_is_preserved_or_safely_rejected(signature):
    candidate = CandidateAction.from_dict(CASES["yc003"]["candidate"])
    parameters = dict(candidate.metadata["action_parameters"])
    if signature != "measured":
        parameters["interaction_pattern_signature"] = signature
    candidate = replace(candidate, metadata={
        **candidate.metadata, "action_parameters": parameters,
    })
    assert h5_global_bottom_navigation_index(candidate) == (
        parameters["navigation_index"] if signature == "measured" else None
    )


@pytest.mark.parametrize("signature", [None, "", "a" * 63, "A" * 64, 7])
def test_yc003_execution_rejects_bad_signature_without_raising(signature):
    candidate = CandidateAction.from_dict(CASES["yc003"]["candidate"])
    candidate = replace(candidate, metadata={
        **candidate.metadata, "action_parameters": {
            **candidate.metadata["action_parameters"], "interaction_pattern_signature": signature,
        },
    })
    work = _work()
    entry = SimpleNamespace(candidate=candidate, source_location=work.frontier.observation.location)
    assert not _turn_module()._can_execute_global_bottom_navigation(work=work, entry=entry)


@pytest.mark.parametrize("field,value", [
    ("navigation_family", {}), ("navigation_index", True),
    ("navigation_index", 0), ("navigation_label", ""),
])
def test_yc003_malformed_capability_never_becomes_global_navigation(field, value):
    candidate = CandidateAction.from_dict(CASES["yc003"]["candidate"])
    candidate = replace(candidate, metadata={
        **candidate.metadata, "action_parameters": {
            **candidate.metadata["action_parameters"], field: value,
        },
    })
    assert h5_global_bottom_navigation_index(candidate) is None


@pytest.mark.parametrize("signature", ["measured", None, "bad", 7])
def test_yc003_profile_projection_keeps_signature_without_manufacturing_authority(signature):
    candidate = CASES["yc003"]["candidate"]
    action = {
        "action": "click", "target": candidate["semantic_target"],
        "risk_level": "low", "navigation_depth": 1,
        **candidate["metadata"],
    }
    action["interaction_pattern_signature"] = (
        candidate["metadata"]["interaction_pattern_signature"]
        if signature == "measured" else signature
    )
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="home", actions=(action,)),
        ui_snapshot=_snapshot(),
    )
    if signature == "measured":
        assert dict(observation.transition_parameters) == candidate["metadata"]["action_parameters"]
    else:
        assert not observation.transition_parameters


def _coverage_snapshot(profile, local):
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    snapshot = snapshot.merge(
        source_capture_id="historic-capture", source_state_version="historic-state",
        source_location="https://example.test/home", navigation_depth=0,
        candidates=(profile,),
    )
    return snapshot.merge(
        source_capture_id="current-capture", source_state_version="current-state",
        source_location="https://example.test/list", navigation_depth=0,
        candidates=(local,),
    )


def test_031_unvisited_global_profile_not_starved_by_local_content_sampling():
    profile = CandidateAction.from_dict(CASES["jha031"]["profile"])
    local = CandidateAction.from_dict(CASES["jha031"]["local_detail"])
    snapshot = _coverage_snapshot(profile, local)
    selected = select_next_h5_entry(snapshot, active_state_version="current-state")
    assert selected.candidate.candidate_id == profile.candidate_id
    assert "profile" not in snapshot.covered_page_types


@pytest.mark.parametrize("mode", [
    "popup", "payment", "video", "payment_entry", "video_entry", "medium", "forged",
    "old_popup", "covered",
])
def test_031_coverage_priority_never_overrides_current_evidence_or_risk(mode):
    profile = CandidateAction.from_dict(CASES["jha031"]["profile"])
    local = CandidateAction.from_dict(CASES["jha031"]["local_detail"])
    if mode == "popup":
        local = replace(local, navigation_depth=0, semantic_target="关闭当前弹窗", metadata={
            "target_keywords": ["popup_dismissal", "close_control"],
            "action_parameters": {"target_mode": "popup_dismissal"},
        })
    elif mode == "payment":
        local = replace(local, action_type=InteractionOperation.SCROLL, navigation_depth=0,
                        metadata={
            "source_page_type": "payment", "target_keywords": ["payment_horizontal_collection"],
            "action_parameters": {"target_mode": "h5_payment_horizontal_collection",
                                  "horizontal_scroll_max_segments": 6,
                                  "split_scroll_screenshots": True},
        })
    elif mode == "video":
        local = replace(local, action_type=InteractionOperation.OBSERVE, metadata={
            "source_page_type": "video_detail", "target_keywords": ["video_timed_capture"],
        })
    elif mode in {"payment_entry", "video_entry"}:
        local = replace(local, metadata={
            **local.metadata, "verified_business_link": True,
            "expected_next_page_type": "payment" if mode == "payment_entry" else "video_detail",
            "target_keywords": ["verified_business_link"],
        })
    elif mode == "medium":
        profile = replace(profile, risk_level=InteractionRiskLevel.MEDIUM)
    elif mode == "old_popup":
        profile = replace(profile, metadata={**profile.metadata, "source_page_state": "popup"})
    elif mode == "covered":
        pass
    else:
        profile = replace(profile, semantic_target="任意选择器")
    snapshot = _coverage_snapshot(profile, local)
    if mode == "covered":
        snapshot = snapshot.record_observed_page_type(
            source_capture_id="observed-profile-capture", source_state_version="profile-state",
            source_location="https://example.test/profile", navigation_depth=1,
            page_type="profile",
        )
    selected = select_next_h5_entry(snapshot, active_state_version="current-state")
    assert selected.candidate.candidate_id == local.candidate_id
