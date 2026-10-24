"""整链审查中页面身份与遮挡事实的跨边界回归。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_exploration_recovery import _stable_dynamic_same_page_matches


@pytest.mark.parametrize("kind,status,blocking,expected_overlay", [
    ("none", "ready", False, False),
    ("nonblocking_notification", "ready", False, False),
    ("none", "incomplete", False, True),
    ("unknown", "ready", False, True),
    ("blocking_overlay", "ready", True, True),
])
def test_frontier_uses_complete_obstruction_facts_without_rewriting_detection(
    kind, status, blocking, expected_overlay,
):
    detection = {
        "page_capture_id": "capture", "page_url": "https://product.test/",
        "detection_status": "recognized", "page_type": "home", "page_state": "popup",
        "confidence": 0.9, "candidate_actions": [], "rule_matching_allowed": False,
        "obstruction_facts": {
            "schema_version": "h5-obstruction-fact.v1", "kind": kind,
            "snapshot_evidence_status": status, "blocking": blocking,
            "modal_census_complete": True, "modal_census_count": 0,
        },
    }
    frontier = build_h5_frontier(
        detection_result=detection, observed_at=datetime.now(UTC), navigation_depth=0,
    )
    assert bool(frontier.observation.overlay_signature) is expected_overlay
    assert detection["page_state"] == "popup"
    assert detection["rule_matching_allowed"] is False
    assert frontier.observation.metadata["detected_page_state"] == "popup"


@pytest.mark.parametrize("expected,current,accepted", [
    ("home", "home", True), ("home", "category", False),
    ("home", None, False), (None, "home", False), (None, None, True),
])
def test_dynamic_restore_does_not_exchange_same_url_same_type_logical_pages(
    expected, current, accepted,
):
    entry = SimpleNamespace(
        source_location="https://product.test/",
        candidate=SimpleNamespace(metadata={
            "source_page_type": "content_list", "source_identity_fingerprint": expected,
        }),
    )
    frontier = SimpleNamespace(observation=SimpleNamespace(
        location="https://product.test/", page_type="content_list", page_state="stable",
        overlay_signature=None, metadata={
            "detection_status": "recognized", "logical_surface_identity_fingerprint": current,
        },
    ))
    assert _stable_dynamic_same_page_matches(
        entry=entry, route="back", frontier=frontier,
    ) is accepted


@pytest.mark.parametrize("census", [
    {}, {"modal_census_complete": False, "modal_census_count": 0},
    {"modal_census_complete": True, "modal_census_count": 1},
    {"modal_census_complete": True, "modal_census_count": None},
])
def test_incomplete_or_nonempty_modal_census_does_not_clear_model_overlay(census):
    frontier = build_h5_frontier(
        detection_result={
            "page_capture_id": "capture", "page_url": "https://product.test/",
            "detection_status": "recognized", "page_type": "home", "page_state": "popup",
            "confidence": 0.9, "candidate_actions": [],
            "obstruction_facts": {
                "schema_version": "h5-obstruction-fact.v1", "kind": "none",
                "snapshot_evidence_status": "ready", "blocking": False, **census,
            },
        },
        observed_at=datetime.now(UTC), navigation_depth=0,
    )
    assert frontier.observation.overlay_signature
