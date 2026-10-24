"""聚合封存前的展示层门禁：被遮挡或诊断材料不进入自然证据成员。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from services.api.app.application.aggregate_inspection_publication import (
    AggregateEvidenceSource,
    AggregateRuleSource,
    _select_rule_evidence,
)
from services.api.app.application.aggregate_rule_config import RuleEvidenceInputConfiguration
from services.api.app.domain.evidence.input_set import EvidenceInputScope
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_PROTOCOL_VERSION,
    BasePageVisibility,
    BlockingScope,
    DiagnosticOperation,
    PresentationAssessment,
    TopmostLayerKind,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
    _evidence_from_row,
)
from services.api.tests.test_aggregate_inspection_publication_repository import (
    FakeConnection,
    FakePool,
)

_VISIBLE = PresentationAssessment(
    topmost_layer_kind=TopmostLayerKind.PAGE,
    base_page_visibility=BasePageVisibility.VISIBLE,
    blocking_scope=BlockingScope.NONE,
)
_OCCLUDED = PresentationAssessment(
    topmost_layer_kind=TopmostLayerKind.OVERLAY,
    base_page_visibility=BasePageVisibility.FULLY_OCCLUDED,
    blocking_scope=BlockingScope.PAGE,
    overlay_signature="ad-1",
)
_DIAGNOSTIC = _VISIBLE.with_diagnostic_operations(
    (DiagnosticOperation(kind="js_click_dispatch", source="fallback_dispatch_attempts"),)
)


def _rule(*, category: str = "visual", judge_config: dict | None = None) -> AggregateRuleSource:
    return AggregateRuleSource(
        rule_id="rule-1",
        rule_code="alioth_spec_48",
        rule_payload={
            "category": category,
            "name": "Toast样式统一性检查",
            "check_point": "Toast 提示应全产品统一",
            "judge_config": judge_config or {},
        },
        applicable_page_types=("home", "video_list"),
        evidence=RuleEvidenceInputConfiguration(scope=EvidenceInputScope.TASK, scope_key="task"),
    )


def _evidence(page_type: str, presentation: PresentationAssessment | None, seq: int = 1):
    return AggregateEvidenceSource(
        candidate=SimpleNamespace(
            manifest=SimpleNamespace(capture_id=f"capture-{seq}", manifest_hash=f"hash-{seq}"),
            page_sequence=seq,
            journey_execution_id=None,
            environment_profile_id=None,
        ),
        page_type=page_type,
        presentation=presentation,
    )


def test_occluded_and_diagnostic_members_are_dropped_before_sealing() -> None:
    evidence = (
        _evidence("home", _OCCLUDED, 1),
        _evidence("home", _VISIBLE, 2),
        _evidence("video_list", _DIAGNOSTIC, 3),
        _evidence("video_list", None, 4),
    )

    selected, disposition = _select_rule_evidence(evidence, rule=_rule())

    assert disposition is None
    assert [item.candidate.page_sequence for item in selected] == [2, 4]


def test_all_members_rejected_yields_evidence_insufficient_not_not_applicable() -> None:
    """全部成员被拒只说明证据不足，不能改写规则适用性（09-11 审查 P0-03）。

    证据不足是终态：仓储同一事务写证据不足的规则执行与待复核项，收口按复核策略放行。
    """
    evidence = (_evidence("home", _OCCLUDED, 1), _evidence("home", _DIAGNOSTIC, 2))

    selected, disposition = _select_rule_evidence(evidence, rule=_rule())

    assert selected == ()
    assert disposition is not None
    status, code, message = disposition
    assert status == "evidence_insufficient"
    assert code == "AGGREGATE_PRESENTATION_EVIDENCE_REJECTED"
    assert "被弹窗挡住" in message and "人工确认" in message


def test_ad_rules_and_declared_targets_keep_overlay_frames() -> None:
    evidence = (_evidence("home", _OCCLUDED, 1),)

    selected, disposition = _select_rule_evidence(evidence, rule=_rule(category="ad"))
    assert disposition is None and len(selected) == 1

    selected, disposition = _select_rule_evidence(
        evidence, rule=_rule(judge_config={"evidence_layer_target": "any"})
    )
    assert disposition is None and len(selected) == 1

    # 诊断材料只有规则显式接受时才能进入
    selected, _ = _select_rule_evidence((_evidence("home", _DIAGNOSTIC, 1),), rule=_rule())
    assert selected == ()
    selected, _ = _select_rule_evidence(
        (_evidence("home", _DIAGNOSTIC, 1),),
        rule=_rule(judge_config={"accepts_diagnostic_evidence": True}),
    )
    assert len(selected) == 1


def _manifest_payload() -> str:
    from datetime import UTC, datetime

    from services.api.app.domain.evidence import (
        ArtifactReference,
        EvidenceQualityStatus,
        build_evidence_manifest_reference,
    )

    manifest = build_evidence_manifest_reference(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="h5:session-1",
        capture_id="44444444-4444-4444-4444-444444444444",
        manifest_version=2,
        artifacts=(
            ArtifactReference(
                artifact_id="22222222-2222-2222-2222-222222222222",
                artifact_type="screenshot",
                role="after_action",
                storage_path="task/capture/shot.png",
                checksum="sha256:shot",
                size_bytes=3,
                content_type="image/png",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="evidence_requirements_satisfied",
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    return json.dumps(manifest.to_dict())


def _row(**overrides):
    row = {
        "capture_id": "44444444-4444-4444-4444-444444444444",
        "capture_role": "after_action",
        "surface_kind": "web",
        "metadata": json.dumps({"presentation": _VISIBLE.to_dict()}),
        "page_state": "stable",
        "visual_page_state": None,
        "visual_overlay_signature": None,
        "visual_surface_facts": None,
        "manifest_payload": _manifest_payload(),
        "page_sequence": 1,
        "page_type": "home",
        "journey_execution_id": None,
        "environment_profile_id": None,
    }
    row.update(overrides)
    return row


def test_h5_row_presentation_is_read_from_metadata_and_reconciled() -> None:
    assert _evidence_from_row(_row()).presentation == _VISIBLE

    popup = _evidence_from_row(_row(page_state="popup")).presentation
    assert popup is not None
    assert popup.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert popup.base_page_visibility is BasePageVisibility.UNKNOWN

    assert _evidence_from_row(_row(metadata=json.dumps({}))).presentation is None


def test_app_row_presentation_is_derived_from_visual_observation() -> None:
    row = _row(
        surface_kind="android",
        metadata=json.dumps({}),
        page_state=None,
        visual_page_state="popup",
        visual_overlay_signature="sig-app",
        visual_surface_facts=json.dumps({"popup_visible": True}),
    )
    presentation = _evidence_from_row(row).presentation
    assert presentation is not None
    assert presentation.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert presentation.overlay_signature == "sig-app"
    assert presentation.protocol_version == PRESENTATION_PROTOCOL_VERSION

    bare = _row(surface_kind="android", metadata=json.dumps({}), page_state=None)
    assert _evidence_from_row(bare).presentation is None


async def test_snapshot_sql_projects_layer_facts_for_both_platforms() -> None:
    connection = FakeConnection()
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    await repository.load_snapshot(
        task_id="11111111-1111-1111-1111-111111111111",
        source_agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
    )

    query = next(sql for sql, _ in connection.queries if "current_app_entry AS" in sql)
    assert "visual.page_state AS visual_page_state" in query
    assert "visual.overlay_signature AS visual_overlay_signature" in query
    assert "visual.surface_facts AS visual_surface_facts" in query
    assert "capture.surface_kind, capture.metadata, page.page_state" in query
