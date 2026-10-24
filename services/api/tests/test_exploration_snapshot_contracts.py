"""Frontier 访问账本和动作指纹的兼容合同。"""

from datetime import UTC, datetime

from services.api.app.domain.exploration import (
    CandidateAction,
    FrontierSnapshot,
    SurfaceObservation,
    build_action_fingerprint,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def _candidate(*, semantic_target: str = "进入详情") -> CandidateAction:
    return CandidateAction(
        candidate_id="candidate-1",
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"expected_state_hint": "detail"},
    )


def _observation() -> SurfaceObservation:
    return SurfaceObservation(
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        location="app://com.example/.MainActivity",
        state_version="state-1",
        observed_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        page_state="stable",
    )


def test_action_fingerprint_uses_state_action_and_semantic_target() -> None:
    first = build_action_fingerprint(
        state_version="state-1",
        candidate=_candidate(),
    )
    replay = build_action_fingerprint(
        state_version="state-1",
        candidate=_candidate(),
    )
    other_target = build_action_fingerprint(
        state_version="state-1",
        candidate=_candidate(semantic_target="进入订单"),
    )

    assert first == replay
    assert first != other_target


def test_legacy_frontier_v1_defaults_new_visit_ledger_fields() -> None:
    payload = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=_observation(),
        candidates=(_candidate(),),
    ).to_dict()
    payload.pop("visited_candidate_ids")
    payload.pop("visited_action_fingerprints")
    payload.pop("repeated_state_count")

    restored = FrontierSnapshot.from_dict(payload)

    assert restored.visited_candidate_ids == ()
    assert restored.visited_action_fingerprints == ()
    assert restored.repeated_state_count == 0


def test_frontier_serializes_visit_ledger_and_repeat_count() -> None:
    snapshot = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=_observation(),
        candidates=(_candidate(),),
        visited_candidate_ids=("candidate-old",),
        visited_action_fingerprints=("action-fingerprint:old",),
        repeated_state_count=2,
    )

    payload = snapshot.to_dict()

    assert payload["visited_candidate_ids"] == ["candidate-old"]
    assert payload["visited_action_fingerprints"] == ["action-fingerprint:old"]
    assert payload["repeated_state_count"] == 2
