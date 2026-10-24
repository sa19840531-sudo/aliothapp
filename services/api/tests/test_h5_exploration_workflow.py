"""H5 V2 首采、页面判定和后继探索工作流测试。"""

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
)
from services.api.app.application.capture_coordination import CaptureCoordinationResult
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.h5_auth_bootstrap import H5AuthBootstrapCommand
from services.api.app.application.orchestration.maf_workflow import prepare_exploration_work
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationWork,
    H5AuthBootstrapResult,
    H5AuthStatus,
    SideEffectPolicy,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5FrontierEntryStatus,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionRef,
)

NOW = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


def _workflow_module():
    return import_module("services.api.app.application.h5_exploration_workflow")


@pytest.mark.asyncio
async def test_adaptive_pattern_outcome_uses_signature_bound_to_action_parameters() -> None:
    module = _workflow_module()

    class PatternKnowledge:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def record_outcome(self, **kwargs: object) -> str:
            self.calls.append(dict(kwargs))
            return "shadow"

    knowledge = PatternKnowledge()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=object(),
        detector=object(),
        orchestrator=object(),
        assessment_repository=object(),
        interaction_pattern_knowledge=knowledge,
    )
    candidate = SimpleNamespace(
        metadata={
            "action_parameters": {
                "navigation_family": "adaptive_snapshot",
                "interaction_pattern_signature": "a" * 64,
            }
        }
    )

    await workflow._record_interaction_pattern_outcome(
        candidate=candidate,
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        outcome="success",
        reason_code="verified_state_transition",
    )

    assert knowledge.calls == [
        {
            "pattern_signature": "a" * 64,
            "task_id": "11111111-1111-1111-1111-111111111111",
            "page_capture_id": "22222222-2222-2222-2222-222222222222",
            "outcome": "success",
            "reason_code": "verified_state_transition",
        }
    ]


@pytest.mark.asyncio
async def test_adaptive_page_tab_outcome_uses_the_same_knowledge_lifecycle() -> None:
    module = _workflow_module()

    class PatternKnowledge:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def record_outcome(self, **kwargs: object) -> str:
            self.calls.append(dict(kwargs))
            return "shadow"

    knowledge = PatternKnowledge()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=object(),
        detector=object(),
        orchestrator=object(),
        assessment_repository=object(),
        interaction_pattern_knowledge=knowledge,
    )
    candidate = SimpleNamespace(
        metadata={
            "action_parameters": {
                "target_mode": "page_tab_navigation",
                "interaction_pattern_signature": "b" * 64,
            }
        }
    )

    await workflow._record_interaction_pattern_outcome(
        candidate=candidate,
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        outcome="success",
        reason_code="verified_state_transition",
    )

    assert knowledge.calls == [
        {
            "pattern_signature": "b" * 64,
            "task_id": "11111111-1111-1111-1111-111111111111",
            "page_capture_id": "22222222-2222-2222-2222-222222222222",
            "outcome": "success",
            "reason_code": "verified_state_transition",
        }
    ]


def test_same_page_fallback_prefers_medium_uncovered_type_over_low_revisit() -> None:
    """活动状态漂移时，同页回退选择也必须避免支付新页型饥饿。"""
    initial = CandidateAction(
        candidate_id="initial-home",
        semantic_target="首页",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"expected_next_page_type": "home"},
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(initial,),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_executed(entry.entry_id)
    home_revisit = replace(
        initial,
        candidate_id="profile-home-revisit",
        navigation_depth=1,
        metadata={
            "source_page_type": "profile",
            "expected_next_page_type": "home",
        },
    )
    payment = replace(
        initial,
        candidate_id="profile-payment",
        semantic_target="立即开通",
        risk_level=InteractionRiskLevel.MEDIUM,
        navigation_depth=1,
        metadata={
            "source_page_type": "profile",
            "expected_next_page_type": "payment",
            "target_keywords": ["profile_local_action"],
        },
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        candidates=(home_revisit, payment),
    )

    selected = _workflow_module()._same_active_page_pending_entry(
        snapshot,
        active_source_location="https://m.example.test/mine",
        active_page_type="profile",
    )

    assert selected is not None
    assert selected.candidate.candidate_id == "profile-payment"


def test_same_page_fallback_prefers_uncovered_navigation_branch_over_tabs() -> None:
    base = CandidateAction(
        candidate_id="home-category",
        semantic_target="internal:page_tab:nth-of-type(2)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "home",
            "target_keywords": ["page_tab_navigation"],
        },
    )
    branch = replace(
        base,
        candidate_id="short-video-branch",
        semantic_target="internal:bottom_navigation:nth-of-type(2)",
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "video_list",
            "target_keywords": ["bottom_navigation"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(base, branch),
    )

    selected = _workflow_module()._same_active_page_pending_entry(
        snapshot,
        active_source_location="https://m.example.test/home",
        active_page_type="home",
    )

    assert selected is not None
    assert selected.candidate.candidate_id == "short-video-branch"


def test_same_page_fallback_prefers_verified_detail_entry_over_tabs() -> None:
    tab = CandidateAction(
        candidate_id="home-tab",
        semantic_target="internal:page_tab:nth-of-type(2)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "home",
            "target_keywords": ["page_tab_navigation"],
        },
    )
    detail = replace(
        tab,
        candidate_id="home-detail",
        semantic_target="当前列表首个内容卡片",
        exploration_value=93,
        navigation_depth=2,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "video_detail",
            "target_keywords": ["list_detail_navigation", "unobstructed_title"],
            "action_parameters": {"target_mode": "list_detail_navigation"},
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(tab, detail),
    )

    selected = _workflow_module()._same_active_page_pending_entry(
        snapshot,
        active_source_location="https://m.example.test/home",
        active_page_type="home",
    )

    assert selected is not None
    assert selected.candidate.candidate_id == "home-detail"


def test_same_page_fallback_prefers_list_evidence_before_more_tabs() -> None:
    tab = CandidateAction(
        candidate_id="home-tab",
        semantic_target="internal:page_tab:nth-of-type(2)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "target_keywords": ["page_tab_navigation"],
        },
    )
    scroll = replace(
        tab,
        candidate_id="home-list-scroll",
        semantic_target="采集列表至底部",
        action_type=InteractionOperation.SCROLL,
        exploration_value=93,
        navigation_depth=0,
        metadata={
            "source_page_type": "home",
            "target_keywords": ["list_scroll_coverage", "bounded_to_bottom"],
            "action_parameters": {
                "target_mode": "h5_list_scroll_coverage",
                "scroll_max_segments": 8,
                "split_scroll_screenshots": True,
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(tab, scroll),
    )

    selected = _workflow_module()._same_active_page_pending_entry(
        snapshot,
        active_source_location="https://m.example.test/home",
        active_page_type="home",
    )

    assert selected is not None
    assert selected.candidate.candidate_id == "home-list-scroll"


def _detection(capture_id: str, *, page_state: str = "stable") -> dict[str, object]:
    return {
        "schema_version": "page-detection.v2",
        "page_capture_id": capture_id,
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": page_state,
        "confidence": 0.95,
        "reason": "首页地标完整",
        "matched_catalog_signals": {
            "ocr_keywords": ["首页", "我的"],
            "dom_hints": ["main"],
            "layout_hints": ["bottom-tabs"],
        },
        "candidate_actions": [
            {
                "action": "click",
                "target": "我的",
                "target_keywords": ["个人中心"],
                "risk_level": "low",
                "purpose": "补齐个人中心覆盖",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            }
        ],
        "next_page_hypotheses": [],
        "risk_flags": [],
        "missing_sources": [],
        "conflicts": [],
        "rule_matching_allowed": True,
        "evidence_artifact_ids": [f"artifact:{capture_id}"],
        "catalog_version": "catalog.v1",
        "catalog_sha256": "catalog-sha",
        "model": "detector-test",
    }


def _logical_detection(
    capture_id: str,
    *,
    transition_proof: str,
) -> dict[str, object]:
    result = _detection(capture_id)
    identity = "sha256:" + "1" * 64
    result["page_url"] = "https://m.example.test/app"
    result["logical_surface"] = {
        "protocol_version": "logical_surface.v1",
        "shadow": False,
        "render_profile": {
            "kind": "dom_spa",
            "status": "confirmed",
            "confidence": 0.94,
        },
        "identity_fingerprint": identity,
        "state_fingerprint": "sha256:" + "2" * 64,
        "logical_transition_proof": transition_proof,
        "page_type_binding": {
            "page_type": "home",
            "identity_fingerprint": identity,
            "page_type_confidence": 0.95,
            "coverage_allowed": transition_proof == "trusted",
        },
    }
    return result


def _safety_interstitial_detection(capture_id: str) -> dict[str, object]:
    result = _detection(capture_id)
    result.update(
        {
            "page_url": "https://m.example.test/",
            "detection_status": "blocked",
            "page_type": None,
            "page_state": "safety_interstitial",
            "confidence": 0.98,
            "reason": "命中安全中间页文案与 checkbox",
            "matched_catalog_signals": {},
            "candidate_actions": [],
            "risk_flags": ["deterministic_safety_interstitial"],
        }
    )
    return result


def _capture(
    capture_id: str,
    *,
    role: str,
    parent: str | None,
    action_execution_id: str = "action-1",
) -> CaptureRef:
    return CaptureRef(
        task_id="task-h5",
        session_id="h5:run-h5",
        capture_id=capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role=role,
        artifact_ids=(f"artifact:{capture_id}",),
        parent_capture_id=parent,
        action_execution_id=action_execution_id,
    )


def _coordination(
    *,
    before: str,
    after: str,
    source: str,
    status: InteractionResultStatus = InteractionResultStatus.SUCCEEDED,
    error_code: str | None = None,
) -> CaptureCoordinationResult:
    return CaptureCoordinationResult(
        before_capture=_capture(before, role="before_action", parent=source),
        interaction=InteractionResult(
            task_id="task-h5",
            session_id="h5:run-h5",
            intent_id="intent-1",
            action_execution_id="action-1",
            source_capture_id=before,
            status=status,
            result_capture_id=after,
            error_code=error_code,
            error_message=("逻辑转换未证实" if error_code is not None else None),
            retryable=error_code is not None,
        ),
        after_capture=_capture(after, role="after_action", parent=before),
    )


def _command() -> Any:
    return _workflow_module().RunH5WorkflowCommand(
        task_id="task-h5",
        agent_run_id="run-h5",
        session_id="h5:run-h5",
        target_url="https://m.example.test/home",
        session_owner="worker-1",
        objective="自动探索 H5 业务页面并形成可质检证据",
        boundaries=ExplorationBoundaries(
            allowed_hosts=("m.example.test",),
            allowed_operations=(
                InteractionOperation.NAVIGATE,
                InteractionOperation.CLICK,
                InteractionOperation.SCROLL,
                InteractionOperation.BACK,
                InteractionOperation.OBSERVE,
            ),
            max_navigation_depth=4,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=8,
            remaining_actions=8,
            max_depth=4,
            remaining_duration_ms=120_000,
        ),
        sampling_policy_summary={
            "journey_steps": ["商品详情"],
            "include_page_types": ["home", "profile", "product_detail"],
            "exclude_page_types": ["payment"],
        },
        observed_at=NOW,
        deadline=NOW + timedelta(minutes=3),
    )


class _Bootstrapper:
    async def start(self, command: Any) -> CaptureCoordinationResult:
        assert command.target_url == "https://m.example.test/home"
        return _coordination(before="capture-anchor", after="capture-initial", source="entry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [InteractionResultStatus.BLOCKED, InteractionResultStatus.FAILED]
)
async def test_failed_bootstrap_stops_auth_detection_and_frontier_and_keeps_diagnostic_refs(status):
    module = _workflow_module()
    coordination = _coordination(
        before="capture-anchor", after="capture-outside", source="entry",
        status=status, error_code="COMMAND_HOST_BOUNDARY_VIOLATION",
    )

    class Bootstrapper:
        async def start(self, command):
            return coordination

    class Forbidden:
        async def run(self, *args, **kwargs):
            raise AssertionError("导航失败后不得继续认证")

        async def detect(self, *args, **kwargs):
            raise AssertionError("导航失败后不得继续识别页面")

        async def start_exploration(self, *args, **kwargs):
            raise AssertionError("导航失败后不得发布 Frontier")

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=Bootstrapper(), detector=Forbidden(), orchestrator=Forbidden(),
        assessment_repository=object(), auth_bootstrap=Forbidden(),
    )
    with pytest.raises(module.CaptureCoordinationError) as caught:
        await workflow.run(_command())
    error = caught.value
    assert error.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert error.before_capture is coordination.before_capture
    assert error.after_capture is coordination.after_capture
    assert error.interaction is coordination.interaction


class _Detector:
    def __init__(self) -> None:
        self.capture_ids: list[str] = []

    async def detect(self, *, capture: CaptureRef, task_id: str, agent_run_id: str):
        assert task_id == "task-h5"
        assert agent_run_id == "run-h5"
        self.capture_ids.append(capture.capture_id)
        return _detection(capture.capture_id)


class _Orchestrator:
    def __init__(self) -> None:
        self.started = []
        self.prepared = []

    async def start_exploration(self, command):
        self.started.append(command)
        return prepare_exploration_work(command)

    async def prepare_exploration(self, command):
        self.prepared.append(command)
        return prepare_exploration_work(command)


class _AssessmentRepository:
    """以可观察事件验证评估落库先于 Work 发布。"""

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.calls: list[dict[str, object]] = []
        self.records: list[SimpleNamespace] = []

    async def upsert(self, **kwargs):
        self.events.append("assessment")
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("assessment persistence unavailable")
        detection = deepcopy(kwargs["detection_result"])
        adapter_result = deepcopy(kwargs["assessment_payload"])
        page_type_assessment = adapter_result.get("page_type_assessment")
        assessment_payload = (
            page_type_assessment if isinstance(page_type_assessment, dict) else {}
        )
        self.records.append(
            SimpleNamespace(
                task_id=kwargs["task_id"],
                agent_run_id=kwargs["agent_run_id"],
                session_id=kwargs["session_id"],
                page_capture_id=kwargs["page_capture_id"],
                detection_status=detection.get("detection_status"),
                page_type=assessment_payload.get("page_type"),
                page_state=detection.get("page_state"),
                status=assessment_payload.get("status"),
                assessment_payload={
                    "detection_result": detection,
                    "adapter_result": adapter_result,
                },
            )
        )

    async def list_for_run(self, *, task_id: str, agent_run_id: str):
        return [
            record
            for record in self.records
            if record.task_id == task_id and record.agent_run_id == agent_run_id
        ]


class _SafetyInterstitialBypass:
    def __init__(self) -> None:
        self.source_capture_ids: list[str] = []

    async def bypass(self, *, command, source_capture):
        module = _workflow_module()
        assert command.task_id == "task-h5"
        self.source_capture_ids.append(source_capture.capture_id)
        return module.H5SafetyInterstitialBypassResult(
            coordination=_coordination(
                before="capture-safety-before",
                after="capture-after-safety",
                source=source_capture.capture_id,
            )
        )


def test_workflow_requires_explicit_assessment_persistence_port() -> None:
    """若仓储参数恢复默认值，生产漏注入会静默丢失 Assessment 审计事实。"""
    module = _workflow_module()

    with pytest.raises(TypeError, match="assessment_repository"):
        module.H5ExplorationWorkflow(
            bootstrapper=_Bootstrapper(),
            detector=_Detector(),
            orchestrator=_Orchestrator(),
        )


@pytest.mark.asyncio
async def test_page_type_detection_retries_once_when_first_result_has_no_page_type() -> None:
    module = _workflow_module()

    class Detector:
        def __init__(self) -> None:
            self.calls = 0

        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            self.calls += 1
            if self.calls == 1:
                result = _detection(capture.capture_id)
                result["detection_status"] = "unknown"
                result["page_type"] = None
                result["candidate_actions"] = []
                return result
            return _detection(capture.capture_id)

    detector = Detector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=object(),
        detector=detector,
        orchestrator=object(),
        assessment_repository=_AssessmentRepository([]),
    )

    detection, assessment = await workflow._detect_and_assess(
        capture=_capture("capture-retry", role="after_action", parent="capture-before"),
        task_id="task-h5",
        agent_run_id="run-h5",
    )

    assert detector.calls == 2
    assert detection["page_type"] == "home"
    assert assessment.execution_status == "accepted"


