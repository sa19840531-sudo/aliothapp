"""架构缺口补齐：Manifest、Evidence Request→Probe、Worker 角色、App Quality Gate。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_evidence_manifest import (
    write_app_capture_manifest,
    write_pending_app_capture_manifest,
)
from services.api.app.application.app_interaction_runtime import _align_required_artifacts
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.evidence_request_routing import (
    _select_strategy,
    route_outcome_evidence_requests,
)
from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ObservationArtifact,
    ObservationPacketService,
    ObservationSource,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.application.supplemental_evidence_policy import (
    SUPPLEMENTAL_EVIDENCE_DISABLED,
    SupplementalEvidencePolicy,
)
from services.api.app.core import config as config_module
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    InspectionHandoff,
    InspectionRuleTarget,
    RestorationStrategy,
    RuleJudgeOutcome,
    build_evidence_request,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    SurfaceKind,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_align_required_artifacts_allows_missing_ui_tree_only() -> None:
    aligned = _align_required_artifacts(
        requested=("screenshot", "ui_tree"),
        produced=("screenshot",),
    )
    assert aligned == ("screenshot",)

    before_with_trace = _align_required_artifacts(
        requested=("screenshot", "action_trace"),
        produced=("screenshot", "action_trace"),
    )
    assert before_with_trace == ("screenshot", "action_trace")

    with pytest.raises(ValueError, match="不一致"):
        _align_required_artifacts(
            requested=("screenshot", "action_trace"),
            produced=("screenshot",),
            role="after_action",
        )


def test_app_probe_profile_uses_persistable_capture_roles() -> None:
    profile = CaptureProfilePolicy.for_app_probe()
    assert profile.before.role == "before_action"
    assert profile.after.role == "after_action"


def test_select_strategy_marks_forbidden_and_original_atomic_unfulfillable() -> None:
    forbidden = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.FORBIDDEN,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(hours=1),
    )
    assert (
        _select_strategy(request=forbidden, surface_kind=SurfaceKind.ANDROID)
        is RestorationStrategy.UNFULFILLABLE
    )

    atomic = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-2",
        rule_code="alioth_2",
        target_capture_id="capture-1",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采原原子证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.ORIGINAL_ATOMIC,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.A0,
        expires_at=NOW + timedelta(hours=1),
    )
    assert (
        _select_strategy(request=atomic, surface_kind=SurfaceKind.ANDROID)
        is RestorationStrategy.UNFULFILLABLE
    )


def test_select_strategy_uses_same_app_session_for_android() -> None:
    request = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="补采 ui_tree",
        required_artifact_types=("ui_tree", "screenshot"),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(hours=1),
    )
    assert (
        _select_strategy(request=request, surface_kind=SurfaceKind.ANDROID)
        is RestorationStrategy.SAME_APP_SESSION
    )


@pytest.mark.asyncio
async def test_route_outcome_skips_unfulfillable_without_probe() -> None:
    request = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="不可恢复缺口",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.FORBIDDEN,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(hours=1),
    )
    handoff = InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="hash-1",
        batch_key="batch-1",
        strategy="rule_engine",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=1,
                contract_hash="contract-1",
            ),
        ),
    )
    outcome = RuleJudgeOutcome(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        evidence_requests=(request,),
    )
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(
            return_value={
                "session_id": "session-1",
                "surface_kind": SurfaceKind.ANDROID,
            }
        ),
    ):
        routed = await route_outcome_evidence_requests(
            handoff=handoff,
            outcome=outcome,
            supplemental_evidence_policy=SupplementalEvidencePolicy(
                enabled=True,
                runtime_ready=True,
            ),
        )
    assert routed.planned_request_ids == ()
    assert routed.suppressed_request_ids == ()


@pytest.mark.asyncio
async def test_route_outcome_plans_restoration_without_publishing_probe() -> None:
    """受理只到恢复计划为止：Probe 要等恢复执行器完成恢复并通过等价门禁后才发布。"""
    request = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="补采 ui_tree",
        required_artifact_types=("ui_tree",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(hours=1),
    )
    handoff = InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="hash-1",
        batch_key="batch-1",
        strategy="rule_engine",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=1,
                contract_hash="contract-1",
            ),
        ),
    )
    outcome = RuleJudgeOutcome(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        evidence_requests=(request,),
    )
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(
            return_value={
                "session_id": "session-1",
                "surface_kind": SurfaceKind.ANDROID,
                "current_capture_id": "capture-1",
            }
        ),
    ):
        routed = await route_outcome_evidence_requests(
            handoff=handoff,
            outcome=outcome,
            request_store=SimpleNamespace(get=AsyncMock(return_value=None)),
            supplemental_evidence_policy=SupplementalEvidencePolicy(
                enabled=True,
                runtime_ready=True,
            ),
        )
    assert routed.planned_request_ids == (request.request_id,)
    assert routed.suppressed_request_ids == ()


@pytest.mark.asyncio
async def test_route_outcome_suppresses_request_before_loading_capture_when_disabled() -> None:
    request = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="补采 ui_tree",
        required_artifact_types=("ui_tree",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(hours=1),
    )
    handoff = InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="hash-1",
        batch_key="batch-1",
        strategy="rule_engine",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=1,
                contract_hash="contract-1",
            ),
        ),
    )
    outcome = RuleJudgeOutcome(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        evidence_requests=(request,),
    )
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(),
    ) as load_capture:
        routed = await route_outcome_evidence_requests(
            handoff=handoff,
            outcome=outcome,
        )

    assert routed.planned_request_ids == ()
    assert routed.suppressed_request_ids == (request.request_id,)
    assert routed.gate_decision is not None
    assert routed.gate_decision.reason_code == SUPPLEMENTAL_EVIDENCE_DISABLED
    load_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_app_capture_manifest_passes_complete_screenshot_evidence() -> None:
    checksum = "a" * 64
    screenshot = PersistedEvidenceArtifact(
        artifact_id="44444444-4444-4444-4444-444444444444",
        artifact_type="screenshot",
        storage_path="local://shot.png",
        content_type="image/png",
        checksum=checksum,
        size_bytes=12,
        metadata={},
    )
    persisted = PersistedAppCapture(
        capture=CaptureRef(
            capture_id="11111111-1111-1111-1111-111111111111",
            task_id="22222222-2222-2222-2222-222222222222",
            session_id="33333333-3333-3333-3333-333333333333",
            capture_kind=CaptureKind.APP,
            surface_kind=SurfaceKind.ANDROID,
            role="after_action",
            artifact_ids=("44444444-4444-4444-4444-444444444444",),
            parent_capture_id="66666666-6666-6666-6666-666666666666",
            action_execution_id="77777777-7777-7777-7777-777777777777",
        ),
        agent_run_id="55555555-5555-5555-5555-555555555555",
        screenshot=screenshot,
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature=checksum,
        package_name="com.example",
        activity=".Main",
    )
    stored = SimpleNamespace(added=None)

    class FakeRepo:
        async def add(self, *, manifest):
            stored.added = manifest

    manifest_id = await write_app_capture_manifest(
        persisted=persisted,
        artifacts=(screenshot,),
        repository=FakeRepo(),  # type: ignore[arg-type]
    )
    assert manifest_id is not None
    assert stored.added is not None
    assert stored.added.quality_status is EvidenceQualityStatus.PASS
    assert stored.added.quality_reason == "app_capture_evidence_complete"
    assert stored.added.capture_id == persisted.capture.capture_id
    assert stored.added.artifacts[0].artifact_type == "screenshot"


@pytest.mark.asyncio
async def test_write_app_capture_manifest_fails_when_capture_artifact_missing() -> None:
    checksum = "a" * 64
    screenshot = PersistedEvidenceArtifact(
        artifact_id="44444444-4444-4444-4444-444444444444",
        artifact_type="screenshot",
        storage_path="local://shot.png",
        content_type="image/png",
        checksum=checksum,
        size_bytes=12,
        metadata={},
    )
    action_trace = PersistedEvidenceArtifact(
        artifact_id="88888888-8888-8888-8888-888888888888",
        artifact_type="action_trace",
        storage_path="local://trace.json",
        content_type="application/json",
        checksum=checksum,
        size_bytes=12,
        metadata={},
    )
    persisted = PersistedAppCapture(
        capture=CaptureRef(
            capture_id="11111111-1111-1111-1111-111111111111",
            task_id="22222222-2222-2222-2222-222222222222",
            session_id="33333333-3333-3333-3333-333333333333",
            capture_kind=CaptureKind.APP,
            surface_kind=SurfaceKind.ANDROID,
            role="after_action",
            artifact_ids=("44444444-4444-4444-4444-444444444444",),
            parent_capture_id="66666666-6666-6666-6666-666666666666",
            action_execution_id="77777777-7777-7777-7777-777777777777",
        ),
        agent_run_id="55555555-5555-5555-5555-555555555555",
        screenshot=screenshot,
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature=checksum,
        package_name="com.example",
        activity=".Main",
    )
    stored = SimpleNamespace(added=None)

    class FakeRepo:
        async def add(self, *, manifest):
            stored.added = manifest

    manifest_id = await write_app_capture_manifest(
        persisted=persisted,
        artifacts=(action_trace,),
        repository=FakeRepo(),  # type: ignore[arg-type]
    )

    assert manifest_id is not None
    assert stored.added is not None
    assert stored.added.quality_status is EvidenceQualityStatus.FAIL
    assert (
        stored.added.quality_reason
        == "manifest_artifact_missing:44444444-4444-4444-4444-444444444444"
    )


@pytest.mark.asyncio
async def test_pending_manifest_entrypoint_defers_quality_until_closure() -> None:
    checksum = "a" * 64
    screenshot = PersistedEvidenceArtifact(
        artifact_id="44444444-4444-4444-4444-444444444444",
        artifact_type="screenshot",
        storage_path="local://shot.png",
        content_type="image/png",
        checksum=checksum,
        size_bytes=12,
        metadata={},
    )
    persisted = PersistedAppCapture(
        capture=CaptureRef(
            capture_id="11111111-1111-1111-1111-111111111111",
            task_id="22222222-2222-2222-2222-222222222222",
            session_id="33333333-3333-3333-3333-333333333333",
            capture_kind=CaptureKind.APP,
            surface_kind=SurfaceKind.ANDROID,
            role="after_action",
            artifact_ids=("44444444-4444-4444-4444-444444444444",),
            parent_capture_id="66666666-6666-6666-6666-666666666666",
            action_execution_id="77777777-7777-7777-7777-777777777777",
        ),
        agent_run_id="55555555-5555-5555-5555-555555555555",
        screenshot=screenshot,
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature=checksum,
        package_name="com.example",
        activity=".Main",
    )
    stored = SimpleNamespace(added=None)

    class FakeRepo:
        async def add(self, *, manifest):
            stored.added = manifest

    await write_pending_app_capture_manifest(
        persisted=persisted,
        artifacts=(screenshot,),
        repository=FakeRepo(),  # type: ignore[arg-type]
    )

    assert stored.added.quality_status is EvidenceQualityStatus.PENDING
    assert stored.added.quality_reason == "app_capture_committed"


@pytest.mark.asyncio
async def test_app_observation_packet_passes_without_dom_using_visual_text() -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.saved: list[dict] = []

        async def load_source(self, *, task_id: str, page_capture_id: str):
            return ObservationSource(
                task_id=task_id,
                page_capture_id=page_capture_id,
                page_url="app://com.example/home",
                page_title=None,
                page_type="home",
                page_state="ready",
                action_log=({"action": "click", "semantic_target": "广告"},),
                artifacts=(
                    ObservationArtifact(
                        artifact_id="shot-1",
                        artifact_type="screenshot",
                        content_type="image/png",
                        payload=b"\x89PNG\r\n\x1a\n",
                    ),
                    ObservationArtifact(
                        artifact_id="tree-1",
                        artifact_type="ui_tree",
                        content_type="application/xml",
                        payload=b"<hierarchy/>",
                    ),
                    ObservationArtifact(
                        artifact_id="snap-1",
                        artifact_type="ui_snapshot",
                        content_type="application/json",
                        payload=(
                            b'{"package_name":"com.example.app","activity":".Main",'
                            b'"screen_id":"screen:abc","safe_area":{"top":0,"left":0,'
                            b'"right":0,"bottom":0,"width":1080,"height":1920,'
                            b'"source":"display_bounds"}}'
                        ),
                    ),
                ),
                surface_kind="android",
                visual_observation={
                    "source": "app_visual",
                    "visual_summary": "开屏广告页",
                    "key_elements": [{"text": "立即下载"}],
                },
            )

        async def save_packet(self, **kwargs):
            self.saved.append(kwargs)
            return "packet-app-1"

    repo = FakeRepo()
    packet = await ObservationPacketService(repository=repo).build(
        BuildObservationPacketRequest(
            task_id="task-1",
            page_capture_id="capture-1",
            rule_id="rule-1",
            rule_code="alioth_ad",
            rule_name="广告规则",
            category="ads",
            executor_type="rule_engine",
            check_point="应识别广告",
            input_contract={
                "required_artifacts": [
                    "screenshot",
                    "dom",
                    "visible_text",
                    "ui_snapshot",
                ],
                "quality_gate": {
                    "min_screenshot_count": 1,
                    "allow_empty_visible_text": False,
                },
            },
            judge_config={},
            manual_review_policy={},
            requested_evidence_artifact_ids=("shot-1",),
            agent_input={},
            agent_run_id="run-1",
        )
    )
    assert packet.quality_gate_status == "pass"
    assert packet.packet_payload["facts"]["visible_text_source"] == "app_visual"
    assert "立即下载" in packet.packet_payload["facts"]["visible_text"]
    assert packet.packet_payload["facts"]["has_ui_tree"] is True
    assert "dom" not in (packet.quality_gate_reason or "")


def test_worker_roles_can_split_explorer_and_judge(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_WORKER_ROLES", "explorer")
    settings = config_module._build_worker_settings()
    assert settings.roles == frozenset({"explorer"})

    monkeypatch.setenv("ALIOTH_WORKER_ROLES", "judge")
    settings = config_module._build_worker_settings()
    assert settings.roles == frozenset({"judge"})

    monkeypatch.setenv("ALIOTH_WORKER_ROLES", "explorer,judge")
    settings = config_module._build_worker_settings()
    assert settings.roles == frozenset({"explorer", "judge"})

    monkeypatch.delenv("ALIOTH_WORKER_ROLES", raising=False)
    settings = config_module._build_worker_settings()
    assert settings.roles == frozenset({"explorer", "judge"})
