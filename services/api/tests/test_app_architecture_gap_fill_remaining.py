"""App 缺口补齐：Session close、Manifest 晋级、Exit Gate、恢复与观测定义。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_evidence_manifest import (
    write_pending_app_capture_manifest,
)
from services.api.app.application.app_probe_hangback import (
    finalize_probe_equivalence_hangback,
)
from services.api.app.application.app_state_invariants import (
    build_app_state_invariant_snapshot,
    derive_app_page_state,
)
from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.exploration.evidence_exit_gate import EvidenceExitGateSnapshot
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind
from services.api.app.domain.interaction.capabilities import (
    build_android_screenshot_only_manifest,
)


def test_android_manifest_marks_reset_and_device_log_ready() -> None:
    manifest = build_android_screenshot_only_manifest(
        adapter_id="test",
        adapter_version="1",
        backend="fake",
    )
    assert manifest.actions["reset_app"].status.value == "ready"
    assert manifest.actions["warm_restore"].status.value == "ready"
    assert manifest.actions["deep_link"].status.value == "ready"
    assert manifest.observations["safe_area"].status.value == "ready"
    assert manifest.observations["identity.package"].status.value == "ready"
    assert manifest.observations["identity.activity"].status.value == "ready"
    assert manifest.observations["device_log"].status.value == "ready"
    assert manifest.artifacts["ui_snapshot"].status.value == "ready"
    assert manifest.artifacts["network_trace"].status.value == "partial"


def test_derive_app_page_state_from_visual_cues() -> None:
    assert (
        derive_app_page_state(
            visual_page_type="splash",
            visual_summary="启动加载中",
            key_elements=("loading",),
            candidate_count=1,
        )
        == "loading"
    )
    assert (
        derive_app_page_state(
            visual_page_type="home",
            visual_summary="开屏广告",
            key_elements=("关闭广告",),
            candidate_count=2,
        )
        == "popup"
    )
    assert (
        derive_app_page_state(
            visual_page_type="feed",
            visual_summary="内容流",
            key_elements=("首页",),
            candidate_count=3,
        )
        == "stable"
    )


def test_build_app_state_invariant_includes_business_session_identity() -> None:
    snap = build_app_state_invariant_snapshot(
        task_id="task-1",
        capture_id="capture-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example",
        activity=".Main",
        device_ref="cloud_phone:default",
        session_options={
            "process_identity": "cloud_phone:default:process",
            "app_data_container_ref": "cloud_phone:default:app-data",
            "lifecycle_phase": "foreground",
            "deep_link": "app://home",
        },
    )
    assert snap.environment["process_identity"] == "cloud_phone:default:process"
    assert snap.environment["app_data_container_ref"] == "cloud_phone:default:app-data"
    assert snap.business["package_name"] == "com.example"
    assert snap.semantic["page_state"] == "stable"
    assert snap.environment["lifecycle_phase"] == "foreground"


def test_build_app_state_invariant_includes_session_event() -> None:
    snap = build_app_state_invariant_snapshot(
        task_id="task-1",
        capture_id="capture-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example",
        activity=".Main",
        session_options={
            "lifecycle_phase": "foreground",
            "session_event": "permission_dialog",
            "page_state_hint": "system_dialog",
            "process_alive": True,
        },
    )
    assert snap.environment["session_event"] == "permission_dialog"
    assert snap.semantic["page_state"] == "system_dialog"
    assert snap.environment["process_alive"] is True


def test_app_exit_gate_requires_pass_when_manifest_exists() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    blocked = EvidenceExitGateSnapshot(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-1",
        requirement_set_id=None,
        baseline_pass_manifest_count=0,
        baseline_manifest_total_count=1,
        nonterminal_active_session_requirement_count=0,
        nonterminal_inline_evidence_request_count=0,
        evidence_commit_in_progress_count=0,
        evaluated_at=now,
        surface_kind="android",
    )
    assert blocked.baseline_evidence_complete is False
    assert blocked.allow_leave is False

    legacy = EvidenceExitGateSnapshot(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-1",
        requirement_set_id=None,
        baseline_pass_manifest_count=0,
        baseline_manifest_total_count=0,
        nonterminal_active_session_requirement_count=0,
        nonterminal_inline_evidence_request_count=0,
        evidence_commit_in_progress_count=0,
        evaluated_at=now,
        surface_kind="android",
    )
    assert legacy.baseline_evidence_complete is True
    assert legacy.allow_leave is True

    passed = EvidenceExitGateSnapshot(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-1",
        requirement_set_id=None,
        baseline_pass_manifest_count=1,
        baseline_manifest_total_count=2,
        nonterminal_active_session_requirement_count=0,
        nonterminal_inline_evidence_request_count=0,
        evidence_commit_in_progress_count=0,
        evaluated_at=now,
        surface_kind="android",
    )
    assert passed.allow_leave is True

    # 规则 inline 补证与探索并行：未结清不得拦 App 离场。
    with_inline = EvidenceExitGateSnapshot(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-1",
        requirement_set_id=None,
        baseline_pass_manifest_count=1,
        baseline_manifest_total_count=2,
        nonterminal_active_session_requirement_count=0,
        nonterminal_inline_evidence_request_count=3,
        evidence_commit_in_progress_count=0,
        evaluated_at=now,
        surface_kind="android",
    )
    assert with_inline.allow_leave is True
    assert with_inline.block_reasons == ()


@pytest.mark.asyncio
async def test_write_pending_manifest_promotes_when_screenshot_ready() -> None:
    from services.api.app.application.app_capture_persistence import PersistedAppCapture

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
    added: list = []

    class FakeRepo:
        async def add(self, *, manifest):
            added.append(manifest)
            return SimpleNamespace(manifest=manifest, created=True)

        async def get_latest_for_capture(self, **kwargs):
            del kwargs
            if not added:
                return None
            return added[-1]

    manifest_id = await write_pending_app_capture_manifest(
        persisted=persisted,
        artifacts=(screenshot,),
        repository=FakeRepo(),  # type: ignore[arg-type]
    )
    assert manifest_id is not None
    assert len(added) == 1
    assert added[0].quality_status is EvidenceQualityStatus.PENDING


@pytest.mark.asyncio
async def test_probe_hangback_marks_request_satisfied_with_pass_manifest() -> None:
    """兼容旧断言：完整恢复/等价闭环后仍写 satisfied + manifest。"""
    from services.api.app.application.app_probe_hangback import (
        _MemoryRestorationAttemptRepository,
    )
    from services.api.app.domain.inspection import (
        EvidenceReplayPolicy,
        EvidenceRequiredEquivalence,
        EvidenceSessionDependency,
        EvidenceSideEffectPolicy,
        EvidenceSourceGrade,
        build_evidence_request,
    )

    now = datetime.now(UTC)
    request = build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {
                "surface_kind": "android",
                "deep_link": "app://x",
                "lifecycle_phase": "foreground",
            },
            "business": {
                "package_name": "com.example",
                "activity": ".Main",
                "route": "app://com.example/.Main",
            },
            "semantic": {"page_state": "stable", "overlay": "none"},
            "structure": {
                "screen_id": "screen:abc123",
                "state_version": "v1",
                "structure_signature": "sha256:struct",
            },
        },
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=now + timedelta(hours=1),
    )
    stored = SimpleNamespace(
        request=request,
        status=EvidenceRequestStatus.RUNNING,
        row_version=3,
    )

    class FakeRequestStore:
        async def get(self, *, request_id: str):
            del request_id
            return stored

        async def transition(self, *, command):
            stored.row_version += 1
            stored.status = command.target_status
            self.last = command
            return stored.row_version

    store = FakeRequestStore()
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=SimpleNamespace(manifest_id="manifest-pass-1")
        )
    )
    await finalize_probe_equivalence_hangback(
        task_id="task-1",
        session_id="session-1",
        outcome=SimpleNamespace(
            claimed=True,
            work_type="probe",
            evidence_request_id=request.request_id,
            result_capture_id="capture-2",
            # 独立观察字段；不得依赖 hangback 从 target_invariants 填空。
            screen_id="screen:abc123",
            structure_signature="sha256:struct",
            state_version="v1",
            page_state="stable",
            lifecycle_phase="foreground",
        ),
        request_store=store,
        manifest_store=manifests,
        restoration_repository=_MemoryRestorationAttemptRepository(),
        package_name="com.example",
        activity=".Main",
        deep_link="app://x",
    )
    assert store.last.resolution_manifest_id == "manifest-pass-1"
    assert store.last.target_status is EvidenceRequestStatus.SATISFIED
    assert "restoration_equivalent:" in (store.last.resolution_reason or "")