@pytest.mark.asyncio
async def test_page_type_detection_stops_after_one_bounded_retry() -> None:
    module = _workflow_module()

    class Detector:
        def __init__(self) -> None:
            self.calls = 0

        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            self.calls += 1
            result = _detection(capture.capture_id)
            result["detection_status"] = "unknown"
            result["page_type"] = None
            result["candidate_actions"] = []
            return result

    detector = Detector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=object(),
        detector=detector,
        orchestrator=object(),
        assessment_repository=_AssessmentRepository([]),
    )

    _, assessment = await workflow._detect_and_assess(
        capture=_capture("capture-retry", role="after_action", parent="capture-before"),
        task_id="task-h5",
        agent_run_id="run-h5",
    )

    assert detector.calls == 2
    assert assessment.execution_status == "blocked"
    assert assessment.error_code == "PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED"


def test_workflow_accepts_only_explicit_visual_runtime_modes() -> None:
    """业务 Workflow 只消费组合根注入的严格三值有效模式。"""
    module = _workflow_module()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        h5_visual_coordinate_mode="shadow",
    )

    assert workflow._h5_visual_coordinate_mode == "shadow"
    with pytest.raises(ValueError, match="h5_visual_coordinate_mode"):
        module.H5ExplorationWorkflow(
            bootstrapper=_Bootstrapper(),
            detector=_Detector(),
            orchestrator=_Orchestrator(),
            assessment_repository=_AssessmentRepository([]),
            h5_visual_coordinate_mode="enabled",
        )


@pytest.mark.asyncio
async def test_blocked_assessment_is_persisted_before_initial_work_publication() -> None:
    """若 blocked 适配结果被跳过，Work 会在缺失证据评估事实时发布。"""
    module = _workflow_module()
    events: list[str] = []

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result["evidence_artifact_ids"] = []
            return result

    class Orchestrator(_Orchestrator):
        async def start_exploration(self, command):
            events.append("publish")
            return await super().start_exploration(command)

    repository = _AssessmentRepository(events)
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=repository,
    )

    result = await workflow.run(_command())

    assert events == ["assessment", "publish"]
    assert repository.calls[0]["assessment_payload"]["execution_status"] == "blocked"
    assert result.page_type_assessment.error_code == "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED"


@pytest.mark.asyncio
async def test_initial_stable_business_page_is_recorded_before_candidate_selection() -> None:
    """直接落地的初始业务页必须立即进入任务级页型去重账本。"""
    module = _workflow_module()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
    )

    result = await workflow.run(_command())
    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )

    assert snapshot.covered_page_count == 1
    assert snapshot.covered_page_types == frozenset({"home"})
    assert all(
        entry.candidate.metadata.get("action_parameters", {}).get("target_mode")
        == "observed_page_type_coverage"
        or entry.candidate.metadata.get("expected_next_page_type") != "home"
        or entry.status is H5FrontierEntryStatus.BLOCKED
        for entry in snapshot.entries
    )


@pytest.mark.asyncio
async def test_safety_interstitial_is_bypassed_before_initial_work_publication() -> None:
    """安全中间页必须先留审计事实，再旁路到业务 Capture 后发布首轮 Work。"""
    module = _workflow_module()
    events: list[str] = []

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            self.capture_ids.append(capture.capture_id)
            if capture.capture_id == "capture-initial":
                return _safety_interstitial_detection(capture.capture_id)
            return _detection(capture.capture_id)

    class Orchestrator(_Orchestrator):
        async def start_exploration(self, command):
            events.append("publish")
            return await super().start_exploration(command)

    detector = Detector()
    repository = _AssessmentRepository(events)
    bypass = _SafetyInterstitialBypass()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        safety_interstitial_bypass=bypass,
        detector=detector,
        orchestrator=Orchestrator(),
        assessment_repository=repository,
    )

    result = await workflow.run(_command())

    assert detector.capture_ids == ["capture-initial", "capture-after-safety"]
    assert bypass.source_capture_ids == ["capture-initial"]
    assert events == ["assessment", "assessment", "publish"]
    assert [call["page_capture_id"] for call in repository.calls] == [
        "capture-initial",
        "capture-after-safety",
    ]
    assert repository.calls[0]["assessment_payload"]["error_code"] == (
        "PAGE_TYPE_ASSESSMENT_BLOCKED_BY_SAFETY"
    )
    assert repository.calls[1]["assessment_payload"]["execution_status"] == "accepted"
    assert result.work.source_capture_id == "capture-after-safety"
    assert result.work.frontier.observation.metadata["h5_safety_interstitial"] == {
        "status": "bypassed",
        "blocked_capture_id": "capture-initial",
        "before_capture_id": "capture-safety-before",
        "result_capture_id": "capture-after-safety",
        "action_execution_id": "action-1",
    }


@pytest.mark.asyncio
async def test_skipped_assessment_is_persisted_before_initial_work_publication() -> None:
    """若 skipped 生命周期页面没有落库，not_applicable 审计事实会丢失。"""
    module = _workflow_module()
    events: list[str] = []

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result["page_type"] = "startup_ad"
            result["page_state"] = "countdown"
            return result

    class Orchestrator(_Orchestrator):
        async def start_exploration(self, command):
            events.append("publish")
            return await super().start_exploration(command)

    repository = _AssessmentRepository(events)
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=repository,
    )

    result = await workflow.run(_command())

    assert events == ["assessment", "publish"]
    assert repository.calls[0]["assessment_payload"]["execution_status"] == "skipped"
    assert result.page_type_assessment.execution_status == "skipped"


@pytest.mark.parametrize("adapter_outcome", ["blocked", "skipped"])
@pytest.mark.asyncio
async def test_nonaccepted_assessment_persistence_failure_prevents_publication(
    adapter_outcome: str,
) -> None:
    """blocked/skipped 也必须形成记录；仓储失败时不得发布首轮 Work。"""
    module = _workflow_module()
    events: list[str] = []

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if adapter_outcome == "blocked":
                result["evidence_artifact_ids"] = []
            else:
                result["page_type"] = "startup_ad"
                result["page_state"] = "countdown"
            return result

    class Orchestrator(_Orchestrator):
        async def start_exploration(self, command):
            events.append("publish")
            return await super().start_exploration(command)

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=_AssessmentRepository(events, fail=True),
    )

    with pytest.raises(RuntimeError, match="assessment persistence unavailable"):
        await workflow.run(_command())

    assert events == ["assessment"]


@pytest.mark.asyncio
async def test_initial_assessment_is_persisted_before_initial_work_is_published() -> None:
    """若发布顺序倒置，持久化失败会扩大未审计的 H5 探索事实。"""
    module = _workflow_module()
    events: list[str] = []

    class Orchestrator(_Orchestrator):
        async def start_exploration(self, command):
            events.append("publish")
            return await super().start_exploration(command)

    repository = _AssessmentRepository(events)
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=repository,
    )

    await workflow.run(_command())

    assert events == ["assessment", "publish"]
    assert repository.calls[0]["session_id"] == "h5:run-h5"
    assert repository.calls[0]["page_capture_id"] == "capture-initial"


@pytest.mark.asyncio
async def test_assessment_persistence_failure_prevents_initial_work_publication() -> None:
    """若忽略仓储失败，首轮 Work 会绕过必须存在的 H5 评估审计记录。"""
    module = _workflow_module()
    events: list[str] = []

    class Orchestrator(_Orchestrator):
        async def start_exploration(self, command):
            events.append("publish")
            return await super().start_exploration(command)

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=_AssessmentRepository(events, fail=True),
    )

    with pytest.raises(RuntimeError, match="assessment persistence unavailable"):
        await workflow.run(_command())

    assert events == ["assessment"]


@pytest.mark.asyncio
async def test_successor_assessment_is_persisted_before_successor_work_is_prepared() -> None:
    """若动作后评估没有先落库，后继 Work 会引用尚未审计的页面 Capture。"""
    module = _workflow_module()
    events: list[str] = []

    class Orchestrator(_Orchestrator):
        async def prepare_exploration(self, command):
            events.append("prepare")
            return await super().prepare_exploration(command)

    repository = _AssessmentRepository(events)
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=repository,
    )
    initial = await workflow.run(_command())
    events.clear()
    candidate = initial.work.frontier.candidates[0]
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=ActionIntent(
            task_id=initial.work.task_id,
            session_id=initial.work.session_id,
            intent_id="intent-successor",
            idempotency_key="intent-successor",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=initial.work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose="进入个人中心",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=initial.work.deadline,
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-successor",
            after="capture-successor",
            source=initial.work.source_capture_id,
        ),
    )

    await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )

    assert events == ["assessment", "prepare"]
    assert repository.calls[-1]["page_capture_id"] == "capture-successor"


@pytest.mark.asyncio
async def test_assessment_persistence_failure_prevents_successor_work_preparation() -> None:
    """若吞掉后继持久化异常，Explorer 会发布没有 Assessment 事实的新页面。"""
    module = _workflow_module()
    events: list[str] = []

    class Orchestrator(_Orchestrator):
        async def prepare_exploration(self, command):
            events.append("prepare")
            return await super().prepare_exploration(command)

    repository = _AssessmentRepository(events)
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=Orchestrator(),
        assessment_repository=repository,
    )
    initial = await workflow.run(_command())
    events.clear()
    repository.fail = True
    candidate = initial.work.frontier.candidates[0]
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=ActionIntent(
            task_id=initial.work.task_id,
            session_id=initial.work.session_id,
            intent_id="intent-failing-successor",
            idempotency_key="intent-failing-successor",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=initial.work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose="进入个人中心",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=initial.work.deadline,
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-failing-successor",
            after="capture-failing-successor",
            source=initial.work.source_capture_id,
        ),
    )

    with pytest.raises(RuntimeError, match="assessment persistence unavailable"):
        await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
        )

    assert events == ["assessment"]


