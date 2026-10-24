"""证据的只读版本与实际时序投影，不参与规则候选选择或输入封存。"""

from datetime import UTC, datetime

from services.api.app.domain.evidence import EvidenceManifestReference


def manifest_review(manifest: EvidenceManifestReference, *, artifacts, role, bound, latest):
    """历史材料仅按同一 Capture 的精确原件事实开放审阅，不补齐当前输入。"""
    actual = {item["artifact_id"]: item for item in artifacts}
    valid = all(
        item.role == role
        and item.artifact_id in actual
        and all(
            actual[item.artifact_id][key] == getattr(item, key)
            for key in ("artifact_type", "checksum", "storage_path", "size_bytes", "content_type")
        )
        for item in manifest.artifacts
    )
    return {
        "manifest_id": manifest.manifest_id,
        "manifest_version": manifest.manifest_version,
        "manifest_hash": manifest.manifest_hash,
        "created_at": manifest.created_at.isoformat(),
        "quality_status": manifest.quality_status.value,
        "quality_reason": manifest.quality_reason,
        "is_bound": manifest.manifest_id == bound,
        "is_latest": manifest.manifest_id == latest,
        "review_only": True,
        "integrity": "verified" if valid else "artifact_mismatch",
        "artifacts": [
            {
                key: actual[item.artifact_id][key]
                for key in (
                    "artifact_id", "artifact_type", "checksum", "content_type", "size_bytes",
                )
            }
            for item in manifest.artifacts
        ] if valid else [],
    }


def execution_timeline(checkpoints):
    """实际时间缺失时保持未知；同一时刻以稳定身份排序，不冒充发生先后。"""
    events = {}
    for plan_order, checkpoint in enumerate(checkpoints, 1):
        for attempt in checkpoint["attempts"]:
            identity = {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "planned_order": plan_order,
                "attempt_id": attempt["attempt_id"],
                "session_id": attempt["session_id"],
                "finalization_epoch": attempt["finalization_epoch"],
            }
            for binding in attempt["bindings"]:
                if binding["integrity"] not in {"current", "manifest_changed"}:
                    continue
                capture_id, action_id = binding["capture_id"], binding["action_execution_id"]
                event_id = f"{attempt['attempt_id']}:capture:{capture_id}"
                events[event_id] = {
                    **identity, "event_id": event_id, "event_type": "capture",
                    "occurred_at": _review_time(binding["captured_at"]), "role": binding["role"],
                    "capture_id": capture_id, "action_execution_id": action_id,
                }
                for kind, key in (("action_started", "action_started_at"),
                                  ("action_finished", "action_finished_at")):
                    event_id = f"{attempt['attempt_id']}:{kind}:{action_id}"
                    events[event_id] = {
                        **identity, "event_id": event_id, "event_type": kind,
                        "occurred_at": _review_time(binding.get(key)), "role": None,
                        "capture_id": None, "action_execution_id": action_id,
                    }
    return sorted(events.values(), key=lambda item: (
        item["occurred_at"] is None,
        datetime.fromisoformat(item["occurred_at"]) if item["occurred_at"] else datetime.max,
        item["event_id"],
    ))


def _review_time(value):
    """只接受有时区的实际时间，统一 UTC；无时区时间不能自行猜成服务器本地时间。"""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC).isoformat()
