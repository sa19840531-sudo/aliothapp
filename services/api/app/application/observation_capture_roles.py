"""按规则声明的采集角色投影真实补充材料，避免只交接主截图。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ObservationArtifact,
    ObservationPacketValidationError,
    ObservationSource,
    _compact_ui_snapshot,
    _decode_artifacts,
)


async def load_role_evidence(
    *,
    request: BuildObservationPacketRequest,
    primary: ObservationSource,
    load_source: Callable[[tuple], Awaitable[ObservationSource | None]],
) -> tuple[tuple[ObservationArtifact, ...], dict[str, dict[str, Any]]]:
    """只加载声明的同任务同生产 Run 材料，并保留精确清单和附件约束。"""
    roles = dict(request.evidence_capture_roles or {})
    if not roles:
        return (), {}
    if roles.get("primary", primary.page_capture_id) != primary.page_capture_id:
        raise ObservationPacketValidationError("primary 角色与实际证据主采集不一致")
    roles.setdefault("primary", primary.page_capture_id)
    refs = request.evidence_manifest_refs or {}
    retained = request.retained_baseline_artifact_ids or {}
    requested = set(request.requested_evidence_artifact_ids)
    artifacts: dict[str, ObservationArtifact] = {}
    facts: dict[str, dict[str, Any]] = {}
    for role, capture_id in roles.items():
        ref = refs.get(role) or {}
        if ref.get("capture_id") is not None and ref["capture_id"] != capture_id:
            raise ObservationPacketValidationError("证据角色清单与声明的采集归属不一致")
        source_key = (
            request.task_id, capture_id, request.source_agent_run_id, ref.get("manifest_id"),
        )
        if request.original_page_input_set_id is not None:
            # 附加角色沿用当前消费授权，不能退回普通 Capture 最新读取。
            source_key += (
                request.original_page_input_set_id, request.rule_id, request.expected_contract_hash,
                request.work_item_id, request.attempt_id, request.agent_run_id,
                request.lease_token, "h5_page",
            )
        source = (
            primary
            if capture_id == primary.page_capture_id
            else await load_source(source_key)
        )
        if source is None or source.task_id != request.task_id:
            raise ObservationPacketValidationError("证据角色采集不存在或不属于当前任务")
        producer_run = request.source_agent_run_id or primary.agent_run_id
        if source.agent_run_id != producer_run:
            raise ObservationPacketValidationError("证据角色采集不属于当前证据生产 Run")
        if (
            ref
            and source.manifest_quality is not None
            and (
                source.manifest_quality.manifest_id != ref.get("manifest_id")
                or str(source.manifest_quality.manifest_version) != ref.get("manifest_version")
            )
        ):
            raise ObservationPacketValidationError("证据角色清单版本与精确绑定不一致")
        selected = source.artifacts
        if role != "primary":
            retained_ids = set(retained.get(role) or ())
            explicit_ids = requested.intersection(item.artifact_id for item in selected)
            allowed_ids = explicit_ids or retained_ids
            if allowed_ids:
                selected = tuple(item for item in selected if item.artifact_id in allowed_ids)
                if allowed_ids - {item.artifact_id for item in selected}:
                    raise ObservationPacketValidationError(
                        "证据角色声明的保留附件未出现在精确清单中"
                    )
            if not selected:
                raise ObservationPacketValidationError("证据角色未引用有效材料")
            for artifact in selected:
                artifacts.setdefault(artifact.artifact_id, artifact)
        facts[role] = {
            "page_capture_id": capture_id,
            "url": source.page_url,
            "page_type": source.page_type,
            "page_state": source.page_state,
            "artifact_ids": [item.artifact_id for item in selected],
            "manifest_id": ref.get("manifest_id"),
            "quality_status": source.manifest_quality.quality_status
            if source.manifest_quality is not None
            else None,
            "quality_reason": source.manifest_quality.quality_reason
            if source.manifest_quality is not None
            else None,
        }
        if primary.target_platform == "h5" and source.target_platform == "h5":
            by_type: dict[str, list[ObservationArtifact]] = {}
            for artifact in selected:
                by_type.setdefault(artifact.artifact_type, []).append(artifact)
            facts[role].update({
                "parent_capture_id": source.parent_capture_id,
                "capture_role": source.capture_role,
                "captured_at": source.captured_at,
                "ui_snapshot": _compact_ui_snapshot(_decode_artifacts(by_type)["ui_snapshot"]),
            })
    return tuple(artifacts.values()), facts
