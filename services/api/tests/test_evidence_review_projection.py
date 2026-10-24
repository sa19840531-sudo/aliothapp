"""通用只读投影不混淆版本、计划和实际动作时间。"""

from datetime import UTC, datetime

from services.api.app.application.evidence_review_projection import (
    execution_timeline,
    manifest_review,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)


def test_exact_manifest_keeps_historical_members_without_adopting_later_artifacts():
    artifact = ArtifactReference(
        artifact_id="a", artifact_type="dom", role="after_action", storage_path="owned/a",
        checksum="sha256:abc", size_bytes=4, content_type="text/html",
    )
    manifest = build_evidence_manifest_reference(
        task_id="task", session_id="session", capture_id="capture", manifest_version=1,
        artifacts=(artifact,), quality_status=EvidenceQualityStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    actual = [{**artifact.to_dict()}, {**artifact.to_dict(), "artifact_id": "later"}]
    projection = manifest_review(manifest, artifacts=actual, role="after_action",
                                 bound=manifest.manifest_id, latest="new")
    assert projection["review_only"] is True
    assert projection["is_latest"] is False
    assert projection["is_bound"] is True
    assert projection["integrity"] == "verified"
    assert [item["artifact_id"] for item in projection["artifacts"]] == ["a"]
    assert "storage_path" not in str(projection)
    actual[0]["size_bytes"] += 1
    denied = manifest_review(manifest, artifacts=actual, role="after_action", bound="", latest="")
    assert denied["artifacts"] == [] and denied["integrity"] == "artifact_mismatch"


def test_actual_timeline_orders_captures_and_deduplicates_action_with_stable_identity():
    bindings = [{
        "capture_id": f"capture-{index}", "action_execution_id": "action", "integrity": "current",
        "role": role, "captured_at": timestamp,
        "action_started_at": "2026-10-23T00:00:01Z",
        "action_finished_at": "2026-10-23T00:00:02Z",
    } for index, role, timestamp in (
        (1, "after_action", "2026-10-23T00:00:03Z"),
        (0, "before_action", "2026-10-23T00:00:00Z"),
    )]
    checkpoint = {"checkpoint_id": "planned-first", "attempts": [{
        "attempt_id": "attempt", "session_id": "s", "finalization_epoch": 0,
        "bindings": bindings,
    }]}
    events = execution_timeline([checkpoint])
    assert [item["event_type"] for item in events] == [
        "capture", "action_started", "action_finished", "capture",
    ]
    assert events[0]["capture_id"] == "capture-0"
    assert all(item["planned_order"] == 1 for item in events)
    assert len({item["event_id"] for item in events}) == 4
    bindings[0]["integrity"] = "binding_mismatch"
    assert len(execution_timeline([checkpoint])) == 3


def test_actual_timeline_normalizes_timezone_offsets_and_keeps_naive_time_unknown():
    bindings = [{
        "capture_id": capture_id, "action_execution_id": "action", "integrity": "current",
        "role": "after_action", "captured_at": timestamp,
    } for capture_id, timestamp in (
        ("later", "2026-10-23T01:00:00+00:00"),
        ("earlier", "2026-10-23T08:00:00+08:00"),
        ("unknown", "2026-10-23T00:00:00"),
    )]
    events = execution_timeline([{"checkpoint_id": "c", "attempts": [{
        "attempt_id": "a", "session_id": "s", "finalization_epoch": 0, "bindings": bindings,
    }]}])
    captures = [item for item in events if item["event_type"] == "capture"]
    assert [item["capture_id"] for item in captures] == ["earlier", "later", "unknown"]
    assert captures[0]["occurred_at"] == "2026-10-23T00:00:00+00:00"
    assert captures[-1]["occurred_at"] is None
