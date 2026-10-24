"""当前载荷是技术需求，不得伪装业务规则或借旧 PASS 满足新需求。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)


def _legacy():
    return build_evidence_exit_requirement_set(
        revision=1,
        task_id="task",
        session_id="session",
        capture_id="capture",
        matcher_snapshot_hash="matcher",
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        requirements=(
            build_active_session_evidence_requirement(
                task_id="task",
                capture_id="capture",
                rule_id="rule",
                rule_code="rule",
                required_artifact_types=("screenshot",),
                required_capture_roles=("primary",),
                status=EvidenceExitRequirementStatus.PENDING,
            ),
        ),
    )


def _web():
    from services.api.app.domain.exploration.web_payload_requirements import (
        create_payload_requirement_set,
    )

    return create_payload_requirement_set(
        task_id="task",
        agent_run_id="run",
        session_id="session",
        finalization_epoch=0,
        capture_id="capture",
        policy_hash="a" * 64,
        capture_role="after_action",
        committed_artifact_types=("screenshot", "dom", "visible_text"),
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
    )


def test_web_technical_identity_roundtrips_without_business_rule_or_legacy_serialization_change():
    web = _web()
    body = web.to_dict()
    assert body["protocol_version"] == "web-payload-requirement-set.v1"
    assert len(body["requirements"]) == 1
    technical = body["requirements"][0]
    assert technical["scope"] == "current_payload" and "rule_id" not in technical
    assert technical["required_artifact_types"] == ["dom", "screenshot", "visible_text"]
    assert technical["status"] == "pending"
    assert EvidenceExitRequirementSet.from_dict(body) == web
    legacy = _legacy().to_dict()
    assert legacy["protocol_version"] == "evidence-exit-requirement-set.v1"
    assert "scope" not in legacy["requirements"][0]
    assert EvidenceExitRequirementSet.from_dict(legacy).to_dict() == legacy


def test_payload_binding_tampering_and_missing_required_dom_are_rejected():
    body = _web().to_dict()
    body["binding"]["agent_run_id"] = "another-run"
    with pytest.raises(ValueError):
        EvidenceExitRequirementSet.from_dict(body)
    body = _web().to_dict()
    body["requirements"][0]["required_artifact_types"] = ["screenshot"]
    with pytest.raises(ValueError):
        EvidenceExitRequirementSet.from_dict(body)


@pytest.mark.asyncio
async def test_legacy_manifest_writer_refuses_web_technical_set_before_old_pass_shortcut():
    from types import SimpleNamespace

    from services.api.app.application.evidence_manifest_writer import EvidenceManifestWriter
    from services.api.app.domain.evidence import EvidenceQualityStatus

    web = _web()

    class Repository:
        async def get_latest_for_capture(self, **kwargs):
            return SimpleNamespace(quality_status=EvidenceQualityStatus.PASS)

        async def get_latest(self, **kwargs):
            return web

    repository = Repository()
    with pytest.raises(ValueError, match="WEB_PAYLOAD_REQUIRES_STRICT_WRITER"):
        await EvidenceManifestWriter(
            manifest_repository=repository, requirement_repository=repository
        ).adjudicate_for_capture(
            task_id="task", capture_id="capture", session_id="session", requirement_set=web
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
async def test_production_web_factory_keeps_rollout_mode_semantics(monkeypatch, mode):
    from services.api.app.application import web_capture_preservation as preservation
    from services.api.app.application.evidence_exit_gate import EvidenceExitGateBlockedError
    from services.api.app.application.web_runtime_composition import _build_payload_exit_gate
    from services.api.app.core.config import Settings
    from services.api.tests.test_web_evidence_exit_gate import _blocked_outcome, _decision

    calls = []
    blocked = _decision(allow=False)

    class Repository:
        def __init__(self, **kwargs):
            calls.append("repository")

        async def preserve_and_reserve(self, **kwargs):
            calls.append("preservation")
            raise EvidenceExitGateBlockedError(blocked, _blocked_outcome(blocked))

    monkeypatch.setattr(preservation, "PostgresWebPayloadReadinessRepository", Repository)
    monkeypatch.setattr(preservation, "build_evidence_artifact_store", lambda _: object())
    settings = Settings()
    assert settings.web_evidence_exit_gate.mode == "off"
    settings.web_evidence_exit_gate.mode = mode
    gate = _build_payload_exit_gate(settings)
    if mode == "on":
        with pytest.raises(EvidenceExitGateBlockedError):
            await gate.reserve(intent=None, permit=None)
    else:
        assert await gate.reserve(intent=None, permit=None) is None
    assert calls == ([] if mode == "off" else ["repository", "preservation"])
    if mode == "shadow":
        assert gate.blocked_decisions == [blocked]
        assert isinstance(gate._inner, preservation.WebPayloadExitGate)
