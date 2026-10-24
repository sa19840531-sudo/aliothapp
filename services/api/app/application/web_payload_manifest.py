"""Web 当前对象留存与分类后需求的严格版本化裁决，不复用旧 PASS 快捷路径。"""

import asyncio
from dataclasses import replace
from hashlib import sha256

from services.api.app.application.evidence_completeness import EvidenceCompletenessAdjudicator
from services.api.app.domain.evidence import (
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementStatus,
    build_evidence_exit_requirement_set,
)
from services.api.app.domain.exploration.web_payload_requirements import (
    WebPayloadRequirement,
    WebPayloadRequirementSet,
)


async def adjudicate_web_payload_manifest(*, manifest, requirements, artifact_store, phase, now):
    if not isinstance(requirements, WebPayloadRequirementSet) or phase not in {
        "capture_preservation",
        "classified_rule_requirements",
    }:
        raise ValueError("WEB_PAYLOAD_STAGE_INVALID")
    if (manifest.task_id, manifest.session_id, manifest.capture_id) != (
        requirements.task_id,
        requirements.session_id,
        requirements.capture_id,
    ):
        raise ValueError("WEB_PAYLOAD_MANIFEST_SCOPE_INVALID")
    technical = next(
        item for item in requirements.requirements if isinstance(item, WebPayloadRequirement)
    )
    actual_types = {item.artifact_type for item in manifest.artifacts}
    reason = None
    if not set(technical.required_artifact_types) <= actual_types:
        reason = "WEB_PAYLOAD_REQUIRED_ARTIFACT_MISSING"
    elif (
        len(manifest.artifacts) > 20
        or sum(item.size_bytes for item in manifest.artifacts) > 64 * 1024 * 1024
    ):
        reason = "WEB_PAYLOAD_OBJECT_BOUND_EXCEEDED"
    else:
        try:
            # 总 I/O 预算防止持有任务锁时无限等待；不输出对象路径或正文。
            async with asyncio.timeout(20):
                for artifact in manifest.artifacts:
                    content = await artifact_store.read(artifact.storage_path)
                    if (
                        len(content) != artifact.size_bytes
                        or "sha256:" + sha256(content).hexdigest() != artifact.checksum
                    ):
                        reason = "WEB_PAYLOAD_OBJECT_INTEGRITY_FAILED"
                        break
        except (OSError, TimeoutError):
            reason = "WEB_PAYLOAD_OBJECT_UNREADABLE"
    business = tuple(
        replace(
            item,
            status=EvidenceExitRequirementStatus.PENDING,
            resolution_manifest_id=None,
            resolution_reason=None,
            evidence_request_id=None,
        )
        for item in requirements.requirements
        if not isinstance(item, WebPayloadRequirement)
    )
    if reason is None and business:
        pending = build_evidence_exit_requirement_set(
            revision=1,
            task_id=requirements.task_id,
            session_id=requirements.session_id,
            capture_id=requirements.capture_id,
            matcher_snapshot_hash=requirements.matcher_snapshot_hash,
            requirements=business,
            created_at=now,
        )
        outcome = EvidenceCompletenessAdjudicator().adjudicate(
            requirement_set=pending, artifacts=manifest.artifacts
        )
        if outcome.quality_status is not EvidenceQualityStatus.PASS:
            reason = "WEB_CLASSIFIED_REQUIREMENTS_INSUFFICIENT"
    stage = {
        "schema_version": "web-payload-stage.v1",
        "phase": phase,
        "binding": dict(requirements.binding),
        "requirement_set_id": requirements.requirement_set_id,
        "requirement_revision": requirements.revision,
        "matcher_snapshot_hash": requirements.matcher_snapshot_hash,
        "business_requirements": "evaluated"
        if phase == "classified_rule_requirements"
        else "not_evaluated",
        "adjudicated_requirement_ids": [item.requirement_id for item in requirements.requirements]
        if reason is None
        else [],
    }
    return build_evidence_manifest_reference(
        task_id=manifest.task_id,
        session_id=manifest.session_id,
        capture_id=manifest.capture_id,
        manifest_version=manifest.manifest_version + 1,
        artifacts=tuple(
            replace(item, metadata={**item.metadata, "web_readiness_stage": stage})
            for item in manifest.artifacts
        ),
        observation_packet_ids=manifest.observation_packet_ids,
        quality_status=EvidenceQualityStatus.FAIL if reason else EvidenceQualityStatus.PASS,
        quality_reason=reason or "web_payload_and_current_requirements_verified",
        created_at=now,
    )
