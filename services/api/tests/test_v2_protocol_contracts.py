"""V2 跨模块协议的版本、兼容、大小和幂等身份门禁。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.evidence import (
    ArtifactReference,
    CaptureCommitted,
    EvidenceBundleReady,
    EvidenceContractValidationError,
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_capture_committed,
    build_evidence_bundle_ready,
    build_evidence_manifest_reference,
)
from services.api.app.domain.inspection import (
    EvidenceInteractionRequirement,
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequestValidationError,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    ProtocolEnvelope,
    build_protocol_envelope,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _action_intent() -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="task-1:branch-1:intent-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        purpose="验证登录入口",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfile(
            profile_id="default",
            before=CaptureDirective(role="before", artifact_types=("screenshot",)),
            after=CaptureDirective(role="primary", artifact_types=("screenshot",)),
        ),
        deadline=NOW,
    )


def _evidence_request() -> EvidenceRequest:
    return build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="确认登录动作后的可见页面状态",
        required_artifact_types=("screenshot", "ui_snapshot"),
        required_interaction=EvidenceInteractionRequirement(
            operation=InteractionOperation.CLICK,
            semantic_target="登录按钮",
            purpose="形成动作后页面证据",
        ),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"route": "/", "account": "test-account"},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW,
    )


def _manifest(*, quality_status: EvidenceQualityStatus) -> EvidenceManifestReference:
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id="artifact-1",
                artifact_type="screenshot",
                role="primary",
                storage_path="evidence/task-1/capture-1/screenshot.png",
                checksum="sha256:abc",
                size_bytes=128,
                content_type="image/png",
            ),
        ),
        quality_status=quality_status,
        quality_reason=("截图损坏" if quality_status is EvidenceQualityStatus.FAIL else None),
        observation_packet_ids=("packet-1",),
        created_at=NOW,
    )


def test_action_intent_round_trip_ignores_unknown_fields() -> None:
    intent = _action_intent()
    payload = {**intent.to_dict(), "future_field": {"producer_version": 2}}

    restored = ActionIntent.from_dict(payload)

    assert restored == intent
    assert restored.to_dict() == intent.to_dict()


def test_protocol_parser_rejects_oversized_unknown_field_and_runtime_handle() -> None:
    intent_payload = _action_intent().to_dict()
    with pytest.raises(ProtocolContractError, match="字节上限"):
        ProtocolEnvelope.from_dict(
            {
                "protocol_version": "message-envelope.v1",
                "message_id": "message-1",
                "message_type": "action-intent",
                "payload_protocol_version": "action-intent.v1",
                "idempotency_key": "intent-1",
                "task_id": "task-1",
                "occurred_at": NOW.isoformat(),
                "payload": intent_payload,
                "future_field": "x" * 300_000,
            }
        )

    with pytest.raises(EvidenceRequestValidationError, match="禁止字段"):
        EvidenceInteractionRequirement(
            operation=InteractionOperation.CLICK,
            semantic_target="登录按钮",
            purpose="错误示例",
            parameters={"selector": "#login"},
        )


def test_protocol_envelope_identity_is_stable_and_round_trips() -> None:
    intent = _action_intent()
    first = build_protocol_envelope(
        message_type="action-intent",
        payload_protocol_version=intent.protocol_version,
        idempotency_key=intent.idempotency_key,
        task_id=intent.task_id,
        aggregate_type="action_intent",
        aggregate_id=intent.intent_id,
        aggregate_version=0,
        occurred_at=NOW,
        payload=intent.to_dict(),
    )
    second = build_protocol_envelope(
        message_type="action-intent",
        payload_protocol_version=intent.protocol_version,
        idempotency_key=intent.idempotency_key,
        task_id=intent.task_id,
        aggregate_type="action_intent",
        aggregate_id=intent.intent_id,
        aggregate_version=0,
        occurred_at=NOW + timedelta(seconds=10),
        payload={**intent.to_dict(), "producer_note": "载荷重建不改变事件身份"},
    )

    assert first.message_id == second.message_id
    assert ProtocolEnvelope.from_dict({**first.to_dict(), "future_field": True}) == first


def test_protocol_envelope_v2_rejects_invalid_version_and_forged_identity() -> None:
    intent = _action_intent()
    with pytest.raises(ProtocolContractError, match="aggregate_version"):
        build_protocol_envelope(
            message_type="action-intent",
            payload_protocol_version=intent.protocol_version,
            idempotency_key=intent.idempotency_key,
            task_id=intent.task_id,
            aggregate_type="action_intent",
            aggregate_id=intent.intent_id,
            aggregate_version=-1,
            occurred_at=NOW,
            payload=intent.to_dict(),
        )

    valid = build_protocol_envelope(
        message_type="action-intent",
        payload_protocol_version=intent.protocol_version,
        idempotency_key=intent.idempotency_key,
        task_id=intent.task_id,
        aggregate_type="action_intent",
        aggregate_id=intent.intent_id,
        aggregate_version=0,
        occurred_at=NOW,
        payload=intent.to_dict(),
    )
    with pytest.raises(ProtocolContractError, match="聚合身份"):
        ProtocolEnvelope.from_dict({**valid.to_dict(), "message_id": "event:forged"})


def test_protocol_envelope_v1_history_remains_readable() -> None:
    restored = ProtocolEnvelope.from_dict(
        {
            "protocol_version": "message-envelope.v1",
            "message_id": "message:legacy-1",
            "message_type": "legacy-event",
            "payload_protocol_version": "legacy-event.v1",
            "idempotency_key": "legacy-1",
            "task_id": "task-1",
            "occurred_at": NOW.isoformat(),
            "payload": {"legacy": True},
        }
    )

    assert restored.protocol_version == "message-envelope.v1"
    assert restored.aggregate_type is None
    assert restored.aggregate_version is None
    assert restored.to_dict()["message_id"] == "message:legacy-1"


def test_evidence_request_locks_capture_rule_and_four_recovery_dimensions() -> None:
    request = _evidence_request()
    payload = {**request.to_dict(), "future_field": "ignored"}

    restored = EvidenceRequest.from_dict(payload)

    assert restored == request
    assert restored.target_capture_id == "capture-1"
    assert restored.session_dependency is EvidenceSessionDependency.ACTIVE_SESSION
    assert restored.replay_policy is EvidenceReplayPolicy.SAME_SURFACE_ONLY
    assert (
        restored.required_equivalence
        is EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION
    )
    assert restored.side_effect_policy is EvidenceSideEffectPolicy.READ_ONLY
    assert restored.minimum_source_grade is EvidenceSourceGrade.A1


def test_evidence_manifest_and_events_are_hash_bound_and_forward_compatible() -> None:
    manifest = _manifest(quality_status=EvidenceQualityStatus.PASS)
    committed = build_capture_committed(manifest, occurred_at=NOW)
    ready = build_evidence_bundle_ready(manifest, occurred_at=NOW)

    assert EvidenceManifestReference.from_dict(
        {**manifest.to_dict(), "future_field": 1}
    ) == manifest
    assert CaptureCommitted.from_dict({**committed.to_dict(), "future_field": 1}) == committed
    assert EvidenceBundleReady.from_dict({**ready.to_dict(), "future_field": 1}) == ready
    assert ready.artifact_ids == ("artifact-1",)

    tampered = {**manifest.to_dict(), "capture_id": "capture-2"}
    with pytest.raises(EvidenceContractValidationError, match="manifest_hash"):
        EvidenceManifestReference.from_dict(tampered)


def test_failed_manifest_cannot_publish_bundle_ready() -> None:
    manifest = _manifest(quality_status=EvidenceQualityStatus.FAIL)

    with pytest.raises(EvidenceContractValidationError, match="quality_status=pass"):
        build_evidence_bundle_ready(manifest, occurred_at=NOW)
