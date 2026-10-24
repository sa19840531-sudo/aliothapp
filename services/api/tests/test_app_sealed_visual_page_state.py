"""封存交接的页面状态只取同帧、同 Manifest 的可信视觉事实。"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_sealed_inspection_publisher import (
    AppSealedInspectionPublishError,
)
from services.api.app.domain.evidence import (
    AppExplorationResultOutcome,
    build_evidence_manifest_reference,
    seal_app_evidence_index,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.tests.test_app_sealed_inspection_publisher import (
    NOW,
    _building_index,
    _publisher,
)


def _sealed_with_state(frozen_state):
    building = _building_index()
    entries = tuple(
        replace(
            entry,
            metadata={} if frozen_state is None else {"page_state": frozen_state},
        )
        for entry in building.entries
    )
    return seal_app_evidence_index(
        replace(building, entries=entries),
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        sealed_at=NOW + timedelta(minutes=1),
    )


def _visual_with_state(original, page_state, **changes):
    values = {
        "capture_id": original.capture_id,
        "screenshot_artifact_id": original.screenshot_artifact_id,
        "image_signature": original.image_signature,
        "width": original.width,
        "height": original.height,
        "orientation": original.orientation,
        "visual_page_type": original.visual_page_type,
        "visual_summary": original.visual_summary,
        "key_elements": original.key_elements,
        "candidates": original.candidates,
        "provider_id": original.provider_id,
        "model_id": original.model_id,
        "page_state": page_state,
    }
    return SimpleNamespace(analysis=AppVisualAnalysis.build(**(values | changes)))


@pytest.mark.parametrize(
    ("frozen_state", "visual_state"),
    [("unknown", "stable"), (None, "popup"), ("stable", "unknown")],
)
async def test_sealed_entry_uses_validated_visual_state_without_rewriting_index(
    frozen_state, visual_state,
):
    index = _sealed_with_state(frozen_state)
    frozen_entries = index.entries
    frozen_hash = index.content_sha256
    publisher, _, visuals, writer = _publisher(index)
    for capture_id, saved in tuple(visuals.values.items()):
        visuals.values[capture_id] = _visual_with_state(saved.analysis, visual_state)

    await publisher.publish(task_id="task-1", agent_run_id="run-1")
    await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert {entry.page_state for entry in writer.calls[0]["entries"]} == {visual_state}
    assert writer.calls[0] == writer.calls[1]
    assert index.entries == frozen_entries
    assert index.content_sha256 == frozen_hash == index.recompute_content_sha256()


@pytest.mark.parametrize("frozen_state", ["stable", "popup", None])
async def test_missing_visual_does_not_default_to_stable_or_use_frozen_metadata(frozen_state):
    index = _sealed_with_state(frozen_state)
    publisher, _, _, writer = _publisher(index, visual_capture_ids=())

    result = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert {entry.page_state for entry in writer.calls[0]["entries"]} == {"unknown"}
    assert all(entry.page_type is None for entry in writer.calls[0]["entries"])
    assert result.review_entry_count == len(index.entries)
    assert result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT


@pytest.mark.parametrize("mismatch", ["capture", "screenshot", "artifact_type"])
async def test_visual_state_requires_exact_capture_and_manifest_screenshot(mismatch):
    index = _sealed_with_state("stable")
    publisher, manifests, visuals, writer = _publisher(index)
    entry = index.entries[0]
    changes = {
        "capture": {"capture_id": "other-capture"},
        "screenshot": {"screenshot_artifact_id": "other-screenshot"},
        "artifact_type": {},
    }[mismatch]
    visuals.values[entry.capture_id] = _visual_with_state(
        visuals.values[entry.capture_id].analysis, "popup", **changes,
    )
    if mismatch == "artifact_type":
        original = manifests.values[entry.evidence_manifest_id]
        manifest = build_evidence_manifest_reference(
            task_id=original.task_id,
            session_id=original.session_id,
            capture_id=original.capture_id,
            manifest_version=original.manifest_version,
            artifacts=tuple(replace(item, artifact_type="uia_xml") for item in original.artifacts),
            quality_status=original.quality_status,
            quality_reason=original.quality_reason,
            created_at=original.created_at,
        )
        building = _building_index()
        index = seal_app_evidence_index(
            replace(
                building,
                entries=tuple(
                    replace(item, evidence_manifest_id=manifest.manifest_id)
                    if item.capture_id == entry.capture_id else item
                    for item in building.entries
                ),
            ),
            outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
            terminal_reason_code="exploration_completed",
            sealed_at=NOW + timedelta(minutes=1),
        )
        publisher, manifests, visuals, writer = _publisher(index)
        manifests.values[manifest.manifest_id] = manifest
        visuals.values[entry.capture_id] = _visual_with_state(
            visuals.values[entry.capture_id].analysis, "popup",
        )

    with pytest.raises(AppSealedInspectionPublishError) as captured:
        await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert captured.value.code == "APP_SEALED_VISUAL_FACT_INVALID"
    assert writer.calls == []
