"""真实 128/204 派生页型漂移与跨页、跨层反例。"""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_targeted_recapture import (
    H5StateEquivalenceGate,
    H5TargetedRecaptureContext,
    H5TargetedRecapturePlanService,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    StateInvariantSnapshot,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5StateInvariantReader,
)
from services.api.tests.test_h5_targeted_recapture import NOW
from services.api.tests.test_v2_control_plane_repositories import FakeConnection, FakePool

CASES = json.loads((Path(__file__).parent / "fixtures/h5_chain_20261012_170654.json").read_text())


@pytest.mark.parametrize("station", ["jha128", "jha204"])
@pytest.mark.parametrize(
    "variation",
    [
        "measured",
        "no_proof",
        "other_identity",
        "other_url",
        "account",
        "remaining_layer",
        "exact",
        "media_type",
        "payment_type",
        "different_document",
        "different_state_or_transition",
        "unconfirmed",
        "external",
    ],
)
async def test_derived_type_drift_requires_independent_business_proof(station, variation):
    case = CASES[station]
    popup = station == "jha128"
    request = build_evidence_request(
        work_item_id="work",
        attempt_id="attempt",
        task_id="task-h5",
        rule_id="rule",
        rule_code="base_page_text",
        target_capture_id="original",
        reason_code="EVIDENCE_INSUFFICIENT",
        evidence_goal="补采同一业务正文",
        required_artifact_types=("screenshot", "dom"),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=(
            EvidenceRequiredEquivalence.ORIGINAL_ATOMIC
            if variation == "exact"
            else EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
        ),
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"account": "guest"},
            "business": {
                "page_type": case["source_page_type"],
                "url_pattern": "https://site.test/",
            },
            "structure": {"layer_target": "base_page"},
        },
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(minutes=10),
    )
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(
                return_value=H5TargetedRecaptureContext(
                    task_id="task-h5",
                    agent_run_id="run-h5",
                    finalization_epoch=4,
                    session_id="h5:run-h5",
                    current_capture_id="original",
                )
            )
        ),
        clock=lambda: NOW,
    ).resolve(request=request, agent_run_id="run-h5", finalization_epoch=4)
    if variation == "exact" and resolution.plan is None:
        return
    assert resolution.plan is not None
    original = StateInvariantSnapshot(
        task_id="task-h5",
        capture_id="original",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        environment={"surface_kind": "web", "account": "guest"},
        business={
            "page_type": case["source_page_type"],
            "url_pattern": "https://site.test/",
            "logical_page_key": case["logical_page_key"],
        },
        semantic={"page_state": "popup" if popup else "stable"},
        structure={
            "layer_target": "overlay" if popup else "base_page",
            "identity_status": "confirmed",
            "is_external": False,
            "document_identity": case.get(
                "source_document_identity", case.get("document_identity")
            ),
            "logical_state_fingerprint": case.get(
                "source_state_fingerprint", case.get("state_fingerprint")
            ),
        },
        captured_at=NOW,
    )
    business = {**original.business, "page_type": case["restored_page_type"]}
    structure = {**original.structure, "layer_target": "base_page"}
    if popup:
        structure.update(
            document_identity=case["restored_document_identity"],
            logical_transition_proof="trusted",
            logical_state_fingerprint=case["restored_state_fingerprint"],
            transition_reason="trusted_obstruction_clearance",
            transition_before_identity=case["logical_page_key"].removeprefix("logical:"),
            transition_after_identity=case["logical_page_key"].removeprefix("logical:"),
            transition_before_document=case["restored_document_identity"],
            transition_after_document=case["restored_document_identity"],
            obstruction_count="0",
            loading=False,
            evidence_quality="high",
        )
    if variation == "no_proof":
        structure = {
            key: value
            for key, value in structure.items()
            if key in {"layer_target", "identity_status", "is_external"}
        }
    elif variation == "other_identity":
        business["logical_page_key"] = "other"
    elif variation == "other_url":
        business["url_pattern"] = "https://site.test/other"
    elif variation == "remaining_layer":
        structure["layer_target"] = "overlay"
    elif variation == "media_type":
        business["page_type"] = "video_detail"
    elif variation == "payment_type":
        business["page_type"] = "payment"
    elif variation == "different_document":
        structure["document_identity"] = "different-document"
    elif variation == "different_state_or_transition":
        if popup:
            structure["transition_reason"] = "material_rotation"
        else:
            structure["logical_state_fingerprint"] = "different-state"
    elif variation == "unconfirmed":
        structure["identity_status"] = "unknown"
    elif variation == "external":
        structure["is_external"] = True
    restored = replace(
        original,
        capture_id="restored",
        business=business,
        structure=structure,
        environment={**original.environment, "account": "member"}
        if variation == "account"
        else original.environment,
        semantic={"page_state": "stable"},
        captured_at=NOW + timedelta(seconds=1),
    )
    frozen = request.to_dict()
    result = H5StateEquivalenceGate().assess(
        request=request,
        plan=resolution.plan,
        restoration_attempt_id="restore",
        original=original,
        restored=restored,
    )
    assert result.passed is (variation == "measured")
    assert request.to_dict() == frozen
    assert restored.business["page_type"] == business["page_type"]
    if variation == "measured":
        assert result.evaluator_version == "alioth-h5-state-equivalence.v3"
        assert any(item.key.startswith("derived_page_type:") for item in result.comparisons)