class _AuthBootstrap:
    def __init__(self, *, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.commands: list[H5AuthBootstrapCommand] = []

    async def run(self, command, *, initial_capture):
        self.events.append("auth")
        self.commands.append(command)
        assert initial_capture.capture_id == "capture-initial"
        return H5AuthBootstrapResult(
            status=H5AuthStatus.AUTHENTICATED,
            final_capture=_capture(
                "capture-auth-root",
                role="after_action",
                parent="capture-auth-profile",
                action_execution_id="auth-return-action",
            ),
            credential_scope="domain",
            reason_code="password_login_verified",
            attempted=True,
            needs_review=False,
        )


@pytest.mark.asyncio
async def test_initial_navigation_capture_is_detected_before_work_is_published() -> None:
    module = _workflow_module()
    detector = _Detector()
    orchestrator = _Orchestrator()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=orchestrator,
        assessment_repository=_AssessmentRepository([]),
    )

    result = await workflow.run(_command())

    assert detector.capture_ids == ["capture-initial"]
    assert len(orchestrator.started) == 1
    assert result.work.surface_kind is SurfaceKind.WEB
    assert result.work.source_capture_id == "capture-initial"
    assert result.work.frontier.observation.page_type == "home"
    assert dict(result.work.frontier.observation.metadata["sampling_policy_summary"]) == {
        "journey_steps": ("商品详情",),
        "include_page_types": ("home", "profile", "product_detail"),
        "exclude_page_types": ("payment",),
    }
    assert result.work.frontier.observation.metadata["journey_steps"] == ("商品详情",)
    assert result.page_type_assessment.execution_status == "accepted"
    assert result.auth_result.status is H5AuthStatus.DISABLED
    assert result.work.frontier.observation.metadata["h5_auth"] == {
        "status": "disabled",
        "credential_scope": None,
        "reason_code": "h5_auth_disabled",
        "attempted": False,
        "needs_review": False,
    }


@pytest.mark.asyncio
async def test_auth_bootstrap_runs_before_formal_root_detection() -> None:
    module = _workflow_module()
    events: list[str] = []
    auth = _AuthBootstrap(events=events)

    class OrderedDetector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            events.append(f"detect:{capture.capture_id}")
            return await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )

    detector = OrderedDetector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        auth_bootstrap=auth,
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
    )

    result = await workflow.run(_command())

    assert events == ["auth", "detect:capture-auth-root"]
    assert detector.capture_ids == ["capture-auth-root"]
    assert result.work.source_capture_id == "capture-auth-root"
    assert result.work.frontier.observation.metadata["before_capture_id"] == (
        "capture-auth-profile"
    )
    assert result.work.frontier.observation.metadata["action_execution_id"] == (
        "auth-return-action"
    )
    assert result.auth_result.status is H5AuthStatus.AUTHENTICATED
    assert result.work.frontier.observation.metadata["h5_auth"] == {
        "status": "authenticated",
        "credential_scope": "domain",
        "reason_code": "password_login_verified",
        "attempted": True,
        "needs_review": False,
    }
    assert auth.commands[0].target_url == "https://m.example.test/home"


@pytest.mark.asyncio
async def test_after_capture_is_redetected_before_successor_work_is_prepared() -> None:
    module = _workflow_module()
    detector = _Detector()
    orchestrator = _Orchestrator()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=orchestrator,
        assessment_repository=_AssessmentRepository([]),
    )
    initial = await workflow.run(_command())
    candidate = initial.work.frontier.candidates[0]
    intent = ActionIntent(
        task_id="task-h5",
        session_id="h5:run-h5",
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-initial",
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        purpose="进入个人中心",
        capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
        deadline=NOW + timedelta(minutes=2),
        parameters={"candidate_id": candidate.candidate_id},
        risk_level=InteractionRiskLevel.LOW,
    )
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id="task-h5",
        decision=intent,
        coordination=_coordination(
            before="capture-before-profile",
            after="capture-profile",
            source="capture-initial",
        ),
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )

    assert detector.capture_ids == ["capture-initial", "capture-profile"]
    assert successor.work.source_capture_id == "capture-profile"
    assert successor.work.budget.remaining_actions == 7
    assert initial.work.frontier.observation.state_version in (
        successor.work.frontier.visited_state_ids
    )
    assert successor.work.frontier.observation.metadata["parent_capture_id"] == ("capture-initial")
    assert successor.work.frontier.observation.metadata["parent_state_version"] == (
        initial.work.frontier.observation.state_version
    )
    assert successor.work.frontier.observation.metadata["executed_candidate_ids"] == (
        candidate.candidate_id,
    )
    assert dict(successor.work.frontier.observation.metadata["sampling_policy_summary"]) == {
        "journey_steps": ("商品详情",),
        "include_page_types": ("home", "profile", "product_detail"),
        "exclude_page_types": ("payment",),
    }
    assert successor.work.frontier.observation.metadata["journey_steps"] == ("商品详情",)
    assert successor.work.frontier.candidates == ()
    assert successor.work.frontier.observation.metadata["objective_satisfied"] is False
    assert successor.work.frontier.observation.metadata["h5_coverage_snapshot"][
        "missing_page_types"
    ] == ("profile",)


@pytest.mark.asyncio
async def test_h5_scheduler_state_survives_multi_round_failure_and_json_restore() -> None:
    """真实 H5 后继链必须累计调度账本，不能因新 Frontier 重新归零。"""
    module = _workflow_module()

    class MultiRoundDetector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if len(self.capture_ids) >= 2:
                result["page_type"] = "profile"
                result["page_url"] = "https://m.example.test/mine"
                base_action = dict(result["candidate_actions"][0])
                result["candidate_actions"] = [
                    {**base_action, "expected_next_page_type": "profile"},
                    {
                        **base_action,
                        "action": "click",
                        "target": "商品详情",
                        "target_keywords": ["商品详情"],
                        "purpose": "补齐商品详情覆盖",
                        "expected_next_page_type": "product_detail",
                    },
                ]
            return result

    detector = MultiRoundDetector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=len(detector.capture_ids)),
    )

    def scheduler_state(work):
        value = work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]
        assert isinstance(value, Mapping)
        return dict(value)

    def action_turn(work, *, before: str, after: str, action_id: str, failed: bool):
        candidate = work.frontier.candidates[0]
        intent = ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id=f"intent:{action_id}",
            idempotency_key=f"intent:{action_id}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose="继续 H5 页面探索",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=work.deadline,
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=candidate.risk_level,
        )
        return ExplorerTurnResult(
            work_id=work.work_id,
            task_id=work.task_id,
            decision=intent,
            coordination=_coordination(
                before=before,
                after=after,
                source=work.source_capture_id,
                status=(
                    InteractionResultStatus.FAILED
                    if failed
                    else InteractionResultStatus.SUCCEEDED
                ),
                error_code="driver_failed" if failed else None,
            ),
        )

    initial = await workflow.run(_command())
    assert not scheduler_state(initial.work)["consumed_actions"]

    first = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(
            prior_work=initial.work,
            turn=action_turn(
                initial.work,
                before="capture-before-1",
                after="capture-profile-1",
                action_id="action-1",
                failed=False,
            ),
        )
    )
    first_state = scheduler_state(first.work)
    assert sum(first_state["consumed_actions"].values()) == 1

    # 用 JSON 形态重建 Frontier metadata，模拟跨进程/持久化恢复。
    restored_metadata = dict(first.work.frontier.observation.metadata)
    restored_metadata[BOUNDED_EXPLORATION_STATE_KEY] = json.loads(
        json.dumps(
            first_state,
            default=lambda value: dict(value) if isinstance(value, Mapping) else value,
        )
    )
    restored_work = replace(
        first.work,
        frontier=replace(
            first.work.frontier,
            observation=replace(
                first.work.frontier.observation,
                metadata=restored_metadata,
            ),
        ),
    )
    assert restored_work.frontier.candidates

    second = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(
            prior_work=restored_work,
            turn=action_turn(
                restored_work,
                before="capture-before-2",
                after="capture-profile-2",
                action_id="action-2",
                failed=True,
            ),
        )
    )
    second_state = scheduler_state(second.work)
    assert sum(second_state["consumed_actions"].values()) == 2
    assert sum(second_state["retry_attempts"].values()) == 1


@pytest.mark.asyncio
async def test_h5_workflow_reads_persisted_gap_facts_on_initial_and_successor_rounds() -> None:
    """H5 正式工作流应把 PG 证据缺口带入每一轮调度，而非依赖候选自报。"""
    module = _workflow_module()

    class SchedulerFactsReader:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def read_h5_facts(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {
                "exploration_scheduler_facts_scope": {
                    "task_id": kwargs["task_id"],
                    "session_id": kwargs["session_id"],
                    "finalization_epoch": 0,
                    "source": "test-pg",
                },
                "h5_coverage_snapshot_fact": {
                    "source": "qc_h5_coverage_snapshot",
                    "status": "incomplete",
                    "missing_page_types": ["profile"],
                },
                "rule_evidence_requests": [
                    {
                        "source": "qc_evidence_request",
                        "target_capture_id": "capture-profile-target",
                        "evidence_gap": {"page_types": ["profile"]},
                    }
                ],
            }

    reader = SchedulerFactsReader()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        scheduler_facts_reader=reader,
    )

    initial = await workflow.run(_command())
    initial_metadata = initial.work.frontier.observation.metadata
    initial_state = initial_metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert initial_metadata["exploration_scheduler_facts_scope"]["source"] == "test-pg"
    assert "profile" in initial_state["evidence_gap_page_types"]
    assert initial_metadata["rule_evidence_requests"][0]["target_capture_id"] == (
        "capture-profile-target"
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(
            prior_work=initial.work,
            turn=ExplorerTurnResult(
                work_id=initial.work.work_id,
                task_id=initial.work.task_id,
                decision=ActionIntent(
                    task_id=initial.work.task_id,
                    session_id=initial.work.session_id,
                    intent_id="intent:scheduler-facts",
                    idempotency_key="intent:scheduler-facts",
                    surface_kind=SurfaceKind.WEB,
                    source_capture_id=initial.work.source_capture_id,
                    action_type=initial.work.frontier.candidates[0].action_type,
                    semantic_target=initial.work.frontier.candidates[0].semantic_target,
                    purpose="继续探索",
                    capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                    deadline=initial.work.deadline,
                    parameters={
                        "candidate_id": initial.work.frontier.candidates[0].candidate_id
                    },
                    risk_level=initial.work.frontier.candidates[0].risk_level,
                ),
                coordination=_coordination(
                    before="capture-before-facts",
                    after="capture-after-facts",
                    source=initial.work.source_capture_id,
                ),
            ),
        )
    )
    successor_metadata = successor.work.frontier.observation.metadata
    successor_state = successor_metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert successor_metadata["exploration_scheduler_facts_scope"]["source"] == "test-pg"
    assert "profile" in successor_state["evidence_gap_page_types"]
    assert len(reader.calls) == 2


