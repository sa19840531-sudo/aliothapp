"""App 触控候选与可信普通动作控件冲突判定测试。"""

import pytest

from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_UIA_ISSUED,
    DISCOVERY_SOURCE_VISION,
)
from services.api.app.application.app_touch_target_conflicts import (
    dismiss_candidate_conflicts_with_trusted_action,
    filter_unproven_dismiss_candidates,
)
from services.api.app.domain.exploration import AppVisualCandidate, NormalizedBounds
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel

CAPTURE_ID = "capture-current"
SIGNATURE = "sha256:" + "a" * 64


def _candidate(
    *,
    semantic_name: str,
    bounds: NormalizedBounds,
    role: str | None,
    discovery_source: str,
    capture_id: str = CAPTURE_ID,
    source_image_signature: str = SIGNATURE,
    source_width: int = 1080,
    source_height: int = 1920,
    source_orientation: str = "portrait",
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=capture_id,
        source_image_signature=source_image_signature,
        source_width=source_width,
        source_height=source_height,
        source_orientation=source_orientation,
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.9,
        risk_level=InteractionRiskLevel.LOW,
        purpose=semantic_name,
        control_role=role,
        discovery_source=discovery_source,
    )


def test_conflicting_dismiss_is_filtered_without_current_close_proof() -> None:
    dismiss = _candidate(
        semantic_name="底部横幅关闭",
        bounds=NormalizedBounds(0.90, 0.88, 0.97, 0.91),
        role="dismiss",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    navigation = _candidate(
        semantic_name="底栏「我的」",
        bounds=NormalizedBounds(0.81, 0.87, 0.99, 0.93),
        role=None,
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )

    assert dismiss_candidate_conflicts_with_trusted_action(
        candidate=dismiss,
        trusted_action_controls=(navigation,),
    )
    assert filter_unproven_dismiss_candidates(
        candidates=(dismiss,),
        trusted_action_controls=(navigation,),
        current_dismiss_proven=False,
    ) == ()


def test_non_conflicting_vision_dismiss_is_kept() -> None:
    dismiss = _candidate(
        semantic_name="关闭弹层",
        bounds=NormalizedBounds(0.90, 0.10, 0.97, 0.16),
        role="dismiss",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    navigation = _candidate(
        semantic_name="底栏「我的」",
        bounds=NormalizedBounds(0.81, 0.87, 0.99, 0.93),
        role=None,
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )

    assert not dismiss_candidate_conflicts_with_trusted_action(
        candidate=dismiss,
        trusted_action_controls=(navigation,),
    )
    assert filter_unproven_dismiss_candidates(
        candidates=(dismiss,),
        trusted_action_controls=(navigation,),
        current_dismiss_proven=False,
    ) == (dismiss,)


def test_conflicting_dismiss_is_kept_with_current_close_proof() -> None:
    dismiss = _candidate(
        semantic_name="关闭广告弹窗",
        bounds=NormalizedBounds(0.90, 0.88, 0.97, 0.91),
        role="dismiss",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    navigation = _candidate(
        semantic_name="底栏「我的」",
        bounds=NormalizedBounds(0.81, 0.87, 0.99, 0.93),
        role=None,
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )

    assert filter_unproven_dismiss_candidates(
        candidates=(dismiss,),
        trusted_action_controls=(navigation,),
        current_dismiss_proven=True,
    ) == (dismiss,)


def test_different_capture_control_cannot_conflict() -> None:
    dismiss = _candidate(
        semantic_name="底部横幅关闭",
        bounds=NormalizedBounds(0.90, 0.88, 0.97, 0.91),
        role="dismiss",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    navigation = _candidate(
        semantic_name="底栏「我的」",
        bounds=NormalizedBounds(0.81, 0.87, 0.99, 0.93),
        role=None,
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
        capture_id="capture-older",
    )

    assert not dismiss_candidate_conflicts_with_trusted_action(
        candidate=dismiss,
        trusted_action_controls=(navigation,),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_image_signature", "sha256:" + "b" * 64),
        ("source_width", 1440),
        ("source_height", 3040),
        ("source_orientation", "landscape"),
    ],
)
def test_same_capture_id_with_different_fingerprint_cannot_conflict(
    field: str,
    value: object,
) -> None:
    dismiss = _candidate(
        semantic_name="底部横幅关闭",
        bounds=NormalizedBounds(0.90, 0.88, 0.97, 0.91),
        role="dismiss",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    navigation = _candidate(
        semantic_name="底栏「我的」",
        bounds=NormalizedBounds(0.81, 0.87, 0.99, 0.93),
        role=None,
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
        **{field: value},
    )

    assert not dismiss_candidate_conflicts_with_trusted_action(
        candidate=dismiss,
        trusted_action_controls=(navigation,),
    )