@pytest.mark.parametrize("station", ["jha128", "jha204"])
async def test_persisted_reader_keeps_independent_proof_not_graph_classifier_hash(station):
    case = CASES[station]
    identity = case["logical_page_key"].removeprefix("logical:")
    document = case.get("restored_document_identity", case.get("document_identity"))
    state = case.get("restored_state_fingerprint", case.get("state_fingerprint"))
    surface = {
        "document_identity": document,
        "state_fingerprint": state,
        "evidence_quality": "high",
        "state_signals": {"loading": False, "obstruction_count": "0"},
        "transition": {
            "logical_transition_proof": "trusted",
            "reason_code": "trusted_obstruction_clearance",
            "before_identity_fingerprint": identity,
            "after_identity_fingerprint": identity,
            "before_document_identity": document,
            "after_document_identity": document,
        },
    }
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": "task-h5",
                "agent_run_id": "run-h5",
                "session_id": "h5:run-h5",
                "metadata": {},
                "captured_at": NOW,
                "url": "https://site.test/",
                "page_type": case["restored_page_type"],
                "page_state": "stable",
                "logical_page_key": case["logical_page_key"],
                "state_fingerprint": "graph-classifier-dependent",
                "is_overlay": False,
                "is_external": False,
                "state_payload": json.dumps(
                    {"identity_status": "confirmed", "logical_surface": surface}
                ),
            }
        ]
    )
    snapshot = await PostgresH5StateInvariantReader(pool=FakePool(connection)).read(
        capture=SimpleNamespace(
            capture_id="restored",
            task_id="task-h5",
            session_id="h5:run-h5",
            surface_kind=SurfaceKind.WEB,
        )
    )
    assert snapshot.structure["logical_state_fingerprint"] == state
    assert snapshot.structure["state_fingerprint"] == "graph-classifier-dependent"
    assert snapshot.structure["transition_before_identity"] == identity
    assert snapshot.structure["obstruction_count"] == "0"


@pytest.mark.parametrize("station", ["jha128", "jha204"])
@pytest.mark.parametrize(
    "variation", ["measured", "other_identity", "no_proof", "video_detail", "payment"]
)
def test_main_frontier_uses_same_proof_but_never_upgrades_media_or_payment(station, variation):
    from services.api.app.application.h5_exploration_recovery import (
        h5_frontier_type_drift_is_proven,
    )

    case = CASES[station]
    identity = case["logical_page_key"].removeprefix("logical:")
    source = {
        "source_page_type": case["source_page_type"],
        "source_page_state": "popup" if station == "jha128" else "stable",
        "source_identity_fingerprint": identity,
        "source_document_identity": case.get("document_identity"),
        "source_state_fingerprint": case.get("state_fingerprint"),
    }
    metadata = {
        "detection_status": "recognized",
        "logical_surface_identity_fingerprint": identity,
        "logical_surface_document_identity": case.get("document_identity"),
        "logical_surface_state_fingerprint": case.get("state_fingerprint"),
        "logical_clearance_proven": True,
        "logical_transition_before_identity": identity,
        "logical_transition_after_identity": identity,
    }
    if variation == "other_identity":
        metadata["logical_surface_identity_fingerprint"] = "other"
    elif variation == "no_proof":
        metadata["logical_clearance_proven"] = False
        metadata["logical_surface_document_identity"] = None
    frontier = SimpleNamespace(
        observation=SimpleNamespace(
            page_type=variation
            if variation in {"video_detail", "payment"}
            else case["restored_page_type"],
            page_state="stable",
            location="https://site.test/",
            metadata=metadata,
        )
    )
    entry = SimpleNamespace(
        candidate=SimpleNamespace(metadata=source), source_location="https://site.test/"
    )
    assert h5_frontier_type_drift_is_proven(entry=entry, frontier=frontier) is (
        variation == "measured"
    )