@pytest.mark.asyncio
async def test_successful_skip_keeps_next_rotating_startup_ad_in_frontier() -> None:
    """同路由连续开屏广告不能因首次跳过成功而被候选去重。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result.update(
                {
                    "page_type": "startup_ad",
                    "page_state": "countdown",
                    "candidate_actions": [
                        {
                            "action": "click",
                            "target": "跳过",
                            "target_keywords": [
                                "startup_ad",
                                "close_control",
                                "startup_ad_dismissal",
                            ],
                            "risk_level": "low",
                            "purpose": "关闭当前开屏广告并继续进入首页",
                            "expected_next_page_type": "home",
                            "navigation_depth": 0,
                            "obstruction_capability": "startup_ad_dismissal",
                            "obstruction_signature": "h5-obstruction:" + "1" * 64,
                        }
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(replace(_command(), sampling_policy_summary={}))
    initial = replace(
        initial,
        work=replace(
            initial.work,
            frontier=replace(
                initial.work.frontier,
                observation=replace(
                    initial.work.frontier.observation,
                    metadata={
                        **dict(initial.work.frontier.observation.metadata),
                        "startup_ad_countdown_reobserve_count": 4,
                    },
                ),
            ),
        ),
    )
    first = initial.work.frontier.candidates[0]
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=ActionIntent(
            task_id=initial.work.task_id,
            session_id=initial.work.session_id,
            intent_id="intent-startup-rotate",
            idempotency_key="intent-startup-rotate",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=initial.work.source_capture_id,
            action_type=first.action_type,
            semantic_target=first.semantic_target,
            purpose="关闭当前开屏广告并继续进入首页",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=initial.work.deadline,
            parameters={"candidate_id": first.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-startup-rotate",
            after="capture-startup-rotate-2",
            source=initial.work.source_capture_id,
        ),
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )
    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )

    assert len(successor.work.frontier.candidates) == 1
    assert successor.work.frontier.candidates[0].semantic_target == "跳过"
    assert successor.work.frontier.candidates[0].candidate_id != first.candidate_id
    assert successor.work.frontier.observation.metadata["startup_ad_dismissal_count"] == 1
    assert (
        successor.work.frontier.observation.metadata[
            "startup_ad_countdown_reobserve_count"
        ]
        == 0
    )
    assert snapshot.covered_page_types == frozenset()


@pytest.mark.asyncio
async def test_retryable_action_failure_reobserves_before_capture_and_skips_stale_entry() -> None:
    """动作被遮挡后必须从动作前 Capture 生成新候选，且不把陈旧动作计为覆盖。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if capture.capture_id == "capture-obstructed":
                result.update(
                    {
                        "page_type": "startup_ad",
                        "page_state": "popup",
                        "candidate_actions": [
                            {
                                "action": "click",
                                "target": "关闭当前弹窗",
                                "target_keywords": [
                                    "popup_dismissal",
                                    "close_control",
                                    "dialog_overlay",
                                ],
                                "risk_level": "low",
                                "purpose": "关闭顶层登录失败弹窗并重新观察",
                                "navigation_depth": 0,
                                "obstruction_capability": "dialog_overlay",
                                "obstruction_signature": "h5-obstruction:" + "a" * 64,
                            }
                        ],
                    }
                )
            return result

    detector = Detector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    before = _capture(
        "capture-obstructed",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-startup",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code="STARTUP_AD_DISMISSAL_NOT_FOUND",
            retryable=True,
        )
    )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    stale = next(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SKIPPED
    )
    selected = next(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    assert detector.capture_ids == ["capture-initial", "capture-obstructed"]
    assert stale.last_failure_reason == (
        "action_failure_reobserved:STARTUP_AD_DISMISSAL_NOT_FOUND"
    )
    assert selected.candidate.semantic_target == "关闭当前弹窗"
    assert successor.work.source_capture_id == before.capture_id
    assert successor.work.budget.remaining_actions == 7
    assert successor.work.frontier.observation.metadata["action_reobserve_error_code"] == (
        "STARTUP_AD_DISMISSAL_NOT_FOUND"
    )


@pytest.mark.asyncio
async def test_startup_failures_do_not_starve_new_home_popup_dismissal() -> None:
    """开屏失败耗尽队列重试后，新首页弹窗仍应获得独立清障机会。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if capture.capture_id != "capture-home-popup":
                result.update(
                    {
                        "page_type": "startup_ad",
                        "page_state": "countdown",
                        "candidate_actions": [
                            {
                                "action": "click",
                                "target": "跳过",
                                "target_keywords": [
                                    "startup_ad",
                                    "close_control",
                                    "startup_ad_dismissal",
                                ],
                                "risk_level": "low",
                                "purpose": "关闭当前开屏广告并继续进入首页",
                                "expected_next_page_type": "home",
                                "navigation_depth": 0,
                                "obstruction_capability": "startup_ad_dismissal",
                                "obstruction_signature": "h5-obstruction:" + "a" * 64,
                            }
                        ],
                    }
                )
                return result
            result.update(
                {
                    "page_type": "home",
                    "page_state": "popup",
                    "candidate_actions": [
                        {
                            "action": "click",
                            "target": "关闭当前弹窗",
                            "target_keywords": [
                                "popup_dismissal",
                                "close_control",
                                "dialog_generic_close",
                            ],
                            "risk_level": "low",
                            "purpose": "关闭首页业务弹窗并继续探索",
                            "navigation_depth": 0,
                            "obstruction_capability": "dialog_generic_close",
                            "obstruction_signature": "h5-obstruction:" + "b" * 64,
                        }
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    current = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for capture_id, error_code in (
        ("capture-startup-still-visible", "STARTUP_AD_DISMISSAL_NOT_OBSERVED"),
        ("capture-home-popup", "STARTUP_AD_DISMISSAL_NOT_FOUND"),
    ):
        current = await workflow.prepare_after_action_failure(
            module.ReobserveH5ExplorationCommand(
                prior_work=current.work,
                before_capture=_capture(
                    capture_id,
                    role="before_action",
                    parent=current.work.source_capture_id,
                    action_execution_id=f"action:{capture_id}",
                ),
                error_code=error_code,
                retryable=True,
            )
        )

    scheduler_state = current.work.frontier.observation.metadata[
        BOUNDED_EXPLORATION_STATE_KEY
    ]
    assert scheduler_state["retry_attempts"] == {"required": 2}
    assert current.work.frontier.observation.page_type == "home"
    assert current.work.frontier.candidates[0].semantic_target == "关闭当前弹窗"
    assert current.work.frontier.candidates[0].metadata["action_parameters"][
        "target_mode"
    ] == "popup_dismissal"


@pytest.mark.asyncio
async def test_action_failure_preserves_monotonic_lifecycle_lineage() -> None:
    """失败重观察不能清零开屏清障等共享预算，否则可绕过有界终止。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if capture.capture_id == "capture-stale-startup":
                result.update(
                    {
                        "page_type": "startup_ad",
                        "page_state": "countdown",
                        "candidate_actions": [
                            {
                                "action": "click",
                                "target": "跳过",
                                "target_keywords": [
                                    "startup_ad",
                                    "close_control",
                                    "startup_ad_dismissal",
                                ],
                                "risk_level": "low",
                                "purpose": "关闭当前开屏广告并继续进入首页",
                                "expected_next_page_type": "home",
                                "navigation_depth": 0,
                                "obstruction_capability": "startup_ad_dismissal",
                                "obstruction_signature": "h5-obstruction:" + "b" * 64,
                            }
                        ],
                    }
                )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    prior_metadata = {
        **dict(initial.work.frontier.observation.metadata),
        "line_loading_reobserve_count": 2,
        "startup_ad_countdown_reobserve_count": 4,
        "startup_ad_dismissal_count": 3,
        "flutter_bootstrap_reobserve_count": 1,
        "insufficient_reobserve_count": 2,
        "ambiguous_launch_shell_reobserve_count": 2,
        "logical_reobserve_count": 1,
        "logical_reobserve_identity_fingerprint": "sha256:" + "c" * 64,
        "capability_projection_reobserve_count": 1,
        "popup_absence_reobserve_completed": True,
        "popup_absence_obstruction_signature": "h5-obstruction:" + "d" * 64,
    }
    initial = replace(
        initial,
        work=replace(
            initial.work,
            frontier=replace(
                initial.work.frontier,
                observation=replace(
                    initial.work.frontier.observation,
                    metadata=prior_metadata,
                ),
            ),
        ),
    )
    before = _capture(
        "capture-stale-startup",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-source-state-stale",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code="LOGICAL_SOURCE_STATE_STALE",
            retryable=True,
        )
    )
    metadata = successor.work.frontier.observation.metadata

    assert metadata["line_loading_reobserve_count"] == 2
    assert metadata["startup_ad_countdown_reobserve_count"] == 4
    assert metadata["startup_ad_dismissal_count"] == 3
    assert metadata["flutter_bootstrap_reobserve_count"] == 1
    assert metadata["insufficient_reobserve_count"] == 2
    assert metadata["ambiguous_launch_shell_reobserve_count"] == 2
    assert metadata["logical_reobserve_count"] == 1
    assert metadata["logical_reobserve_identity_fingerprint"] == "sha256:" + "c" * 64
    # 当前 startup_ad 没有能力投影拒绝，Observation 会按既有合同把该局部计数归零；
    # failure lineage 仍先继承原值，只有新状态确认不再需要时才能重置。
    assert metadata["capability_projection_reobserve_count"] == 0
    assert metadata["popup_absence_reobserve_completed"] is True
    assert metadata["popup_absence_obstruction_signature"] == (
        "h5-obstruction:" + "d" * 64
    )
    scheduler_state = metadata["bounded_exploration_scheduler"]
    assert scheduler_state["consumed_actions"] == {}
    assert scheduler_state["retry_attempts"] == {}


@pytest.mark.asyncio
async def test_action_failure_cannot_bypass_startup_ad_dismissal_limit() -> None:
    """达到十层上限后即使发生 stale 重观察，也不得重新签发跳过动作。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result.update(
                {
                    "page_type": "startup_ad",
                    "page_state": "countdown",
                    "candidate_actions": [
                        {
                            "action": "click",
                            "target": "跳过",
                            "target_keywords": ["startup_ad", "close_control"],
                            "risk_level": "low",
                            "purpose": "关闭当前开屏广告并继续进入首页",
                            "expected_next_page_type": "home",
                            "navigation_depth": 0,
                        }
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    initial_metadata = {
        **dict(initial.work.frontier.observation.metadata),
        "startup_ad_dismissal_count": 10,
    }
    prior_work = replace(
        initial.work,
        frontier=replace(
            initial.work.frontier,
            observation=replace(
                initial.work.frontier.observation,
                metadata=initial_metadata,
            ),
        ),
    )
    before = _capture(
        "capture-startup-limit-stale",
        role="before_action",
        parent=prior_work.source_capture_id,
        action_execution_id="action-startup-limit-stale",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=prior_work,
            before_capture=before,
            error_code="LOGICAL_SOURCE_STATE_STALE",
            retryable=True,
        )
    )

    assert successor.work.frontier.candidates == ()
    assert successor.work.frontier.observation.metadata["startup_ad_dismissal_count"] == 10
    assert (
        successor.work.frontier.observation.metadata["startup_ad_dismissal_exhausted"]
        is True
    )


@pytest.mark.asyncio
async def test_action_failure_reuses_accepted_assessment_for_same_immutable_capture() -> None:
    """同一 Capture 的模型重判即使非确定，也不得制造第二次评估争写。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result["reason"] = f"第 {len(self.capture_ids)} 次模型表述"
            return result

    class ConflictOnReplayRepository(_AssessmentRepository):
        async def upsert(self, **kwargs):
            if any(
                record.page_capture_id == kwargs["page_capture_id"]
                for record in self.records
            ):
                raise RuntimeError("同一 Capture 的非确定评估 payload 冲突")
            await super().upsert(**kwargs)

    detector = Detector()
    repository = ConflictOnReplayRepository([])
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=repository,
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    same_capture = _capture(
        initial.work.source_capture_id,
        role="before_action",
        parent="capture-anchor",
        action_execution_id="action-source-state-stale",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=same_capture,
            error_code="LOGICAL_SOURCE_STATE_STALE",
            retryable=True,
        )
    )

    assert detector.capture_ids == ["capture-initial"]
    assert len(repository.calls) == 1
    assert successor.work.source_capture_id == "capture-initial"
    assert successor.detection_result["reason"] == "第 1 次模型表述"
    assert successor.page_type_assessment.execution_status == "accepted"


@pytest.mark.asyncio
async def test_repeated_action_failure_on_same_capture_creates_new_successor_lineage() -> None:
    """同一不可变 Capture 再次 stale 时，后继不得复用当前 Work 身份。"""
    module = _workflow_module()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    before = _capture(
        "capture-repeated-stale",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-repeated-stale",
    )
    first = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code="LOGICAL_SOURCE_STATE_STALE",
            retryable=True,
        )
    )
    repeated_frontier = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=before.capture_id,
        source_state_version=first.work.frontier.observation.state_version,
        source_location=first.work.frontier.observation.location,
        navigation_depth=0,
        candidates=(initial.work.frontier.candidates[0],),
    )
    repeated_frontier = repeated_frontier.select_entry(
        repeated_frontier.pending_entries[0].entry_id
    )
    first = replace(
        first,
        work=replace(
            first.work,
            frontier=replace(
                first.work.frontier,
                observation=replace(
                    first.work.frontier.observation,
                    metadata={
                        **dict(first.work.frontier.observation.metadata),
                        H5_TASK_FRONTIER_METADATA_KEY: repeated_frontier.to_dict(),
                    },
                ),
            ),
        ),
    )

    second = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=first.work,
            before_capture=before,
            error_code="LOGICAL_SOURCE_STATE_STALE",
            retryable=True,
        )
    )

    assert first.work.work_id != initial.work.work_id
    assert second.work.work_id != first.work.work_id
    assert len(second.work.branch_id) <= 128
    assert second.work.source_capture_id == before.capture_id
    assert (
        second.work.frontier.observation.metadata["parent_work_id"]
        == first.work.work_id
    )


@pytest.mark.asyncio
async def test_action_failure_does_not_reuse_blocked_retryable_assessment() -> None:
    """blocked/retryable 不是完整事实，必须重检并保持冲突失败关闭。"""
    module = _workflow_module()

    class ConflictOnReplayRepository(_AssessmentRepository):
        def __init__(self) -> None:
            super().__init__([])
            self.upsert_attempts = 0

        async def upsert(self, **kwargs):
            self.upsert_attempts += 1
            if any(
                record.page_capture_id == kwargs["page_capture_id"]
                for record in self.records
            ):
                raise RuntimeError("重复评估必须走 CAS 或失败关闭")
            await super().upsert(**kwargs)

    detector = _Detector()
    repository = ConflictOnReplayRepository()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=repository,
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    repository.records[0].assessment_payload["adapter_result"] = {
        "protocol_version": "h5-page-type-assessment-adapter.v1",
        "execution_status": "blocked",
        "error_code": "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED",
        "message": "证据尚不完整",
        "retryable": True,
    }
    same_capture = _capture(
        initial.work.source_capture_id,
        role="before_action",
        parent="capture-anchor",
        action_execution_id="action-retryable-assessment",
    )

    with pytest.raises(RuntimeError, match="CAS 或失败关闭"):
        await workflow.prepare_after_action_failure(
            module.ReobserveH5ExplorationCommand(
                prior_work=initial.work,
                before_capture=same_capture,
                error_code="LOGICAL_SOURCE_STATE_STALE",
                retryable=True,
            )
        )

    assert detector.capture_ids == ["capture-initial", "capture-initial"]
    assert repository.upsert_attempts == 2


