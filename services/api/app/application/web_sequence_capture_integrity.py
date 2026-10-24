"""序列只放宽稳定等待，不放宽同一次截图与结构的文档边界。"""

from collections.abc import Mapping

from services.api.app.application.web_readiness_contracts import web_capture_consistency_proven
from services.api.app.domain.protocol import stable_protocol_hash


def web_sequence_capture_integrity(metadata, *, capture_role, profile):
    """普通 before 可无 Web readiness；显式 Web 采集必须有可重算的一致性证明。"""
    pipeline = metadata.get("pipeline_metrics", {})
    raw = pipeline.get("web_readiness") if isinstance(pipeline, Mapping) else None
    if raw is None and capture_role == "before_action":
        return None
    if not isinstance(raw, Mapping) or not web_capture_consistency_proven(raw, profile=profile):
        raise ValueError("WEB_SEQUENCE_CAPTURE_INCONSISTENT")
    return stable_protocol_hash(
        {
            name: raw.get(name)
            for name in (
                "capture_consistency",
                "capture_started_ms",
                "capture_ended_ms",
                "structure_before",
                "structure_after",
            )
        }
    )
