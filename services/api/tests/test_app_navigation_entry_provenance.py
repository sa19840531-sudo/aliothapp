"""App 导航入口发现 provenance 回归。"""

from __future__ import annotations

from types import SimpleNamespace

from services.api.app.application.app_coverage_page_seek import (
    append_coverage_page_seek_candidate,
    resolve_coverage_page_seek,
)
from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    DISCOVERY_SOURCE_PROMOTED_COVERAGE,
    DISCOVERY_SOURCE_UIA_ALIGNED,
    DISCOVERY_SOURCE_VISION,
    is_business_navigation_entry,
    normalize_discovery_source,
    tag_candidate_action_provenance,
    tag_navigation_entry_provenance,
    with_visual_candidate_discovery_source,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    DEFAULT_STARTUP_AD_SKIP_BOUNDS,
    startup_ad_skip_contract,
)
from services.api.app.domain.exploration import (
    AppVisualCandidate,
    CandidateAction,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

_SIG = "sha256:" + ("ab" * 32)


def _visual(
    *,
    capture_id: str = "cap-1",
    semantic: str = "内容列表",
    discovery_source: str | None = DISCOVERY_SOURCE_VISION,
    frontier_scope: str = "page",
    expected: str | None = "content_list",
    bounds: NormalizedBounds | None = None,
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=capture_id,
        source_image_signature=_SIG,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name=semantic,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds or NormalizedBounds(0.1, 0.4, 0.9, 0.55),
        confidence=0.9,
        risk_level=InteractionRiskLevel.LOW,
        purpose=f"进入{semantic}",
        expected_state_hint=expected,
        frontier_scope=frontier_scope,
        discovery_source=discovery_source,
    )


def test_normalize_and_business_gate() -> None:
    assert normalize_discovery_source("VISION") == DISCOVERY_SOURCE_VISION
    assert normalize_discovery_source("nope") is None
    assert is_business_navigation_entry(
        source_capture_id="c1",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    assert not is_business_navigation_entry(
        source_capture_id="c1",
        discovery_source=DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    )
    assert not is_business_navigation_entry(
        source_capture_id="",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def test_tag_provenance_and_visual_candidate() -> None:
    tagged = tag_navigation_entry_provenance(
        discovery_source=DISCOVERY_SOURCE_VISION,
        source_capture_id="cap-9",
        screenshot_artifact_id="art-9",
    )
    assert tagged["discovery_source"] == DISCOVERY_SOURCE_VISION
    assert tagged["source_capture_id"] == "cap-9"
    assert tagged["business_navigation_entry"] is True

    meta = tag_candidate_action_provenance(
        {"purpose": "x"},
        discovery_source=DISCOVERY_SOURCE_UIA_ALIGNED,
        source_capture_id="cap-2",
    )
    assert meta["discovery_source"] == DISCOVERY_SOURCE_UIA_ALIGNED
    assert meta["source_capture_id"] == "cap-2"

    visual = _visual(discovery_source=None)
    aligned = with_visual_candidate_discovery_source(visual, DISCOVERY_SOURCE_UIA_ALIGNED)
    assert aligned.discovery_source == DISCOVERY_SOURCE_UIA_ALIGNED
    assert aligned.candidate_id == visual.candidate_id


def test_startup_skip_default_is_clearance_only() -> None:
    contract = startup_ad_skip_contract(
        bounds=DEFAULT_STARTUP_AD_SKIP_BOUNDS,
        purpose="跳过",
        used_default_fixed_bounds=True,
    )
    assert contract["discovery_source"] == DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC
    assert contract["clearance_only"] is True
    assert contract["business_navigation_entry"] is False
    assert contract["requires_fresh_capture_after_action"] is True
    assert contract["cross_capture_coordinate_replay_forbidden"] is True


def test_coverage_promote_tags_promoted_coverage() -> None:
    visual = _visual()
    candidate = CandidateAction(
        candidate_id=visual.candidate_id,
        semantic_target=visual.semantic_name,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata=tag_candidate_action_provenance(
            {
                "source_capture_id": visual.source_capture_id,
                "expected_state_hint": "content_list",
                "expected_next_page_type": "content_list",
                "purpose": visual.purpose,
            },
            discovery_source=DISCOVERY_SOURCE_VISION,
            source_capture_id=visual.source_capture_id,
        ),
    )
    analysis = SimpleNamespace(
        capture_id="cap-1",
        package_name="com.example.app",
        page_state="stable",
        visual_page_type="home",
        overlay_signature=None,
        surface_facts={},
        candidates=(visual,),
    )
    out = append_coverage_page_seek_candidate(
        candidates=(candidate,),
        analysis=analysis,  # type: ignore[arg-type]
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.app",),
        coverage_metadata={
            "required_page_types": ["home", "content_list"],
            "covered_page_types": ["home"],
        },
    )
    assert len(out) == 1
    assert out[0].metadata.get("discovery_source") == DISCOVERY_SOURCE_PROMOTED_COVERAGE
    assert out[0].metadata.get("source_capture_id") == "cap-1"
    assert out[0].metadata.get("coverage_page_seek") is True


def test_legacy_coverage_page_seek_contract_still_rejected() -> None:
    analysis = SimpleNamespace()
    assert (
        resolve_coverage_page_seek(
            analysis=analysis,  # type: ignore[arg-type]
            missing=["home"],
            covered=set(),
        )
        is None
    )


def test_clearance_cannot_be_promoted_as_business_nav() -> None:
    assert not is_business_navigation_entry(
        source_capture_id="cap",
        discovery_source=DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    )
    meta = tag_candidate_action_provenance(
        {},
        discovery_source=DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
        source_capture_id="cap",
        requires_fresh_capture_after_action=True,
    )
    assert meta["clearance_only"] is True
    assert meta["business_navigation_entry"] is False