@pytest.mark.asyncio
async def test_popup_dismissal_reobserve_state_accumulates_across_successors() -> None:
    """无关闭控件弹窗的失败重观察：同签名跨后继工作累计计数，供 decide_next 止损。"""
    module = _workflow_module()

    def _popup_detection(result: dict) -> dict:
        result.update(
            {
                "page_type": "video_detail",
                "page_state": "popup",
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "关闭当前弹窗",
                        "target_keywords": [
                            "popup_dismissal",
                            "close_control",
                            "dialog_overlay",
                        ],
                        "risk_level": "low",
                        "purpose": "关闭无关闭控件的底部弹层并重新观察",
                        "navigation_depth": 0,
                        "obstruction_capability": "dialog_overlay",
                        "obstruction_signature": "h5-obstruction:" + "a" * 64,
                    }
                ],
            }
        )
        return result

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            # 初始与重观察均产出弹窗关闭候选：初始 work 选中它、失败后又重现。
            if capture.capture_id in {"capture-initial", "capture-obstructed"}:
                return _popup_detection(result)
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    # 注入前序累计（模拟同一弹窗已连续重观察 1 次）。
    prior_observation = initial.work.frontier.observation
    seeded_metadata = {
        **dict(prior_observation.metadata),
        "popup_dismissal_reobserve_state": {
            "signature": "h5-obstruction:" + "a" * 64,
            "count": 1,
            "streak": 1,
        },
    }
    seeded_work = replace(
        initial.work,
        frontier=replace(
            initial.work.frontier,
            observation=replace(prior_observation, metadata=seeded_metadata),
        ),
    )
    before = _capture(
        "capture-obstructed",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-popup",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=seeded_work,
            before_capture=before,
            error_code="POPUP_DISMISSAL_NOT_FOUND",
            retryable=True,
        )
    )

    state = successor.work.frontier.observation.metadata["popup_dismissal_reobserve_state"]
    assert state == {"signature": "h5-obstruction:" + "a" * 64, "count": 2, "streak": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation_depth", [0, 1])
async def test_popup_not_found_schedules_state_observation_without_claiming_absence(
    navigation_depth: int,
) -> None:
    """短命弹窗在点击前消失后必须先刷新页面事实，不能循环签发关闭。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result.update(
                {
                    "page_type": "video_list",
                    "page_state": "popup",
                    "candidate_actions": [
                        {
                            "action": "click",
                            "target": "关闭当前弹窗",
                            "target_keywords": [
                                "popup_dismissal",
                                "close_control",
                                "dialog_overlay",
                            ],
                            "risk_level": "low",
                            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                            "navigation_depth": 0,
                            "obstruction_capability": "dialog_overlay",
                            "obstruction_signature": "h5-obstruction:" + "a" * 64,
                        }
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(_command())
    before = _capture(
        "capture-popup-absent",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-popup",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code="POPUP_DISMISSAL_NOT_FOUND",
            retryable=True,
        )
    )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    stale = next(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SKIPPED
    )
    selected = next(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    assert stale.last_failure_reason == (
        "action_failure_reobserved:POPUP_DISMISSAL_NOT_FOUND"
    )
    assert selected.candidate.action_type is InteractionOperation.OBSERVE
    assert selected.candidate.semantic_target == "重新核对弹窗状态"
    assert selected.candidate.metadata["popup_absence_reobserve_capture_id"] == (
        before.capture_id
    )
    assert successor.work.frontier.candidates == (selected.candidate,)

    observe_candidate = replace(
        selected.candidate,
        navigation_depth=navigation_depth,
    )
    successor = replace(
        successor,
        work=replace(
            successor.work,
            frontier=replace(
                successor.work.frontier,
                candidates=(observe_candidate,),
            ),
        ),
    )

    observe_turn = ExplorerTurnResult(
        work_id=successor.work.work_id,
        task_id=successor.work.task_id,
        decision=ActionIntent(
            task_id=successor.work.task_id,
            session_id=successor.work.session_id,
            intent_id="intent-popup-absence-observe",
            idempotency_key="intent-popup-absence-observe",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=successor.work.source_capture_id,
            action_type=observe_candidate.action_type,
            semantic_target=observe_candidate.semantic_target,
            purpose=str(observe_candidate.metadata["purpose"]),
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=successor.work.deadline,
            parameters={"candidate_id": observe_candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-popup-observe-before",
            after="capture-popup-observe-after",
            source=successor.work.source_capture_id,
        ),
    )

    observed = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(
            prior_work=successor.work,
            turn=observe_turn,
        )
    )

    assert observed.work.frontier.observation.metadata[
        "popup_absence_reobserve_completed"
    ] is True
    assert observed.work.frontier.observation.metadata[
        "popup_absence_obstruction_signature"
    ] == "h5-obstruction:" + "a" * 64
    assert observed.work.frontier.observation.metadata[
        "popup_dismissal_reobserve_state"
    ] == {
        "signature": "h5-obstruction:" + "a" * 64,
        "count": 1,
        "streak": 1,
    }
    assert observed.work.frontier.observation.page_state == "popup"
    assert any(
        candidate.semantic_target == "关闭当前弹窗"
        for candidate in observed.work.frontier.candidates
    )


@pytest.mark.asyncio
async def test_retryable_action_reobservation_keeps_same_startup_candidate_occurrence() -> None:
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result.update(
                {
                    "page_type": "startup_ad",
                    "page_state": "countdown",
                    "candidate_actions": [
                        {
                            "action": "click",
                            "target": "跳过",
                            "target_keywords": [
                                "startup_ad",
                                "close_control",
                                "startup_ad_dismissal",
                            ],
                            "risk_level": "low",
                            "purpose": "关闭开屏广告并进入首页",
                            "expected_next_page_type": "home",
                            "navigation_depth": 0,
                            "obstruction_capability": "startup_ad_dismissal",
                            "obstruction_signature": "h5-obstruction:" + "2" * 64,
                        }
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(replace(_command(), sampling_policy_summary={}))
    before = _capture(
        "capture-startup-reobserve",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-startup",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code="STARTUP_AD_DISMISSAL_NOT_FOUND",
            retryable=True,
        )
    )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert [entry.status.value for entry in snapshot.entries] == ["skipped", "selected"]
    selected = snapshot.entries[-1]
    assert selected.candidate.semantic_target == "跳过"
    assert selected.candidate.metadata["action_reobservation_capture_id"] == before.capture_id
    assert successor.work.frontier.candidates == (selected.candidate,)
    assert successor.work.frontier.observation.metadata[
        "action_reobservation_failure_count"
    ] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_error_code", "second_error_code"),
    [
        (
            "VISUAL_TARGET_SOURCE_STATE_STALE",
            "VISUAL_TARGET_NOT_UNIQUE",
        ),
        (
            "VISUAL_TARGET_NOT_UNIQUE",
            "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
        ),
        (
            "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
            "VISUAL_TARGET_SOURCE_STATE_STALE",
        ),
    ],
)
async def test_visual_action_reobservation_is_bounded_to_one_new_capture(
    first_error_code: str,
    second_error_code: str,
) -> None:
    """同 session/source state 跨错误与候选共享一次检测前预算。"""
    module = _workflow_module()
    detector = _Detector()
    assessment_repository = _AssessmentRepository([])
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=assessment_repository,
        h5_visual_coordinate_mode="enforced",
        now=lambda: NOW + timedelta(seconds=5),
    )
    current = await workflow.run(replace(_command(), sampling_policy_summary={}))

    first_before = _capture(
        "capture-visual-reobserve-1",
        role="before_action",
        parent=current.work.source_capture_id,
        action_execution_id="action-visual-1",
    )
    current = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=current.work,
            before_capture=first_before,
            error_code=first_error_code,
            retryable=True,
        )
    )
    current = replace(
        current,
        work=replace(
            current.work,
            frontier=replace(
                current.work.frontier,
                observation=replace(
                    current.work.frontier.observation,
                    metadata={
                        **current.work.frontier.observation.metadata,
                        "action_reobservation_candidate_identity": {
                            "action_type": "click",
                            "semantic_target": "同状态 sibling",
                            "expected_next_page_type": "content_detail",
                        },
                    },
                ),
            ),
        ),
    )
    second_before = _capture(
        "capture-visual-reobserve-2",
        role="before_action",
        parent=current.work.source_capture_id,
        action_execution_id="action-visual-2",
    )

    with pytest.raises(ValueError, match="视觉动作重观察预算已耗尽"):
        await workflow.prepare_after_action_failure(
            module.ReobserveH5ExplorationCommand(
                prior_work=current.work,
                before_capture=second_before,
                error_code=second_error_code,
                retryable=True,
            )
        )

    assert detector.capture_ids == [
        "capture-initial",
        "capture-visual-reobserve-1",
    ]
    assert len(assessment_repository.calls) == 2


@pytest.mark.asyncio
async def test_profile_action_reobservation_stops_at_candidate_retry_limit() -> None:
    """同一 H5 候选到达重试上限后停止，不能依赖队列级连坐收口。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result.update(
                {
                    "page_type": "profile",
                    "candidate_actions": [
                        {
                            "action": "click",
                            "target": "个人中心会员支付入口",
                            "target_keywords": [
                                "profile_vip_payment_entry",
                                "profile_local_action",
                                "page_type:payment",
                                "entry:立即开通",
                                "evidence:structure",
                                "capability:profile_vip_center_image",
                            ],
                            "risk_level": "low",
                            "purpose": "探索我的页会员支付入口：立即开通",
                            "expected_next_page_type": "payment",
                            "navigation_depth": 1,
                        }
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    current = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for failure_count in range(1, 3):
        before = _capture(
            f"capture-profile-reobserve-{failure_count}",
            role="before_action",
            parent=current.work.source_capture_id,
            action_execution_id=f"action-profile-{failure_count}",
        )
        current = await workflow.prepare_after_action_failure(
            module.ReobserveH5ExplorationCommand(
                prior_work=current.work,
                before_capture=before,
                error_code="SEMANTIC_TARGET_NOT_FOUND",
                retryable=True,
            )
        )
        assert current.work.frontier.observation.metadata[
            "action_reobservation_failure_count"
        ] == failure_count

    assert current.work.frontier.candidates == ()
    metadata = current.work.frontier.observation.metadata
    assert metadata["bounded_exploration_scheduler"]["retry_attempts"] == {"discovery": 2}
    assert metadata["action_reobservation_exhausted"] is True
    assert metadata["h5_task_frontier_selected_entry_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("SEMANTIC_TARGET_AMBIGUOUS", H5FrontierEntryStatus.SKIPPED),
        ("COMMAND_TARGET_RISK_UNDERRATED", H5FrontierEntryStatus.BLOCKED),
        ("COMMAND_TARGET_SEMANTICS_UNTRUSTED", H5FrontierEntryStatus.BLOCKED),
    ],
)
async def test_terminal_candidate_failure_does_not_readd_same_semantic_candidate(
    error_code: str,
    expected_status: H5FrontierEntryStatus,
) -> None:
    """确定性的候选解析失败必须保留兄弟候选，且不得重新加入同一失败候选。"""
    module = _workflow_module()

    class Detector(_Detector):
        async def detect(self, *, capture, task_id, agent_run_id):
            result = await super().detect(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            result["page_url"] = "https://m.example.test/ai"
            result["page_type"] = "content_list"
            result["candidate_actions"] = [
                {
                    "action": "click",
                    "target": "AI图片换脸",
                    "target_keywords": ["AI图片换脸"],
                    "risk_level": "low",
                    "purpose": "探索图片换脸功能",
                    "expected_next_page_type": "content_detail",
                    "navigation_depth": 1,
                },
                {
                    "action": "click",
                    "target": "我的",
                    "target_keywords": ["个人中心"],
                    "risk_level": "low",
                    "purpose": "补齐个人中心覆盖",
                    "expected_next_page_type": "profile",
                    "navigation_depth": 1,
                },
            ]
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=5),
    )
    initial = await workflow.run(
        replace(
            _command(),
            sampling_policy_summary={
                "journey_steps": ["content_detail"],
                "include_page_types": ["content_list", "content_detail", "profile"],
                "exclude_page_types": [],
            },
        )
    )
    initial_snapshot = H5TaskFrontierSnapshot.from_dict(
        initial.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    selected = next(
        entry
        for entry in initial_snapshot.entries
        if entry.status is H5FrontierEntryStatus.SELECTED
    )
    assert selected.candidate.semantic_target == "AI图片换脸"
    before = _capture(
        "capture-ambiguous",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="action-ambiguous",
    )

    successor = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code=error_code,
            retryable=False,
        )
    )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    failed_identity_entries = [
        entry for entry in snapshot.entries if entry.candidate.semantic_target == "AI图片换脸"
    ]
    selected = next(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    assert [entry.status for entry in failed_identity_entries] == [expected_status]
    assert selected.candidate.semantic_target == "我的"
    assert successor.work.frontier.observation.metadata["action_reobservation_retryable"] is False
    assert (
        successor.work.frontier.observation.metadata["bounded_exploration_scheduler"][
            "no_progress_count"
        ]
        == 0
    )


def test_history_parent_metadata_propagates_across_same_location_popup_actions() -> None:
    """详情内弹窗不应抹掉浏览器历史中的直接列表来源。"""
    module = _workflow_module()
    prior = _work_for_history_parent_test()

    entered = module._history_parent_metadata(
        prior=prior,
        next_location="https://m.example.test/movie/1",
    )
    detail = replace(
        prior,
        frontier=replace(
            prior.frontier,
            observation=replace(
                prior.frontier.observation,
                location="https://m.example.test/movie/1",
                state_version="state:video-detail-popup",
                metadata={**dict(prior.frontier.observation.metadata), **entered},
            ),
        ),
    )

    assert entered == {
        "history_parent_state_version": prior.frontier.observation.state_version,
        "history_parent_location": prior.frontier.observation.location,
    }
    assert (
        module._history_parent_metadata(
            prior=detail,
            next_location="https://m.example.test/movie/1",
        )
        == entered
    )


def test_empty_child_frontier_prefers_history_parent_sibling_before_root() -> None:
    """详情无候选时应先返回列表，不得越级选择首页旧 sibling。"""
    module = _workflow_module()

    def candidate(candidate_id: str, target: str) -> CandidateAction:
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=target,
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=90,
            navigation_depth=1,
            expected_surface_kind=SurfaceKind.WEB,
        )

    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home",
            source_state_version="state:home",
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(candidate("candidate:root", "首页旧 sibling"),),
        )
        .merge(
            source_capture_id="capture-video-list",
            source_state_version="state:video-list",
            source_location="https://m.example.test/movie",
            navigation_depth=1,
            parent_state_version="state:home",
            candidates=(candidate("candidate:list", "列表下一个 sibling"),),
        )
    )

    selected = module._select_next_task_entry(
        snapshot,
        active_state_version="state:video-detail-empty",
        preferred_source_state_version="state:video-list",
        preferred_source_location="https://m.example.test/movie",
    )

    assert selected.pending_count == 1
    chosen = next(entry for entry in selected.entries if entry.status.value == "selected")
    assert chosen.candidate.candidate_id == "candidate:list"


def test_scheduler_action_limit_settles_pending_frontier_facts() -> None:
    """总动作预算耗尽后，不得保留无法执行的 pending 候选。"""
    module = _workflow_module()
    candidate = CandidateAction(
        candidate_id="candidate:home",
        semantic_target="首页",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"expected_next_page_type": "home"},
    )
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home"]},
    ).merge(
        source_capture_id="capture-root",
        source_state_version="state-root",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )

    settled = module._select_next_task_entry(
        snapshot,
        active_state_version="state-root",
        active_source_location="https://m.example.test/",
        active_page_type="home",
        active_observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        scheduler_state=module.BoundedExplorationState(
            max_actions=1,
            consumed_actions={"required": 1},
            required_page_types=("home",),
        ),
    )

    assert settled.pending_count == 0
    assert settled.blocked_count == 0
    assert {entry.status.value for entry in settled.entries} == {"skipped"}
    assert settled.entries[0].last_failure_reason == (
        "bounded_exploration:action_budget_exhausted"
    )


def test_scheduler_queue_retry_limit_does_not_reject_untried_h5_sibling() -> None:
    """同队列其它候选失败后，尚未尝试的 H5 入口仍应继续执行。"""
    module = _workflow_module()
    candidate = CandidateAction(
        candidate_id="candidate:home",
        semantic_target="首页",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"expected_next_page_type": "home"},
    )
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home"]},
    ).merge(
        source_capture_id="capture-root",
        source_state_version="state-root",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )

    selected = module._select_next_task_entry(
        snapshot,
        active_state_version="state-root",
        active_source_location="https://m.example.test/",
        active_page_type="home",
        active_observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        scheduler_state=module.BoundedExplorationState(
            max_actions=10,
            retry_attempts={"required": 2},
            required_page_types=("home",),
        ),
    )

    assert selected.pending_count == 0
    assert selected.entries[0].status.value == "selected"
    assert selected.entries[0].last_failure_reason is None


def test_returned_business_page_prefers_same_page_pending_candidate() -> None:
    """子页返回导致状态版本变化时，应继续当前业务页候选而非跳到旧 sibling。"""
    module = _workflow_module()

    def candidate(
        candidate_id: str,
        target: str,
        *,
        source_page_type: str,
        expected_page_type: str,
        risk: InteractionRiskLevel = InteractionRiskLevel.LOW,
    ) -> CandidateAction:
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=target,
            action_type=InteractionOperation.CLICK,
            risk_level=risk,
            exploration_value=96,
            navigation_depth=1,
            expected_surface_kind=SurfaceKind.WEB,
            metadata={
                "source_page_type": source_page_type,
                "expected_next_page_type": expected_page_type,
            },
        )

    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-video-list",
            source_state_version="state:video-list",
            source_location="https://m.example.test/movie",
            navigation_depth=1,
            candidates=(
                candidate(
                    "candidate:old-home",
                    "底部首页",
                    source_page_type="video_list",
                    expected_page_type="home",
                ),
            ),
        )
        .merge(
            source_capture_id="capture-profile-before-activity",
            source_state_version="state:profile-before-activity",
            source_location="https://m.example.test/mine",
            navigation_depth=1,
            candidates=(
                candidate(
                    "candidate:payment",
                    "立即开通",
                    source_page_type="profile",
                    expected_page_type="payment",
                    risk=InteractionRiskLevel.MEDIUM,
                ),
                candidate(
                    "candidate:settings",
                    "设置",
                    source_page_type="profile",
                    expected_page_type="settings",
                ),
            ),
            parent_state_version="state:video-list",
        )
    )

    selected = module._select_next_task_entry(
        snapshot,
        active_state_version="state:profile-after-activity-back",
        active_source_location="https://m.example.test/mine",
        active_page_type="profile",
    )

    chosen = next(entry for entry in selected.entries if entry.status.value == "selected")
    assert chosen.candidate.candidate_id == "candidate:settings"


def test_stale_profile_local_action_precedes_fresh_global_navigation() -> None:
    """设置入口本帧短暂漏检时，也不能先离开个人中心再导航根地址恢复。"""
    module = _workflow_module()
    settings = CandidateAction(
        candidate_id="candidate:settings-old",
        semantic_target="个人中心右上设置图标",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "profile",
            "source_page_state": "stable",
            "expected_next_page_type": "settings",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:structure",
            ],
        },
    )
    global_ai = CandidateAction(
        candidate_id="candidate:global-ai-current",
        semantic_target=(
            "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible"
        ),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "profile",
            "source_page_state": "stable",
            "expected_next_page_type": "content_list",
            "target_keywords": ["bottom_navigation", "ai", "content_list"],
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 4,
                "navigation_label": "ai",
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile-old",
        source_state_version="state:profile-old",
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        candidates=(settings,),
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-profile-current",
        source_state_version="state:profile-current",
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        parent_state_version="state:activity",
        candidates=(global_ai,),
    )

    selected = module._select_next_task_entry(
        snapshot,
        active_state_version="state:profile-current",
        active_source_location="https://m.example.test/mine",
        active_page_type="profile",
        active_page_state="stable",
        active_overlay_signature=None,
    )

    chosen = next(entry for entry in selected.entries if entry.status.value == "selected")
    assert chosen.candidate.candidate_id == "candidate:settings-old"


def test_obstructed_page_prefers_popup_dismissal_over_high_value_candidates() -> None:
    """弹窗态状态版本漂移后，同页清障关闭候选应先于高价值业务候选执行。"""
    module = _workflow_module()

    card = CandidateAction(
        candidate_id="candidate:card",
        semantic_target="第一张视频卡",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_list",
            "expected_next_page_type": "video_detail",
        },
    )
    dismissal = CandidateAction(
        candidate_id="candidate:close-popup",
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_list",
            "expected_next_page_type": None,
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                "dialog_active_ad_close",
            ],
            "obstruction_signature": "h5-obstruction:" + b"ad".hex().ljust(64, "0"),
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-movie-popup",
        source_state_version="state:movie-popup",
        source_location="https://m.example.test/movie",
        navigation_depth=1,
        candidates=(card, dismissal),
    )

    selected = module._select_next_task_entry(
        snapshot,
        active_state_version="state:movie-popup-next",
        active_source_location="https://m.example.test/movie",
        active_page_type="video_list",
        active_page_state="popup",
    )

    chosen = next(entry for entry in selected.entries if entry.status.value == "selected")
    assert chosen.candidate.candidate_id == "candidate:close-popup"


def test_popup_dismissal_accepts_revealed_business_page_type() -> None:
    """弹窗关闭后露出不同业务页时，动作仍应记为已执行。"""
    module = _workflow_module()
    candidate = CandidateAction(
        candidate_id="candidate:close-home-popup",
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "home",
            "target_keywords": ["popup_dismissal", "close_control", "dialog_overlay"],
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-popup",
        source_state_version="state:home-popup",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]
    selected = snapshot.select_entry(entry.entry_id, selected_at=NOW)

    assert (
        module._arrival_page_type_failure_reason(
            snapshot=selected,
            entry_id=entry.entry_id,
            actual_page_type="video_list",
        )
        is None
    )


def test_navigation_still_rejects_unexpected_page_type() -> None:
    """普通业务导航必须继续严格校验预期页型。"""
    module = _workflow_module()
    candidate = CandidateAction(
        candidate_id="candidate:open-detail",
        semantic_target="打开首个内容",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_list",
            "expected_next_page_type": "video_detail",
            "action_parameters": {"target_mode": "list_detail_navigation"},
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-video-list",
        source_state_version="state:video-list",
        source_location="https://m.example.test/videos",
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]
    selected = snapshot.select_entry(entry.entry_id, selected_at=NOW)

    assert module._arrival_page_type_failure_reason(
        snapshot=selected,
        entry_id=entry.entry_id,
        actual_page_type="video_list",
    ) == "arrival_page_type_mismatch:expected=video_detail:actual=video_list"


def test_stable_page_skips_stale_popup_candidate_before_selecting_sibling() -> None:
    """当前页已稳定且无遮罩时，旧弹窗候选不得触发根 URL 来源恢复。"""
    module = _workflow_module()
    stale_popup = CandidateAction(
        candidate_id="candidate:stale-popup",
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "target_keywords": ["popup_dismissal", "close_control", "dialog_overlay"],
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
            "obstruction_signature": "h5-obstruction:" + "a" * 64,
            "expected_next_page_type": None,
        },
    )
    sibling = CandidateAction(
        candidate_id="candidate:content-list",
        semantic_target="内容",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "content_list",
        },
    )
    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home-popup",
            source_state_version="state:home-popup",
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(stale_popup,),
        )
        .merge(
            source_capture_id="capture-home-before-detail",
            source_state_version="state:home-before-detail",
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(sibling,),
        )
    )

    selected = module._select_next_task_entry(
        snapshot,
        active_state_version="state:home-returned-stable",
        active_source_location="https://m.example.test/",
        active_page_type="home",
        active_page_state="stable",
        active_overlay_signature=None,
    )

    stale_entry = next(
        entry
        for entry in selected.entries
        if entry.candidate.candidate_id == stale_popup.candidate_id
    )
    chosen = next(entry for entry in selected.entries if entry.status.value == "selected")
    assert stale_entry.status.value == "skipped"
    assert stale_entry.last_failure_reason == "popup_obstruction_absent_on_stable_page"
    assert chosen.candidate.candidate_id == sibling.candidate_id


def _work_for_history_parent_test() -> ExplorationWork:
    from services.api.app.application.orchestration import StartExplorationCommand
    from services.api.app.domain.exploration import FrontierSnapshot, SurfaceObservation

    return prepare_exploration_work(
        StartExplorationCommand(
            task_id="task-h5",
            session_id="h5:run-h5",
            branch_id="h5:capture-video-list",
            objective="探索视频详情",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="capture-video-list",
            frontier_snapshot_id="snapshot-video-list",
            frontier=FrontierSnapshot(
                snapshot_id="snapshot-video-list",
                observation=SurfaceObservation(
                    surface_kind=SurfaceKind.WEB,
                    capture_id="capture-video-list",
                    page_type="video_list",
                    page_state="stable",
                    location="https://m.example.test/movie",
                    state_version="state:video-list",
                    observed_at=NOW,
                ),
                candidates=(),
            ),
            boundaries=ExplorationBoundaries(
                allowed_hosts=("m.example.test",),
                allowed_operations=(InteractionOperation.CLICK, InteractionOperation.BACK),
                max_navigation_depth=4,
                side_effect_policy=SideEffectPolicy.READ_ONLY,
            ),
            budget=ExplorationBudget(
                max_actions=8,
                remaining_actions=8,
                max_depth=4,
                remaining_duration_ms=120_000,
            ),
            deadline=NOW + timedelta(minutes=3),
        )
    )


@pytest.mark.asyncio
async def test_task_frontier_never_completes_with_pending_blocked_failed_or_auth_entries() -> None:
    """任务级 Frontier 只在所有可达项成功耗尽时才允许完成。"""

    module = _workflow_module()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    initial = await workflow.run(_command())
    frontier = initial.work.frontier
    base = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=initial.work.source_capture_id,
        source_state_version=frontier.observation.state_version,
        source_location=frontier.observation.location,
        navigation_depth=0,
        candidates=frontier.candidates,
    )
    entry_id = base.pending_entries[0].entry_id
    failed = base.select_entry(entry_id).mark_failed(
        entry_id,
        reason="state_equivalence_failed",
    )
    executed = base.select_entry(entry_id).mark_executed(entry_id)
    blocked = H5TaskFrontierSnapshot.create(
        max_pages=None,
        sampling_policy_summary={
            "include_page_types": ["payment"],
            "exclude_page_types": [],
            "journey_steps": [],
        },
    ).merge(
        source_capture_id=initial.work.source_capture_id,
        source_state_version=frontier.observation.state_version,
        source_location=frontier.observation.location,
        navigation_depth=0,
        candidates=frontier.candidates,
    )
    auth_frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={**frontier.observation.metadata, "requires_auth": True},
        ),
    )
    popup_frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            page_state="popup",
            overlay_signature="h5-overlay:popup-state",
            metadata={
                **frontier.observation.metadata,
                "objective_satisfied": False,
            },
        ),
    )

    cases = (
        ("pending", base, frontier),
        ("blocked", blocked, frontier),
        ("failed", failed, frontier),
        ("requires_auth", executed, auth_frontier),
        ("popup", executed, popup_frontier),
    )
    for reason, snapshot, current_frontier in cases:
        projected = module._with_task_frontier(
            frontier=current_frontier,
            snapshot=snapshot,
        )
        assert projected.observation.metadata["objective_satisfied"] is False, reason


def test_task_frontier_projection_drops_blocked_ids_outside_selected_candidate() -> None:
    """来源页重观察可发现新的高风险候选，但局部 Work 只投影全局 selected。"""
    module = _workflow_module()
    frontier = module.build_h5_frontier(
        detection_result=_detection("capture-profile"),
        observed_at=NOW,
        navigation_depth=1,
    )
    selected_candidate = frontier.candidates[0]
    newly_blocked_candidate = replace(
        selected_candidate,
        candidate_id="h5-candidate:newly-blocked-on-restore",
        semantic_target="立即充值",
        risk_level=InteractionRiskLevel.HIGH,
    )
    refreshed_frontier = replace(
        frontier,
        candidates=(selected_candidate, newly_blocked_candidate),
        blocked_candidate_ids=(
            selected_candidate.candidate_id,
            newly_blocked_candidate.candidate_id,
        ),
        visited_candidate_ids=(
            selected_candidate.candidate_id,
            newly_blocked_candidate.candidate_id,
        ),
    )
    task_frontier = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile-original",
        source_state_version="state:profile-original",
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        candidates=(selected_candidate,),
    )
    task_frontier = task_frontier.select_entry(task_frontier.pending_entries[0].entry_id)

    projected = module._with_task_frontier(
        frontier=refreshed_frontier,
        snapshot=task_frontier,
    )

    assert projected.candidates == (selected_candidate,)
    assert projected.blocked_candidate_ids == (selected_candidate.candidate_id,)
    assert projected.visited_candidate_ids == (selected_candidate.candidate_id,)


@pytest.mark.asyncio
async def test_after_action_preserves_parent_sibling_and_serializes_task_frontier() -> None:
    """如果移除全局快照合并，后继 Work 会错误地丢失购物车 sibling。"""

    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            result = _detection(capture.capture_id)
            if capture.capture_id == "capture-initial":
                result["candidate_actions"] = [
                    {
                        "action": "click",
                        "target": "商品",
                        "risk_level": "low",
                        "purpose": "进入商品详情",
                        "expected_next_page_type": "product_detail",
                        "navigation_depth": 1,
                    },
                    {
                        "action": "click",
                        "target": "购物车",
                        "risk_level": "low",
                        "purpose": "进入购物车",
                        "expected_next_page_type": "cart",
                        "navigation_depth": 1,
                    },
                ]
            else:
                result["page_url"] = "https://m.example.test/product/1"
                result["page_type"] = "product_detail"
                result["matched_catalog_signals"] = {
                    "ocr_keywords": ["商品详情"],
                    "dom_hints": ["product"],
                    "layout_hints": ["product-detail"],
                }
                result["candidate_actions"] = [
                    {
                        "action": "click",
                        "target": "加入收藏",
                        "risk_level": "low",
                        "purpose": "覆盖商品详情子分支",
                        "expected_next_page_type": "favorite",
                        "navigation_depth": 2,
                    }
                ]
            return result

    orchestrator = _Orchestrator()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=orchestrator,
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    initial = await workflow.run(replace(_command(), sampling_policy_summary={}))
    selected_candidate = initial.work.frontier.candidates[0]
    intent = ActionIntent(
        task_id=initial.work.task_id,
        session_id=initial.work.session_id,
        intent_id="intent-product",
        idempotency_key="intent-product",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=initial.work.source_capture_id,
        action_type=selected_candidate.action_type,
        semantic_target=selected_candidate.semantic_target,
        purpose="进入商品详情",
        capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
        deadline=initial.work.deadline,
        parameters={"candidate_id": selected_candidate.candidate_id},
        risk_level=InteractionRiskLevel.LOW,
    )
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=intent,
        coordination=_coordination(
            before="capture-before-product",
            after="capture-product",
            source=initial.work.source_capture_id,
        ),
    )

    result = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )

    restored_work = ExplorationWork.from_dict(result.work.to_dict())
    snapshot = H5TaskFrontierSnapshot.from_dict(
        restored_work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert [entry.candidate.semantic_target for entry in snapshot.pending_entries] == ["购物车"]
    assert restored_work.frontier.candidates[0].semantic_target == "加入收藏"
    assert restored_work.frontier.observation.metadata["objective_satisfied"] is False


@pytest.mark.asyncio
async def test_uncatalogued_landing_is_skipped_and_parent_sibling_remains_selected() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            if capture.capture_id == "capture-initial":
                result["candidate_actions"] = [
                    {
                        "action": "click",
                        "target": "实验功能",
                        "risk_level": "low",
                        "purpose": "验证目录外落地页",
                        "expected_next_page_type": None,
                        "navigation_depth": 1,
                    },
                    {
                        "action": "click",
                        "target": "我的",
                        # 覆盖优先策略会先选带规范页型的低风险候选；这里用
                        # medium 显式保证本用例先验证目录外落地，再验证 sibling。
                        "risk_level": "medium",
                        "purpose": "验证父页 sibling 续跑",
                        "expected_next_page_type": "profile",
                        "navigation_depth": 1,
                    },
                ]
            else:
                result.update(
                    {
                        "page_url": "https://m.example.test/experimental-feature",
                        "detection_status": "unrecognized",
                        "page_type": None,
                        "candidate_actions": [],
                        "catalog_disposition": "skipped",
                        "catalog_skip_reason": "uncatalogued_page_type",
                    }
                )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    initial = await workflow.run(replace(_command(), sampling_policy_summary={}))
    selected_candidate = initial.work.frontier.candidates[0]
    assert selected_candidate.semantic_target == "实验功能"
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=ActionIntent(
            task_id=initial.work.task_id,
            session_id=initial.work.session_id,
            intent_id="intent-uncatalogued",
            idempotency_key="intent-uncatalogued",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=initial.work.source_capture_id,
            action_type=selected_candidate.action_type,
            semantic_target=selected_candidate.semantic_target,
            purpose="验证目录外落地页",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=initial.work.deadline,
            parameters={"candidate_id": selected_candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-uncatalogued",
            after="capture-uncatalogued",
            source=initial.work.source_capture_id,
        ),
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )
    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    skipped = next(
        entry
        for entry in snapshot.entries
        if entry.candidate.candidate_id == selected_candidate.candidate_id
    )
    selected = next(entry for entry in snapshot.entries if entry.status.value == "selected")

    assert skipped.status.value == "skipped"
    assert skipped.last_failure_reason == "uncatalogued_page_type"
    assert selected.candidate.semantic_target == "我的"
    assert successor.work.frontier.candidates == (selected.candidate,)
    assert successor.page_type_assessment.execution_status == "skipped"


@pytest.mark.asyncio
async def test_line_loading_reobserves_three_times_then_fails_selected_entry() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            result.update(
                {
                    "page_type": "line_loading",
                    "page_state": "line_loading",
                    "candidate_actions": [],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    result = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for attempt in range(1, 4):
        prior = result.work
        candidate = prior.frontier.candidates[0]
        turn = ExplorerTurnResult(
            work_id=prior.work_id,
            task_id=prior.task_id,
            decision=ActionIntent(
                task_id=prior.task_id,
                session_id=prior.session_id,
                intent_id=f"intent-line-{attempt}",
                idempotency_key=f"intent-line-{attempt}",
                surface_kind=SurfaceKind.WEB,
                source_capture_id=prior.source_capture_id,
                action_type=candidate.action_type,
                semantic_target=candidate.semantic_target,
                purpose="等待线路加载完成后同会话重观察",
                capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                deadline=prior.deadline,
                parameters={"candidate_id": candidate.candidate_id},
                risk_level=InteractionRiskLevel.LOW,
            ),
            coordination=_coordination(
                before=f"capture-before-line-{attempt}",
                after=f"capture-line-{attempt}",
                source=prior.source_capture_id,
            ),
        )
        result = await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
        )
        assert result.work.session_id == prior.session_id
        assert result.work.frontier.observation.metadata["line_loading_reobserve_count"] == attempt
        if attempt < 3:
            assert len(result.work.frontier.candidates) == 1
            assert (
                result.work.frontier.candidates[0].metadata["line_loading_reobserve_attempt"]
                == attempt + 1
            )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert result.work.frontier.candidates == ()
    assert snapshot.failed_count == 1
    assert snapshot.entries[-1].last_failure_reason == ("line_loading_reobserve_limit_exceeded")
    assert result.work.frontier.observation.metadata["objective_satisfied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("page_state", ["countdown", "stable"])
async def test_startup_ad_countdown_reobserves_ten_times_then_fails_selected_entry(
    page_state: str,
) -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            result.update(
                {
                    "page_type": "startup_ad",
                    "page_state": page_state,
                    "candidate_actions": [],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    result = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for attempt in range(1, 11):
        prior = result.work
        candidate = prior.frontier.candidates[0]
        turn = ExplorerTurnResult(
            work_id=prior.work_id,
            task_id=prior.task_id,
            decision=ActionIntent(
                task_id=prior.task_id,
                session_id=prior.session_id,
                intent_id=f"intent-countdown-{attempt}",
                idempotency_key=f"intent-countdown-{attempt}",
                surface_kind=SurfaceKind.WEB,
                source_capture_id=prior.source_capture_id,
                action_type=candidate.action_type,
                semantic_target=candidate.semantic_target,
                purpose="等待启动广告倒计时结束后同会话重观察",
                capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                deadline=prior.deadline,
                parameters={"candidate_id": candidate.candidate_id},
                risk_level=InteractionRiskLevel.LOW,
            ),
            coordination=_coordination(
                before=f"capture-before-countdown-{attempt}",
                after=f"capture-countdown-{attempt}",
                source=prior.source_capture_id,
            ),
        )
        result = await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
        )
        assert result.work.session_id == prior.session_id
        assert (
            result.work.frontier.observation.metadata[
                "startup_ad_countdown_reobserve_count"
            ]
            == attempt
        )
        if attempt < 10:
            assert len(result.work.frontier.candidates) == 1
            assert (
                result.work.frontier.candidates[0].metadata[
                    "startup_ad_countdown_reobserve_attempt"
                ]
                == attempt + 1
            )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert result.work.frontier.candidates == ()
    assert snapshot.failed_count == 1
    assert snapshot.entries[-1].last_failure_reason == (
        "startup_ad_countdown_reobserve_limit_exceeded"
    )
    assert result.work.frontier.observation.metadata["objective_satisfied"] is False


@pytest.mark.asyncio
async def test_empty_flutter_bootstrap_reobserves_five_times_then_fails_selected_entry() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            result.update(
                {
                    "detection_status": "insufficient",
                    "page_type": None,
                    "page_state": "needs_more_observation",
                    "candidate_actions": [],
                    "risk_flags": ["deterministic_flutter_bootstrap_pending"],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    result = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for attempt in range(1, 6):
        prior = result.work
        candidate = prior.frontier.candidates[0]
        turn = ExplorerTurnResult(
            work_id=prior.work_id,
            task_id=prior.task_id,
            decision=ActionIntent(
                task_id=prior.task_id,
                session_id=prior.session_id,
                intent_id=f"intent-bootstrap-{attempt}",
                idempotency_key=f"intent-bootstrap-{attempt}",
                surface_kind=SurfaceKind.WEB,
                source_capture_id=prior.source_capture_id,
                action_type=candidate.action_type,
                semantic_target=candidate.semantic_target,
                purpose="等待 Flutter 启动完成后同会话重观察",
                capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                deadline=prior.deadline,
                parameters={"candidate_id": candidate.candidate_id},
                risk_level=InteractionRiskLevel.LOW,
            ),
            coordination=_coordination(
                before=f"capture-before-bootstrap-{attempt}",
                after=f"capture-bootstrap-{attempt}",
                source=prior.source_capture_id,
            ),
        )
        result = await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
        )
        assert result.work.session_id == prior.session_id
        assert (
            result.work.frontier.observation.metadata[
                "flutter_bootstrap_reobserve_count"
            ]
            == attempt
        )
        if attempt < 5:
            assert (
                result.work.frontier.candidates[0].metadata[
                    "flutter_bootstrap_reobserve_attempt"
                ]
                == attempt + 1
            )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert result.work.frontier.candidates == ()
    assert snapshot.failed_count == 1
    assert snapshot.entries[-1].last_failure_reason == (
        "flutter_bootstrap_reobserve_limit_exceeded"
    )
    assert result.work.frontier.observation.metadata["objective_satisfied"] is False


@pytest.mark.asyncio
async def test_insufficient_unknown_page_reobserves_three_times_then_fails_selected_entry() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            result.update(
                {
                    "detection_status": "insufficient",
                    "page_type": None,
                    "page_state": "needs_more_observation",
                    "candidate_actions": [],
                    "risk_flags": [],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    result = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for attempt in range(1, 4):
        prior = result.work
        candidate = prior.frontier.candidates[0]
        turn = ExplorerTurnResult(
            work_id=prior.work_id,
            task_id=prior.task_id,
            decision=ActionIntent(
                task_id=prior.task_id,
                session_id=prior.session_id,
                intent_id=f"intent-insufficient-{attempt}",
                idempotency_key=f"intent-insufficient-{attempt}",
                surface_kind=SurfaceKind.WEB,
                source_capture_id=prior.source_capture_id,
                action_type=candidate.action_type,
                semantic_target=candidate.semantic_target,
                purpose="等待页面稳定后同会话重观察",
                capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                deadline=prior.deadline,
                parameters={"candidate_id": candidate.candidate_id},
                risk_level=InteractionRiskLevel.LOW,
            ),
            coordination=_coordination(
                before=f"capture-before-insufficient-{attempt}",
                after=f"capture-insufficient-{attempt}",
                source=prior.source_capture_id,
            ),
        )
        result = await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
        )
        assert result.work.session_id == prior.session_id
        assert (
            result.work.frontier.observation.metadata["insufficient_reobserve_count"]
            == attempt
        )
        if attempt < 3:
            assert (
                result.work.frontier.candidates[0].metadata[
                    "insufficient_reobserve_attempt"
                ]
                == attempt + 1
            )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert result.work.frontier.candidates == ()
    assert snapshot.failed_count == 1
    assert snapshot.entries[-1].last_failure_reason == (
        "insufficient_reobserve_limit_exceeded"
    )
    assert result.work.frontier.observation.metadata["objective_satisfied"] is False


@pytest.mark.asyncio
async def test_ambiguous_launch_shell_reobserves_three_times_then_fails_selected_entry() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            result.update(
                {
                    "detection_status": "ambiguous",
                    "page_type": None,
                    "page_state": "loading",
                    "candidate_actions": [],
                    "risk_flags": [],
                    "metrics": {
                        "page_settle_complete": True,
                        "h5_business_structure_present": False,
                    },
                    "matched_catalog_signals": {
                        "ocr_keywords": ["品牌启动图"],
                        "dom_hints": ["splash", "startup"],
                        "layout_hints": ["全屏启动宣传图"],
                    },
                    "page_type_candidates": [
                        {"page_type": "line_loading", "confidence": 0.56},
                        {"page_type": "startup_ad", "confidence": 0.48},
                    ],
                }
            )
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    result = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for attempt in range(1, 4):
        prior = result.work
        candidate = prior.frontier.candidates[0]
        turn = ExplorerTurnResult(
            work_id=prior.work_id,
            task_id=prior.task_id,
            decision=ActionIntent(
                task_id=prior.task_id,
                session_id=prior.session_id,
                intent_id=f"intent-ambiguous-launch-{attempt}",
                idempotency_key=f"intent-ambiguous-launch-{attempt}",
                surface_kind=SurfaceKind.WEB,
                source_capture_id=prior.source_capture_id,
                action_type=candidate.action_type,
                semantic_target=candidate.semantic_target,
                purpose="零触碰复观察启动壳层",
                capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                deadline=prior.deadline,
                parameters={"candidate_id": candidate.candidate_id},
                risk_level=InteractionRiskLevel.LOW,
            ),
            coordination=_coordination(
                before=f"capture-before-ambiguous-launch-{attempt}",
                after=f"capture-ambiguous-launch-{attempt}",
                source=prior.source_capture_id,
            ),
        )
        result = await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
        )
        assert (
            result.work.frontier.observation.metadata[
                "ambiguous_launch_shell_reobserve_count"
            ]
            == attempt
        )
        if attempt < 3:
            assert (
                result.work.frontier.candidates[0].metadata[
                    "ambiguous_launch_shell_reobserve_attempt"
                ]
                == attempt + 1
            )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert result.work.frontier.candidates == ()
    assert snapshot.failed_count == 1
    assert snapshot.entries[-1].last_failure_reason == (
        "ambiguous_launch_shell_reobserve_limit_exceeded"
    )
    assert result.work.frontier.observation.metadata["objective_satisfied"] is False


@pytest.mark.asyncio
async def test_uncertain_logical_transition_reobserves_twice_then_skips_entry() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            return _logical_detection(
                capture.capture_id,
                transition_proof="uncertain",
            )

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        logical_surface_effective=True,
        now=lambda: NOW,
    )
    result = await workflow.run(replace(_command(), sampling_policy_summary={}))

    for attempt in range(1, 3):
        prior = result.work
        assert prior.frontier.candidates
        candidate = prior.frontier.candidates[0]
        assert candidate.metadata["purpose"] == "复验 SPA/Flutter 逻辑页面转换"
        turn = ExplorerTurnResult(
            work_id=prior.work_id,
            task_id=prior.task_id,
            decision=ActionIntent(
                task_id=prior.task_id,
                session_id=prior.session_id,
                intent_id=f"intent-logical-{attempt}",
                idempotency_key=f"intent-logical-{attempt}",
                surface_kind=SurfaceKind.WEB,
                source_capture_id=prior.source_capture_id,
                action_type=candidate.action_type,
                semantic_target=candidate.semantic_target,
                purpose="复验 SPA/Flutter 逻辑页面转换",
                capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
                deadline=prior.deadline,
                parameters={"candidate_id": candidate.candidate_id},
                risk_level=InteractionRiskLevel.LOW,
            ),
            coordination=_coordination(
                before=f"capture-before-logical-{attempt}",
                after=f"capture-logical-{attempt}",
                source=prior.source_capture_id,
            ),
        )
        result = await workflow.prepare_after_action(
            module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
        )
        assert result.work.frontier.observation.metadata["logical_reobserve_count"] == (
            attempt
        )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert result.work.frontier.candidates == ()
    assert result.work.frontier.observation.metadata["logical_reobserve_exhausted"] is True
    assert snapshot.failed_count == 0
    assert snapshot.covered_page_count == 1
    assert snapshot.covered_page_types == frozenset({"home"})
    assert snapshot.entries[-1].last_failure_reason == "h5_logical_transition_unstable"
    assert result.work.frontier.observation.metadata["objective_satisfied"] is False


def test_logical_reobserve_exhaustion_recognizes_existing_covered_scope() -> None:
    module = _workflow_module()
    from services.api.app.domain.exploration.h5_coverage import (
        H5_COVERAGE_METADATA_KEY,
        H5CoverageSnapshot,
    )

    task_frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["profile"]},
    )
    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=task_frontier,
        location="https://example.test/mine",
        capture_id="capture-covered",
        page_type="profile",
        page_state="stable",
        assessment={
            "execution_status": "success",
            "page_type_assessment": {"status": "covered"},
        },
        requires_auth=False,
        overlay_present=False,
    )
    prior = SimpleNamespace(
        frontier=SimpleNamespace(
            observation=SimpleNamespace(
                metadata={H5_COVERAGE_METADATA_KEY: coverage.to_dict()}
            )
        )
    )
    current = SimpleNamespace(
        observation=SimpleNamespace(
            location="https://example.test/mine",
            page_type="profile",
            page_state="stable",
        )
    )

    assert module._observed_scope_was_already_covered(prior=prior, frontier=current) is True


@pytest.mark.asyncio
async def test_unverified_logical_navigation_skips_candidate_without_failing_frontier() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            return _logical_detection(capture.capture_id, transition_proof="trusted")

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        logical_surface_effective=True,
        logical_targets_effective=True,
        now=lambda: NOW,
    )
    prior = (await workflow.run(replace(_command(), sampling_policy_summary={}))).work
    candidate = prior.frontier.candidates[0]
    turn = ExplorerTurnResult(
        work_id=prior.work_id,
        task_id=prior.task_id,
        decision=ActionIntent(
            task_id=prior.task_id,
            session_id=prior.session_id,
            intent_id="intent-logical-transition-unverified",
            idempotency_key="intent-logical-transition-unverified",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=prior.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose="验证逻辑导航失败收口",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=prior.deadline,
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-logical-unverified",
            after="capture-logical-unverified",
            source=prior.source_capture_id,
            status=InteractionResultStatus.BLOCKED,
            error_code="LOGICAL_TRANSITION_UNVERIFIED",
        ),
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=prior, turn=turn)
    )

    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert snapshot.failed_count == 0
    assert snapshot.covered_page_count == 1
    skipped_entry = next(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SKIPPED
    )
    assert skipped_entry.last_failure_reason == "h5_logical_transition_unverified"


def test_next_depth_uses_selected_candidate_absolute_target_depth() -> None:
    from types import SimpleNamespace

    from services.api.app.domain.exploration import CandidateAction, FrontierSnapshot

    module = _workflow_module()
    detection = _detection("capture-depth")
    frontier = import_module(
        "services.api.app.application.h5_exploration_observation"
    ).build_h5_frontier(
        detection_result=detection,
        observed_at=NOW,
        navigation_depth=2,
    )

    def _depth_for(operation: InteractionOperation) -> int:
        expected_depth = {
            InteractionOperation.BACK: 1,
            InteractionOperation.SCROLL: 2,
            InteractionOperation.OBSERVE: 2,
            InteractionOperation.CLICK: 3,
        }[operation]
        candidate = CandidateAction(
            candidate_id=f"candidate:{operation.value}",
            semantic_target=operation.value,
            action_type=operation,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=50,
            navigation_depth=expected_depth,
            expected_surface_kind=SurfaceKind.WEB,
        )
        work_frontier = FrontierSnapshot(
            snapshot_id=f"frontier:{operation.value}",
            observation=frontier.observation,
            candidates=(candidate,),
        )
        work = SimpleNamespace(frontier=work_frontier)
        intent = ActionIntent(
            task_id="task-h5",
            session_id="h5:run-h5",
            intent_id=f"intent:{operation.value}",
            idempotency_key=f"intent:{operation.value}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="capture-depth",
            action_type=operation,
            semantic_target=operation.value,
            purpose="验证导航深度",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=NOW + timedelta(minutes=1),
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        )
        turn = ExplorerTurnResult(
            work_id="work-depth",
            task_id="task-h5",
            decision=intent,
            coordination=_coordination(
                before=f"before-{operation.value}",
                after=f"after-{operation.value}",
                source="capture-depth",
            ),
        )
        return module._next_depth(work, turn)

    assert _depth_for(InteractionOperation.BACK) == 1
    assert _depth_for(InteractionOperation.SCROLL) == 2
    assert _depth_for(InteractionOperation.OBSERVE) == 2
    assert _depth_for(InteractionOperation.CLICK) == 3


@pytest.mark.asyncio
async def test_surface_bootstrapper_registers_session_and_uses_dual_capture_navigation() -> None:
    module = _workflow_module()
    calls: dict[str, object] = {"events": []}

    def record_event(name: str) -> None:
        events = calls["events"]
        assert isinstance(events, list)
        events.append(name)

    class Driver:
        async def start_session(self, spec):
            record_event("driver_start")
            calls["spec"] = spec
            return SurfaceSessionRef(
                task_id=spec.task_id,
                session_id=spec.session_id,
                surface_kind=spec.surface_kind,
                primary_surface_ref="web-primary:h5:run-h5",
            )

    class Sessions:
        async def register_active(self, *, spec, session_owner, primary_surface_ref):
            record_event("session_register")
            calls["registration"] = (spec, session_owner, primary_surface_ref)

    class Coordinator:
        async def coordinate(self, command):
            record_event("coordinator")
            calls["coordination_command"] = command
            return _coordination(before="capture-anchor", after="capture-initial", source="entry")

    bootstrapper = module.H5SurfaceBootstrapper(
        driver=Driver(),
        surface_sessions=Sessions(),
        capture_coordinator=Coordinator(),
    )

    result = await bootstrapper.start(_command())

    assert calls["events"] == ["driver_start", "session_register", "coordinator"]
    spec, owner, primary = calls["registration"]
    assert spec.surface_kind is SurfaceKind.WEB
    assert spec.options["viewport_width"] == 390
    assert spec.options["viewport_height"] == 844
    assert owner == "worker-1"
    assert primary == "web-primary:h5:run-h5"
    intent = calls["coordination_command"].intent
    assert intent.action_type is InteractionOperation.NAVIGATE
    assert intent.parameters["destination_url"] == "https://m.example.test/home"
    assert intent.parameters["initial_surface_action"] is True
    assert intent.parameters["viewport_width"] == 390
    assert intent.parameters["viewport_height"] == 844
    assert intent.capture_profile.before.artifact_types == (
        "screenshot",
        "dom",
        "visible_text",
        "ui_snapshot",
    )
    assert intent.capture_profile.after.artifact_types == (
        "screenshot",
        "dom",
        "visible_text",
        "ui_snapshot",
        "action_trace",
    )
    assert result.after_capture.capture_id == "capture-initial"


@pytest.mark.asyncio
async def test_auth_landing_remains_blocked_after_switching_to_sibling() -> None:
    module = _workflow_module()

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            result = _detection(capture.capture_id)
            if capture.capture_id == "capture-initial":
                result["candidate_actions"] = [
                    {
                        "action": "click",
                        "target": "需要登录的入口",
                        "risk_level": "low",
                        "purpose": "验证鉴权分支",
                        "expected_next_page_type": "profile",
                        "navigation_depth": 1,
                    },
                    {
                        "action": "click",
                        "target": "公开内容入口",
                        "risk_level": "low",
                        "purpose": "验证公开 sibling",
                        "expected_next_page_type": "product_detail",
                        "navigation_depth": 1,
                    },
                ]
            else:
                result["page_state"] = "login_required"
                result["candidate_actions"] = []
            return result

    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW,
    )
    initial = await workflow.run(replace(_command(), sampling_policy_summary={}))
    selected_candidate = initial.work.frontier.candidates[0]
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=ActionIntent(
            task_id=initial.work.task_id,
            session_id=initial.work.session_id,
            intent_id="intent-auth",
            idempotency_key="intent-auth",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=initial.work.source_capture_id,
            action_type=selected_candidate.action_type,
            semantic_target=selected_candidate.semantic_target,
            purpose="验证鉴权分支",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=initial.work.deadline,
            parameters={"candidate_id": selected_candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-auth",
            after="capture-auth",
            source=initial.work.source_capture_id,
        ),
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )
    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    auth_entry = next(
        entry
        for entry in snapshot.entries
        if entry.candidate.candidate_id == selected_candidate.candidate_id
    )
    assert auth_entry.status.value == "blocked"
    assert auth_entry.last_failure_reason == "authentication_required"

    sibling = next(entry for entry in snapshot.entries if entry.status.value == "selected")
    exhausted = snapshot.mark_executed(sibling.entry_id, finished_at=NOW)
    sibling_leaf = replace(
        successor.work.frontier,
        observation=replace(
            successor.work.frontier.observation,
            metadata={
                **successor.work.frontier.observation.metadata,
                "requires_auth": False,
            },
        ),
    )
    projected = module._with_task_frontier(frontier=sibling_leaf, snapshot=exhausted)

    assert exhausted.blocked_count == 1
    assert projected.observation.metadata["objective_satisfied"] is False
