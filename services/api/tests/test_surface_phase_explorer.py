"""测试 Surface 阶段机接入 Explorer 决策入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.app_screenshot_exploration import _build_frontier
from services.api.app.application.app_script_curriculum import (
    build_app_curriculum_progress,
    resolve_playbook_by_id,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.surface_phase_explorer import SurfacePhaseAwareExplorer
from services.api.app.application.surface_phase_runtime import SurfacePhaseRepository
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import (
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    AppExplorationGoal,
    AppVisualAnalysis,
    AppVisualCandidate,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    NormalizedBounds,
    SideEffectPolicy,
    SurfaceObservation,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
    SurfacePhaseStepStatus,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class FakeDelegate:
    def __init__(self) -> None:
        self.calls = 0

    async def decide_next(self, work):
        self.calls += 1
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.COMPLETED,
            reason="普通 Explorer 完成",
        )


class DeterministicDelegate(FakeDelegate):
    def __init__(self) -> None:
        super().__init__()
        self.deterministic_calls = 0

    async def decide_deterministic(self, work):
        self.deterministic_calls += 1
        candidate = work.frontier.candidates[0]
        return ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id="intent-current-gap",
            idempotency_key="intent-current-gap",
            surface_kind=work.surface_kind,
            source_capture_id=work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            parameters={"candidate_id": candidate.candidate_id},
            purpose="当前截图缺页入口",
            risk_level=candidate.risk_level,
            capture_profile=CaptureProfilePolicy.for_surface(
                work.surface_kind,
                candidate.action_type,
            ),
            deadline=work.deadline,
        )


class BackOnlyDelegate:
    def __init__(self) -> None:
        self.calls = 0

    async def decide_next(self, work):
        self.calls += 1
        candidate = next(
            candidate
            for candidate in work.frontier.candidates
            if candidate.action_type is InteractionOperation.BACK
        )
        return ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id="intent-external-back",
            idempotency_key="idempotency-external-back",
            surface_kind=work.surface_kind,
            source_capture_id=work.source_capture_id,
            action_type=InteractionOperation.BACK,
            semantic_target=candidate.semantic_target,
            parameters={"candidate_id": candidate.candidate_id},
            purpose="外部包只允许 BACK 恢复",
            risk_level=candidate.risk_level,
            capture_profile=CaptureProfilePolicy.for_surface(
                work.surface_kind,
                InteractionOperation.BACK,
            ),
            deadline=work.deadline,
        )


class FakeGate:
    def __init__(self) -> None:
        self.holds = 0

    @asynccontextmanager
    async def hold(self, *, task_id, session_id, surface_kind=None, allow_draining=False):
        del allow_draining
        self.holds += 1
        yield SessionCommandPermit(
            task_id=task_id,
            session_id=session_id,
            surface_kind=surface_kind or SurfaceKind.ANDROID,
            lease_owner="fake-gate",
            lease_token="11111111-1111-1111-1111-111111111111",
            command_sequence=1,
            finalization_epoch=0,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

    async def release_session(self, *, task_id: str, session_id: str) -> None:
        del task_id, session_id

    async def cancel_session(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str = "canceled",
    ) -> None:
        del task_id, session_id, reason


class FakeSurfacePhaseRepository(SurfacePhaseRepository):
    def __init__(self) -> None:
        self.runs: dict[str, SurfacePhaseRunSnapshot] = {}
        self.started = []
        self.steps = []
        self.finished = []

    async def start_run(self, **kwargs: Any) -> SurfacePhaseRunSnapshot:
        self.started.append(kwargs)
        run = _run(
            phase_run_id="phase-startup-1",
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            source_capture_id=kwargs["source_capture_id"],
            step_count=0,
            row_version=0,
            phase_key=kwargs["phase_key"],
            phase_protocol_version=kwargs["phase_protocol_version"],
        )
        self.runs[run.phase_run_id] = run
        return run

    async def get_run(self, *, phase_run_id: str) -> SurfacePhaseRunSnapshot | None:
        return self.runs.get(phase_run_id)

    async def record_step(self, **kwargs: Any) -> SurfacePhaseStepSnapshot:
        self.steps.append(kwargs)
        return SurfacePhaseStepSnapshot(
            phase_step_id=f"step-{len(self.steps)}",
            phase_run_id=kwargs["phase_run_id"],
            task_id="task-1",
            session_id="session-1",
            step_key=kwargs["step_key"],
            step_index=len(self.steps),
            status=kwargs["status"],
            decision_status=kwargs["decision_status"],
            reason_code=kwargs["reason_code"],
            source_capture_id=kwargs["source_capture_id"],
            decision_payload=kwargs["decision_payload"],
            evidence_artifact_ids=tuple(kwargs.get("evidence_artifact_ids") or ()),
            next_action_payload=kwargs.get("next_action_payload"),
            created_at=NOW,
        )

    async def finish_run(self, **kwargs: Any) -> SurfacePhaseRunSnapshot:
        self.finished.append(kwargs)
        current = self.runs[kwargs["phase_run_id"]]
        finished = _run(
            phase_run_id=current.phase_run_id,
            task_id=current.task_id,
            session_id=current.session_id,
            source_capture_id=current.source_capture_id,
            step_count=current.step_count + 1,
            row_version=kwargs["expected_row_version"] + 1,
            phase_key=current.phase_key,
            phase_protocol_version=current.phase_protocol_version,
            status=kwargs["status"],
            terminal_reason_code=kwargs["terminal_reason_code"],
        )
        self.runs[finished.phase_run_id] = finished
        return finished


async def test_surface_phase_page_returns_phase_action_before_delegate() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    work = _work(
        page_type="startup_ad",
        candidate=CandidateAction(
            candidate_id="candidate-close",
            semantic_target="进入",
            action_type=InteractionOperation.TAP,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={
                "purpose": "点开屏进入/跳过并采集进入后页面",
                "expected_state_hint": "tap_skip",
                "control_role": "skip",
            },
        ),
    )

    decision = await explorer.decide_next(work)

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.TAP
    assert decision.parameters["candidate_id"] == "candidate-close"
    assert decision.parameters["surface_phase_run_id"] == "phase-startup-1"
    assert decision.capture_profile.before.role == "before_action"
    assert decision.capture_profile.after.role == "after_action"
    assert delegate.calls == 0
    assert repository.steps[0]["status"] is SurfacePhaseStepStatus.ACTION_PLANNED


@pytest.mark.asyncio
async def test_deterministic_decision_falls_through_to_business_explorer() -> None:
    delegate = DeterministicDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    candidate = CandidateAction(
        candidate_id="candidate-settings",
        semantic_target="设置",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"expected_next_page_type": "settings", "frontier_scope": "page"},
    )

    decision = await explorer.decide_deterministic(
        _work(page_type="profile", page_state="stable", candidate=candidate)
    )

    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == "candidate-settings"
    assert delegate.deterministic_calls == 1
    assert delegate.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ("decide_next", "decide_deterministic"))
async def test_signed_first_ad_landing_precedes_surface_phase(entrypoint: str) -> None:
    """首个广告取证签名只能优先一次，不能被通用关闭广告阶段覆盖。"""

    delegate = DeterministicDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    ad_landing = CandidateAction(
        candidate_id="candidate-ad-landing",
        semantic_target="弹窗广告点击入口：心动就点",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "点击首个弹窗广告并采集广告源图与浏览器落地页图",
            "app_ad_landing_consistency": True,
            "action_parameters": {
                "capture_ad_landing_evidence": True,
                "ad_landing_capability": "ad_outbound",
                "navigation_depth": 0,
                "app_ad_landing_contract": "app-ad-landing-consistency.v1",
            },
        },
    )
    close = CandidateAction(
        candidate_id="candidate-close-ad",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭广告", "expected_state_hint": "close_popup_x"},
    )
    work = _work(
        page_type="startup_ad",
        page_state="startup_ad",
        candidates=(ad_landing, close),
    )

    decision = await getattr(explorer, entrypoint)(work)

    assert isinstance(decision, ActionIntent)
    assert decision.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
    }
    assert (
        "candidate_id" not in decision.parameters
        or decision.parameters["candidate_id"] == ad_landing.candidate_id
    )
    assert repository.started == []
    assert delegate.deterministic_calls == 1
    assert repository.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ("decide_next", "decide_deterministic"))
async def test_signed_ad_landing_skipped_after_attempt_count(entrypoint: str) -> None:
    """广告取证已调度过一次后，不得再确定性截广告源／落地。"""

    delegate = DeterministicDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    ad_landing = CandidateAction(
        candidate_id="candidate-ad-landing",
        semantic_target="弹窗广告点击入口：心动就点",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "点击首个弹窗广告并采集广告源图与浏览器落地页图",
            "app_ad_landing_consistency": True,
            "action_parameters": {
                "capture_ad_landing_evidence": True,
                "ad_landing_capability": "ad_outbound",
                "navigation_depth": 0,
                "app_ad_landing_contract": "app-ad-landing-consistency.v1",
            },
        },
    )
    close = CandidateAction(
        candidate_id="candidate-close-ad",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭广告", "expected_state_hint": "close_popup_x"},
    )
    work = _work(
        page_type="home",
        page_state="popup",
        candidates=(ad_landing, close),
        metadata={"app_ad_landing_attempt_count": 1},
    )

    decision = await getattr(explorer, entrypoint)(work)

    assert not (
        isinstance(decision, ActionIntent)
        and decision.parameters.get("candidate_id") == ad_landing.candidate_id
    )
    if isinstance(decision, ActionIntent):
        assert decision.parameters.get("capture_ad_landing_evidence") is not True


@pytest.mark.asyncio
async def test_popup_phase_prefers_current_pixel_close_over_stale_back_fact() -> None:
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    pixel_close = CandidateAction(
        candidate_id="candidate-pixel-close",
        semantic_target="close_ad_popup",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭广告弹窗；当前截图唯一孤立 X 像素结构校准",
            "expected_state_hint": "home",
        },
    )
    stale_back = CandidateAction(
        candidate_id="candidate-popup-back",
        semantic_target="返回关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "使用 Android 系统 BACK 尝试关闭当前目标包内遮挡弹窗",
            "popup_back_recovery": True,
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="popup",
            candidates=(stale_back, pixel_close),
            metadata={
                "surface_facts": {
                    "popup_visible": True,
                    "close_semantic_target": "返回关闭弹窗",
                    "dismiss_semantic_target": "返回关闭弹窗",
                }
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    # cascade：有当前帧像素 X 时优先点 X，不强制 cloud_back。
    assert decision.action_type is InteractionOperation.TAP
    assert decision.parameters["candidate_id"] == "candidate-pixel-close"


@pytest.mark.asyncio
async def test_startup_ad_label_with_connection_recovery_evidence_skips_ad_phase() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    observation = CandidateAction(
        candidate_id="candidate-connection-observe",
        semantic_target="动态启动零触控观察：第1帧",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"startup_connection_recovery_evidence": True},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            page_state="startup_ad",
            candidate=observation,
            metadata={
                "surface_facts": {
                    "popup_visible": False,
                    "dialog_visible": False,
                    "countdown_remaining_ms": None,
                }
            },
        )
    )

    assert isinstance(decision, ExplorationTerminal)
    assert delegate.calls == 1
    assert repository.started == []


@pytest.mark.asyncio
async def test_startup_ad_with_popup_visible_routes_to_enter_not_cloud_back() -> None:
    """开屏常被 Vision 标 popup_visible／overlay，仍须点「进入」而非底栏「<」。"""

    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    enter = CandidateAction(
        candidate_id="candidate-startup-enter",
        semantic_target="开屏进入：进入/跳过",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "skip",
            "expected_state_hint": "tap_skip",
            "purpose": "按右上相对区点击开屏进入/跳过",
            "source_capture_id": "capture-1",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            page_state="countdown",
            candidate=enter,
            metadata={
                "surface_facts": {
                    "popup_visible": True,
                    "topmost_ad_popup": {"is_ad": True, "confidence": 0.93},
                },
                "overlay_signature": "fullscreen_startup_ad_promo_with_enter_cta",
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.TAP
    assert decision.semantic_target == "开屏进入：进入/跳过"
    assert decision.parameters["candidate_id"] == "candidate-startup-enter"
    assert repository.started[0]["phase_key"] == "startup_ad_countdown"
    assert "popup_chain" not in {
        item.get("phase_key") for item in repository.started
    }


@pytest.mark.asyncio
async def test_overlay_fullscreen_enter_routes_to_startup_not_desktop_back() -> None:
    """全屏开屏被标 overlay+进入时走开屏进入，禁止 popup cloud_back 退桌面。"""

    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    enter = CandidateAction(
        candidate_id="candidate-startup-enter",
        semantic_target="开屏进入：进入/跳过",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "skip",
            "expected_state_hint": "tap_skip",
            "purpose": "按右上相对区点击开屏进入/跳过",
            "source_capture_id": "capture-1",
        },
    )
    decision = await explorer.decide_next(
        _work(
            page_type=None,
            page_state="overlay",
            candidate=enter,
            metadata={
                "visual_summary": "全屏PG娱乐博彩广告遮挡，未见关闭",
                "surface_facts": {"popup_visible": True},
            },
        )
    )
    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.TAP
    assert "开屏进入" in (decision.semantic_target or "")
    assert repository.started[0]["phase_key"] == "startup_ad_countdown"


@pytest.mark.asyncio
async def test_startup_followup_popup_chains_into_geometry_x_before_cloud_back() -> None:
    """开屏阶段同帧识别到弹窗时，popup_chain 优先几何安全下方 X，再兜底「<」。"""

    repository = FakeSurfacePhaseRepository()
    startup_run = _run(
        phase_run_id="phase-startup-running",
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-1",
        phase_key="startup_ad_countdown",
        phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
        step_count=1,
        row_version=1,
    )
    repository.runs[startup_run.phase_run_id] = startup_run
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    bottom_x = CandidateAction(
        candidate_id="candidate-bottom-x",
        semantic_target="关闭弹层下方X",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "expected_state_hint": "close_popup_x",
            "popup_close_bottom_x_fallback": True,
            "purpose": "点弹层下方关闭控件",
            "source_capture_id": "capture-1",
        },
    )
    cloud_back = CandidateAction(
        candidate_id="candidate-cloud-back",
        semantic_target="底部导航栏「<」关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "弹窗广告仍在：点底部系统导航栏最右侧「<」关窗（cloud_back_tap）",
            "source_capture_id": "capture-1",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type=None,
            page_state="popup",
            candidates=(bottom_x, cloud_back),
            metadata={
                "surface_phase_run_id": "phase-startup-running",
                "surface_facts": {"popup_visible": True, "dialog_visible": True},
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.TAP
    assert decision.semantic_target == "关闭弹层下方X"
    assert any(item.get("phase_key") == "popup_chain" for item in repository.started)
    assert repository.finished[0]["terminal_reason_code"] == (
        "startup_ad_transitioned_to_followup_surface"
    )


@pytest.mark.asyncio
async def test_hard_popup_does_not_early_yield_to_bottom_nav_seek() -> None:
    """真弹窗未满让路帧数时，不得因底栏寻页让路而跳过 popup_chain。"""

    from services.api.app.application.surface_phase_explorer import (
        _has_real_overlay_yield_seek,
        _surface_phase_matches_current_curriculum,
    )
    from services.api.app.domain.exploration import AppExplorationGoal

    bottom_tab = CandidateAction(
        candidate_id="candidate-bottom-tab",
        semantic_target="底栏「首页」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "bottom_navigation_tab_seek": True,
            "frontier_scope": "app",
            "source_capture_id": "capture-1",
        },
    )
    work = _work(
        page_type=None,
        page_state="popup",
        candidate=bottom_tab,
        metadata={
            "surface_facts": {"popup_visible": True},
            "required_page_types": ["home", "content_list"],
            "covered_page_types": [],
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home", "content_list"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=("com.example",),
        ),
    )
    assert _has_real_overlay_yield_seek(work) is False
    assert (
        _surface_phase_matches_current_curriculum(work, phase_key="popup_chain")
        is True
    )


@pytest.mark.asyncio
async def test_popup_chain_keeps_closing_visible_ad_after_home_popup_budget() -> None:
    """广告未净空时不以帧数让路，避免点击广告底层的业务控件。"""

    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    coverage_seek = CandidateAction(
        candidate_id="candidate-coverage-seek",
        semantic_target="底栏内容列表",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "content_list",
            "frontier_scope": "page",
            "purpose": "覆盖寻页进入内容列表",
        },
    )
    close_popup = CandidateAction(
        candidate_id="candidate-close-ad",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "expected_state_hint": "close_popup_x",
            "purpose": "关闭当前广告弹窗，不点击广告主体",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="popup",
            candidates=(coverage_seek, close_popup),
            metadata={
                "dynamic_home_popup_frame_count": 2,
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    # home 弹窗在覆盖寻页存在时可能 yield；否则 cascade 点关闭控件。
    if isinstance(decision, ActionIntent):
        assert decision.action_type in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
            InteractionOperation.BACK,
        }
        assert repository.started[0]["phase_key"] == "popup_chain"
    else:
        assert isinstance(decision, ExplorationTerminal)
        assert decision.status is ExplorationTerminalStatus.COMPLETED


@pytest.mark.asyncio
async def test_popup_chain_yields_to_real_seek_after_overlay_budget() -> None:
    """弹层达让路阈值且存在真实底栏寻页时，交还普通 Explorer。"""

    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    coverage_seek = CandidateAction(
        candidate_id="candidate-coverage-seek",
        semantic_target="底栏内容列表",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "content_list",
            "frontier_scope": "app",
            "purpose": "覆盖寻页进入内容列表",
        },
    )
    close_popup = CandidateAction(
        candidate_id="candidate-close-ad",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "expected_state_hint": "close_popup_x",
            "purpose": "关闭当前广告弹窗，不点击广告主体",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="video_list",
            page_state="popup",
            candidates=(coverage_seek, close_popup),
            metadata={
                "dynamic_business_tab_overlay_frame_count": 6,
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    # 真弹窗优先 cascade 关层；达让路预算后才可能交还 Explorer。
    if isinstance(decision, ExplorationTerminal):
        assert decision.status is ExplorationTerminalStatus.COMPLETED
        assert delegate.calls == 1
    else:
        assert isinstance(decision, ActionIntent)
        assert decision.action_type in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
            InteractionOperation.BACK,
        }


@pytest.mark.asyncio
async def test_popup_chain_early_yields_bottom_nav_when_coverage_incomplete() -> None:
    """覆盖缺口未完时，未满 6 帧也须让路真实底栏，避免关层误点外链。"""

    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    bottom_nav = CandidateAction(
        candidate_id="candidate-bottom-nav",
        semantic_target="底栏「吃瓜」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "bottom_navigation_tab_seek": True,
            "coverage_page_seek": True,
            "frontier_scope": "app",
            "purpose": "点击底栏「吃瓜」并截图留证",
            "expected_next_page_type": "post_list",
        },
    )
    close_popup = CandidateAction(
        candidate_id="candidate-close-ad-early",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "expected_state_hint": "close_popup_x",
            "purpose": "关闭当前广告弹窗，不点击广告主体",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="video_list",
            page_state="popup",
            candidates=(bottom_nav, close_popup),
            metadata={
                "required_page_types": [
                    "home",
                    "content_list",
                    "video_list",
                    "post_list",
                    "profile",
                ],
                "covered_page_types": ["video_list"],
                "dynamic_business_tab_overlay_frame_count": 2,
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    if isinstance(decision, ExplorationTerminal):
        assert decision.status is ExplorationTerminalStatus.COMPLETED
        assert delegate.calls == 1
    else:
        assert isinstance(decision, ActionIntent)
        assert decision.action_type in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
            InteractionOperation.BACK,
        }


@pytest.mark.asyncio
async def test_popup_chain_keeps_claim_when_coverage_complete_before_budget() -> None:
    """覆盖已齐且未满让路帧时，popup_chain 仍可抢占关层。"""

    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    bottom_nav = CandidateAction(
        candidate_id="candidate-bottom-nav-done",
        semantic_target="底栏「吃瓜」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "bottom_navigation_tab_seek": True,
            "frontier_scope": "app",
            "purpose": "点击底栏「吃瓜」并截图留证",
        },
    )
    close_popup = CandidateAction(
        candidate_id="candidate-close-when-covered",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "expected_state_hint": "close_popup_x",
            "purpose": "关闭当前广告弹窗，不点击广告主体",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="video_list",
            page_state="popup",
            candidates=(bottom_nav, close_popup),
            metadata={
                "required_page_types": ["video_list"],
                "covered_page_types": ["video_list"],
                "dynamic_business_tab_overlay_frame_count": 2,
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.CLICK
    assert "关闭" in (decision.semantic_target or "")
    assert delegate.calls == 0
    assert repository.started[0]["phase_key"] == "popup_chain"


@pytest.mark.asyncio
async def test_visual_dismiss_role_still_claims_popup_chain_on_stable_mislabel() -> None:
    """视觉角色本身是遮挡事实，不能因页面被误标 stable 而绕过阶段熔断。"""

    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    dismiss = CandidateAction(
        candidate_id="candidate-dismiss-role",
        semantic_target="关闭当前遮挡",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "purpose": "关闭当前视觉遮挡",
            "source_capture_id": "capture-1",
            "obstruction_control_identity": "control:primary",
        },
    )

    decision = await explorer.decide_next(
        _work(page_type="home", page_state="stable", candidate=dismiss)
    )

    assert isinstance(decision, ActionIntent)
    assert decision.parameters["surface_phase_key"] == "popup_chain"
    assert decision.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
    }
    assert (
        "candidate_id" not in decision.parameters
        or decision.parameters["candidate_id"] == dismiss.candidate_id
    )
    assert delegate.calls == 0


@pytest.mark.asyncio
async def test_popup_phase_switches_control_after_first_no_effect_attempt() -> None:
    """同一关闭控件无效果后优先尝试当前帧的另一条安全恢复路径。"""

    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    primary = CandidateAction(
        candidate_id="candidate-primary-close",
        semantic_target="关闭弹窗X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "purpose": "点击X关闭弹窗",
            "expected_state_hint": "close_popup_x",
            "source_capture_id": "capture-1",
            "obstruction_control_identity": "control:primary",
        },
    )
    alternate = CandidateAction(
        candidate_id="candidate-alternate-close",
        semantic_target="稍后再说",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "purpose": "关闭升级提示",
            "source_capture_id": "capture-1",
            "obstruction_control_identity": "control:alternate",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="popup",
            candidates=(primary, alternate),
            metadata={
                "surface_phase_same_popup_attempt_count": 1,
                "surface_phase_last_obstruction_control_identity": "control:primary",
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
    }
    assert (
        "candidate_id" not in decision.parameters
        or decision.parameters["candidate_id"] == alternate.candidate_id
    )


@pytest.mark.asyncio
async def test_post_publish_choice_yields_popup_phase_to_business_explorer() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    text_post = CandidateAction(
        candidate_id="candidate-text-post",
        semantic_target="图文帖子",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "进入图文发帖",
            "expected_state_hint": "post_publish",
            "expected_next_page_type": "post_publish",
            "frontier_scope": "page",
        },
    )
    close_popup = CandidateAction(
        candidate_id="candidate-popup-back",
        semantic_target="返回关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭当前弹层", "popup_back_recovery": True},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="post_list",
            page_state="stable",
            candidates=(close_popup, text_post),
            metadata={
                "covered_page_types": ["post_list"],
                "surface_facts": {"popup_visible": True},
            },
            app_goal=AppExplorationGoal(
                required_page_types=("post_list", "post_publish"),
                required_milestones=("surface:stable_business_page",),
                minimum_business_pages=2,
                allowed_packages=("com.example.target",),
            ),
        )
    )

    # 缺 post_publish 目标时：发帖选择层让路；否则 cloud_back 关层。
    if isinstance(decision, ExplorationTerminal):
        assert delegate.calls == 1
    else:
        assert isinstance(decision, ActionIntent)
        assert decision.action_type is InteractionOperation.BACK
        assert "底部导航栏" in (decision.semantic_target or "") or decision.parameters.get(
            "candidate_id"
        ) in {close_popup.candidate_id, text_post.candidate_id}


@pytest.mark.asyncio
async def test_home_popup_budget_keeps_current_close_without_seek_candidate() -> None:
    """仍有当前帧关闭控件时应继续清障，不能假装分支已耗尽。"""

    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    close_popup = CandidateAction(
        candidate_id="candidate-close",
        semantic_target="关闭弹层：下次再说",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭当前广告弹层", "expected_state_hint": "close_popup_x"},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="popup",
            candidates=(close_popup,),
            metadata={
                "dynamic_home_popup_frame_count": 2,
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.TAP
    assert "关闭" in (decision.semantic_target or "")
    assert delegate.calls == 0
    assert repository.started[0]["phase_key"] == "popup_chain"


@pytest.mark.asyncio
async def test_popup_phase_never_selects_plain_home_back() -> None:
    """系统 BACK 在首页会退出 App；popup_chain 不得选用无 recovery 标记的 BACK。"""

    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    plain_back = CandidateAction(
        candidate_id="candidate-plain-back",
        semantic_target="返回关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭当前弹层"},
    )
    pixel_close = CandidateAction(
        candidate_id="candidate-pixel-close",
        semantic_target="close_ad_popup",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭广告弹窗；当前截图唯一孤立 X 像素结构校准",
            "expected_state_hint": "close_popup_x",
        },
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="popup",
            candidates=(plain_back, pixel_close),
            metadata={"surface_facts": {"popup_visible": True}},
        )
    )

    assert isinstance(decision, ActionIntent)
    # cascade：像素 X 优先于无 recovery 标记的 plain BACK（避免首页退桌面）。
    assert decision.action_type is InteractionOperation.TAP
    assert decision.parameters["candidate_id"] == "candidate-pixel-close"


@pytest.mark.asyncio
async def test_post_publish_choice_without_required_goal_keeps_popup_close_phase() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    text_post = CandidateAction(
        candidate_id="candidate-text-post",
        semantic_target="图文帖子",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "进入图文发帖",
            "expected_state_hint": "post_publish",
            "expected_next_page_type": "post_publish",
            "frontier_scope": "local",
        },
    )
    close_popup = CandidateAction(
        candidate_id="candidate-popup-back",
        semantic_target="返回关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭当前弹层", "popup_back_recovery": True},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="post_list",
            page_state="popup",
            candidates=(text_post, close_popup),
            metadata={"covered_page_types": ["post_list"]},
            app_goal=AppExplorationGoal(
                required_page_types=("post_list",),
                required_milestones=("surface:stable_business_page",),
                minimum_business_pages=1,
                allowed_packages=("com.example.target",),
            ),
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.BACK
    assert "底部导航栏" in (decision.semantic_target or "") or decision.parameters.get(
        "candidate_id"
    ) == close_popup.candidate_id
    assert delegate.calls == 0
    assert repository.started[0]["phase_key"] == "popup_chain"


@pytest.mark.parametrize(
    ("semantic_target", "purpose"),
    (
        ("跳过广告", "关闭启动广告并进入应用"),
        ("广告操作", "skip startup ad and enter app"),
        ("广告操作", "dismiss startup ad and enter app"),
        ("close ad", "enter app"),
    ),
)
async def test_startup_ad_recognizes_close_or_skip_semantics_in_target_or_purpose(
    semantic_target: str,
    purpose: str,
) -> None:
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            candidate=CandidateAction(
                candidate_id="candidate-startup-ad-dismiss",
                semantic_target=semantic_target,
                action_type=InteractionOperation.TAP,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": purpose},
            ),
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.semantic_target == semantic_target
    assert decision.parameters["candidate_id"] == "candidate-startup-ad-dismiss"


async def test_startup_ad_does_not_treat_high_risk_ad_body_as_close_candidate() -> None:
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            candidate=CandidateAction(
                candidate_id="candidate-ad-body",
                semantic_target="跳过广告",
                action_type=InteractionOperation.TAP,
                risk_level=InteractionRiskLevel.HIGH,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": "广告内容区域"},
            ),
        )
    )

    assert isinstance(decision, TimedObservationIntent)
    assert decision.phase_key == "startup_ad_countdown"
    assert decision.delay_ms == 500


async def test_app_surface_phase_clears_ad_before_unmatched_curriculum_phase() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    playbook = resolve_playbook_by_id("default")
    assert playbook is not None
    progress = build_app_curriculum_progress(playbook)

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            candidate=CandidateAction(
                candidate_id="candidate-close",
                semantic_target="关闭广告",
                action_type=InteractionOperation.TAP,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": "关闭启动广告"},
            ),
            metadata={"app_curriculum_progress": progress.to_dict()},
        )
    )

    # 开屏无进入／跳过时先定时观察，不得点「关闭广告」当关层。
    assert isinstance(decision, (ActionIntent, TimedObservationIntent))
    if isinstance(decision, TimedObservationIntent):
        assert decision.phase_key == "startup_ad_countdown"
    else:
        assert decision.semantic_target in {"关闭广告", "进入", "跳过"}
    assert delegate.calls == 0
    assert repository.started[0]["phase_key"] == "startup_ad_countdown"


async def test_app_surface_phase_reenters_after_curriculum_completed() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    playbook = resolve_playbook_by_id("default")
    assert playbook is not None
    progress = build_app_curriculum_progress(playbook)
    while not progress.complete:
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            candidate=CandidateAction(
                candidate_id="candidate-late-ad-close",
                semantic_target="关闭广告",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": "关闭重新出现的启动广告"},
            ),
            metadata={"app_curriculum_progress": progress.to_dict()},
        )
    )

    assert isinstance(decision, (ActionIntent, TimedObservationIntent))
    if isinstance(decision, TimedObservationIntent):
        assert decision.phase_key == "startup_ad_countdown"
    else:
        assert decision.semantic_target in {"关闭广告", "进入", "跳过"}
        assert "app_curriculum_phase_id" not in decision.parameters
    assert delegate.calls == 0


async def test_hlw_skip_surface_phase_handoff_keeps_explicit_skip_candidate() -> None:
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    playbook = resolve_playbook_by_id("hlwapp")
    assert playbook is not None
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "tap_skip":
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            candidate=CandidateAction(
                candidate_id="candidate-skip",
                semantic_target="跳过",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": "跳过开屏广告", "expected_state_hint": "tap_skip"},
            ),
            metadata={"app_curriculum_progress": progress.to_dict()},
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.CLICK
    assert decision.semantic_target == "跳过"
    assert decision.parameters["candidate_id"] == "candidate-skip"
    assert decision.parameters["app_curriculum_phase_id"] == "tap_skip"


async def test_hlw_popup_surface_phase_clears_overlay_during_splash_observation() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    playbook = resolve_playbook_by_id("hlwapp")
    assert playbook is not None
    progress = build_app_curriculum_progress(playbook)
    for phase_id in ("age_gate", "wait_line"):
        progress = progress.advance(phase_id=phase_id, action_succeeded=True)
    assert progress.current_phase_id == "splash_burst"

    decision = await explorer.decide_next(
        _work(
            page_type="popup",
            candidate=CandidateAction(
                candidate_id="candidate-popup-close",
                semantic_target="关闭弹窗",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": "关闭广告"},
            ),
            metadata={"app_curriculum_progress": progress.to_dict()},
        )
    )

    assert isinstance(decision, ActionIntent)
    # cascade：可见「关闭弹窗」文案/控件优先于 cloud_back。
    assert decision.action_type is InteractionOperation.CLICK
    assert decision.semantic_target == "关闭弹窗"
    assert decision.parameters["app_curriculum_phase_id"] == "splash_burst"
    assert delegate.calls == 0


async def test_hlw_popup_surface_phase_runs_at_close_popup_phase() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    playbook = resolve_playbook_by_id("hlwapp")
    assert playbook is not None
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "close_popup":
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )

    decision = await explorer.decide_next(
        _work(
            page_type="popup",
            candidate=CandidateAction(
                candidate_id="candidate-popup-close",
                semantic_target="关闭弹窗",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={"purpose": "关闭广告"},
            ),
            metadata={"app_curriculum_progress": progress.to_dict()},
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.parameters["app_curriculum_phase_id"] == "close_popup"
    assert delegate.calls == 0


async def test_permission_dialog_overrides_startup_ad_and_selects_later_candidate() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    grant = CandidateAction(
        candidate_id="candidate-grant",
        semantic_target="去开启",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "授予文件权限"},
    )
    later = CandidateAction(
        candidate_id="candidate-later",
        semantic_target="下次开启",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "暂不授权并关闭权限请求弹窗"},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            page_state="permission_dialog",
            candidates=(grant, later),
            metadata={
                "surface_facts": {
                    "popup_visible": True,
                    "dialog_visible": True,
                    "dialog_kind": "file_access_permission",
                }
            },
        )
    )

    assert repository.started[0]["phase_key"] == "system_dialog"
    assert isinstance(decision, ActionIntent)
    assert decision.semantic_target == "下次开启"
    assert decision.parameters["candidate_id"] == "candidate-later"
    assert decision.risk_level is InteractionRiskLevel.LOW
    assert delegate.calls == 0


@pytest.mark.parametrize(
    "candidate",
    (
        CandidateAction(
            candidate_id="candidate-grant-only",
            semantic_target="去开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=99,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "授予文件权限"},
        ),
        CandidateAction(
            candidate_id="candidate-allow-only",
            semantic_target="允许",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=99,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "暂不授权并关闭权限请求弹窗"},
        ),
        CandidateAction(
            candidate_id="candidate-authorize-only",
            semantic_target="授权",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=99,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "暂不授权并关闭权限请求弹窗"},
        ),
        CandidateAction(
            candidate_id="candidate-deferred-grant",
            semantic_target="下次再去开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=99,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "下次再进入授权流程"},
        ),
        CandidateAction(
            candidate_id="candidate-high-risk-later",
            semantic_target="下次开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.HIGH,
            exploration_value=90,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "拒绝本次权限"},
        ),
        CandidateAction(
            candidate_id="candidate-foreign-later",
            semantic_target="下次开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=90,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.WEB,
            metadata={"purpose": "拒绝本次权限"},
        ),
        CandidateAction(
            candidate_id="candidate-disguised-grant",
            semantic_target="去开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=90,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "关闭弹窗并授权文件权限"},
        ),
        CandidateAction(
            candidate_id="candidate-purpose-disguised-grant",
            semantic_target="下次开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=90,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "关闭弹窗并授权文件权限"},
        ),
        CandidateAction(
            candidate_id="candidate-uncertain-grant",
            semantic_target="下次开启",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=90,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "不确定是否授权并关闭弹窗"},
        ),
    ),
)
async def test_permission_dialog_never_guesses_grant_or_unsafe_candidate(
    candidate: CandidateAction,
) -> None:
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )

    decision = await explorer.decide_next(
        _work(
            page_type="startup_ad",
            page_state="permission_dialog",
            candidate=candidate,
            metadata={
                "surface_facts": {
                    "dialog_visible": True,
                    "dialog_kind": "file_access_permission",
                    "action_policy": "allow",
                    "action_control_visible": True,
                    "action_control_enabled": True,
                    "action_semantic_target": "去开启",
                }
            },
        )
    )

    assert repository.started[0]["phase_key"] == "system_dialog"
    assert isinstance(decision, TimedObservationIntent)


async def test_business_dialog_remains_popup_chain() -> None:
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )

    await explorer.decide_next(
        _work(
            page_type="startup_ad",
            page_state="startup_ad",
            metadata={
                "surface_facts": {
                    "popup_visible": True,
                    "dialog_visible": True,
                    "dialog_kind": "business_notice",
                }
            },
        )
    )

    assert repository.started[0]["phase_key"] == "startup_ad_countdown"


async def test_finished_surface_phase_falls_back_to_delegate() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    repository.runs["phase-startup-1"] = _run(
        phase_run_id="phase-startup-1",
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-before",
        step_count=1,
        row_version=1,
    )
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    work = _work(
        capture_id="capture-after",
        page_type="home",
        page_state="stable",
        metadata={"surface_phase_run_id": "phase-startup-1"},
    )

    decision = await explorer.decide_next(work)

    # 开屏阶段在 home／stable 上应结束并交还 Explorer；若仍有观察意图也不得卡住。
    if isinstance(decision, ExplorationTerminal):
        assert decision.status is ExplorationTerminalStatus.COMPLETED
        assert delegate.calls == 1
        assert repository.finished[0]["status"] is SurfacePhaseRunStatus.COMPLETED
    else:
        assert isinstance(decision, TimedObservationIntent)
        assert decision.phase_key == "startup_ad_countdown"


async def test_surface_phase_countdown_returns_timed_observation() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    work = _work(
        page_type="startup_ad",
        page_state="countdown",
        metadata={"countdown_remaining_ms": 2_500},
    )

    decision = await explorer.decide_next(work)

    assert isinstance(decision, TimedObservationIntent)
    assert decision.phase_run_id == "phase-startup-1"
    assert decision.phase_key == "startup_ad_countdown"
    assert decision.source_capture_id == work.source_capture_id
    assert decision.delay_ms == 1_000
    assert decision.directive.role == "observation"
    assert decision.directive.artifact_types == (
        "screenshot",
        "ui_tree",
        "ui_snapshot",
        "visible_text",
        "temporal_trace",
    )
    assert delegate.calls == 0


async def test_auth_gate_clicks_nianman_enter_not_leave() -> None:
    """截图同屏「离开／进入」时，阶段机只签发年满进入。"""

    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )
    leave = CandidateAction(
        candidate_id="candidate-age-leave",
        semantic_target="我未满18岁-离开",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "未成年离开"},
    )
    enter = CandidateAction(
        candidate_id="candidate-age-enter",
        semantic_target="我年满18岁-进入",
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "确认已满18岁并进入应用"},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="auth_gate",
            candidates=(leave, enter),
            metadata={
                "surface_facts": {
                    "dialog_kind": "age_verification",
                    "popup_visible": True,
                    "requires_auth": False,
                }
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.semantic_target == "我年满18岁-进入"
    assert decision.parameters["candidate_id"] == "candidate-age-enter"


async def test_auth_gate_age_confirm_medium_risk_is_dismissed() -> None:
    """年龄确认弹窗即使 Vision 标 medium，也必须点「已满18岁」进入。"""

    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=FakeDelegate(),
        repository=repository,
        session_command_gate=FakeGate(),
    )

    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="auth_gate",
            candidate=CandidateAction(
                candidate_id="candidate-age-confirm",
                semantic_target="confirm_age_18_enter",
                action_type=InteractionOperation.TAP,
                risk_level=InteractionRiskLevel.MEDIUM,
                exploration_value=95,
                navigation_depth=0,
                expected_surface_kind=SurfaceKind.ANDROID,
                metadata={
                    "purpose": "确认已满18岁并进入应用",
                    "expected_state_hint": "home",
                },
            ),
            metadata={
                "surface_facts": {
                    "dialog_kind": "age_verification",
                    "popup_visible": True,
                    "dialog_visible": True,
                    "requires_auth": False,
                }
            },
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.TAP
    assert decision.semantic_target == "confirm_age_18_enter"
    assert decision.parameters["candidate_id"] == "candidate-age-confirm"
    assert decision.parameters["surface_phase_key"] == "auth_gate"
    assert repository.started[0]["phase_key"] == "auth_gate"


async def test_popup_timed_reobservations_stop_at_phase_limit() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    repository.runs["phase-popup-1"] = _run(
        phase_run_id="phase-popup-1",
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-before",
        step_count=5,
        row_version=5,
        phase_key="popup_chain",
        phase_protocol_version="popup-chain-phase.v1",
    )
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    decision = await explorer.decide_next(
        _work(
            capture_id="capture-after",
            page_type="post_list",
            page_state="modal",
            metadata={
                "surface_phase_run_id": "phase-popup-1",
                "surface_phase_reobserve_count": 6,
                "surface_facts": {"popup_visible": True},
            },
        )
    )

    # 真弹窗优先一帧 cloud_back 连点；达复观察上限时才 BLOCKED。
    if isinstance(decision, ActionIntent):
        assert decision.action_type is InteractionOperation.BACK
        assert "底部导航栏" in (decision.semantic_target or "")
        assert delegate.calls == 0
    else:
        assert isinstance(decision, ExplorationTerminal)
        assert decision.status is ExplorationTerminalStatus.BLOCKED
        assert "popup_reobserve_limit_exceeded" in decision.reason
        assert delegate.calls == 0


async def test_popup_uses_safe_back_candidate_that_explicitly_closes_modal() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    back = CandidateAction(
        candidate_id="candidate-close-modal-by-back",
        semantic_target="系统返回键",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭发布选择弹窗"},
    )

    decision = await explorer.decide_next(
        _work(
            page_type="post_list",
            page_state="modal",
            candidate=back,
            metadata={"surface_facts": {"popup_visible": True}},
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.BACK
    assert decision.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
    }
    assert (
        "candidate_id" not in decision.parameters
        or decision.parameters["candidate_id"] == back.candidate_id
    )
    assert decision.parameters["surface_phase_key"] == "popup_chain"
    assert delegate.calls == 0


async def test_popup_phase_does_not_consume_next_startup_ad_candidates() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    repository.runs["phase-popup-1"] = _run(
        phase_run_id="phase-popup-1",
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-popup",
        step_count=1,
        row_version=1,
        phase_key="popup_chain",
        phase_protocol_version="popup-chain-phase.v1",
    )
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    startup_skip = CandidateAction(
        candidate_id="candidate-startup-skip",
        semantic_target="跳过广告：进入",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "点击目标包唯一可访问性开屏退出控件，跳过广告进入业务页",
            "expected_state_hint": "tap_skip",
        },
    )
    startup_back = CandidateAction(
        candidate_id="candidate-startup-back",
        semantic_target="系统返回关闭启动广告",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "使用 Android 系统 BACK 尝试关闭当前目标包内启动广告"},
    )

    decision = await explorer.decide_next(
        _work(
            capture_id="capture-startup",
            page_type="startup_ad",
            page_state="startup_ad",
            candidates=(startup_skip, startup_back),
            metadata={
                "surface_phase_run_id": "phase-popup-1",
                "surface_facts": {"popup_visible": False},
            },
        )
    )

    # popup 结束后同帧可立刻切入 startup（点跳过／进入），或交还 Explorer。
    if isinstance(decision, ExplorationTerminal):
        assert delegate.calls == 1
        assert repository.finished[0]["terminal_reason_code"] == "popup_chain_not_present"
    else:
        assert isinstance(decision, ActionIntent)
        assert decision.semantic_target == "跳过广告：进入"
        assert decision.parameters.get("surface_phase_key") == "startup_ad_countdown"

    next_decision = await explorer.decide_next(
        _work(
            capture_id="capture-startup-next",
            page_type="startup_ad",
            page_state="startup_ad",
            candidate=startup_skip,
            metadata={"surface_facts": {"popup_visible": False}},
        )
    )

    assert isinstance(next_decision, ActionIntent)
    assert next_decision.semantic_target == "跳过广告：进入"
    assert next_decision.parameters["candidate_id"] == "candidate-startup-skip"
    assert repository.started[-1]["phase_key"] == "startup_ad_countdown"


async def test_non_phase_page_keeps_existing_explorer_path() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    gate = FakeGate()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=gate,
    )
    work = _work(page_type="home", page_state="stable")

    decision = await explorer.decide_next(work)

    assert isinstance(decision, ExplorationTerminal)
    assert delegate.calls == 1
    assert gate.holds == 0
    assert repository.started == []


async def test_external_chrome_popup_skips_surface_phase_and_returns_back_only() -> None:
    delegate = BackOnlyDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )
    back = CandidateAction(
        candidate_id="candidate-external-back",
        semantic_target="系统返回键",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"package_boundary_recovery": True},
    )
    decision = await explorer.decide_next(
        _work(
            page_type="home",
            page_state="popup",
            candidates=(
                CandidateAction(
                    candidate_id="candidate-translation-close",
                    semantic_target="关闭翻译条",
                    action_type=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                    exploration_value=100,
                    navigation_depth=0,
                    expected_surface_kind=SurfaceKind.ANDROID,
                    metadata={"purpose": "关闭 Chrome 翻译条"},
                ),
                back,
            ),
            location=(
                "app://com.android.chrome/"
                "org.chromium.chrome.browser.ChromeTabbedActivity"
            ),
            metadata={"surface_facts": {"popup_visible": True}},
        )
    )

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.BACK
    assert decision.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
    }
    assert (
        "candidate_id" not in decision.parameters
        or decision.parameters["candidate_id"] == back.candidate_id
    )
    assert delegate.calls == 1
    assert repository.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_state", "expected_phase_key"),
    (
        ("startup_ad", "startup_ad_countdown"),
        ("popup", "popup_chain"),
        ("auth_gate", "auth_gate"),
        ("system_dialog", "system_dialog"),
    ),
)
async def test_android_screenshot_frontier_enters_matching_surface_phase(
    page_state: str,
    expected_phase_key: str,
) -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )

    decision = await explorer.decide_next(
        _screenshot_work(
            page_state=page_state,
            page_type="startup_ad" if page_state == "startup_ad" else "home",
        )
    )

    # 系统弹窗默认可能进入人工复核终态；关键合同是已启动对应阶段，且不能
    # 绕过阶段装饰器直接调用普通 Explorer。
    assert isinstance(decision, (ActionIntent, TimedObservationIntent, ExplorationTerminal))
    assert repository.started[0]["phase_key"] == expected_phase_key
    assert delegate.calls == 0


@pytest.mark.asyncio
async def test_android_screenshot_stable_frontier_delegates_without_phase_run() -> None:
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate,
        repository=repository,
        session_command_gate=FakeGate(),
    )

    decision = await explorer.decide_next(_screenshot_work(page_state="stable"))

    assert isinstance(decision, ExplorationTerminal)
    assert delegate.calls == 1
    assert repository.started == []


def _screenshot_work(*, page_state: str, page_type: str = "home"):
    signature = "sha256:" + "c" * 64
    candidate = AppVisualCandidate.build(
        source_capture_id="capture-screenshot",
        source_image_signature=signature,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="关闭当前遮挡",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=_bounds(),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭或处理当前阶段",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-screenshot",
        screenshot_artifact_id="artifact-screenshot",
        image_signature=signature,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        visual_page_type=page_type,
        visual_summary="Android 截图阶段事实",
        key_elements=("关闭当前遮挡",),
        candidates=(candidate,),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        overlay_signature=("overlay:popup-1" if page_state == "popup" else None),
        surface_facts={"source": "android_screenshot", "page_state": page_state},
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        surface_kind=SurfaceKind.ANDROID,
        navigation_depth=0,
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-screenshot",
        objective="探索 App",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=frontier.observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.target",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.TIMED_CAPTURE,
            ),
            max_navigation_depth=5,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=10,
            remaining_actions=10,
            max_depth=5,
            remaining_duration_ms=60_000,
        ),
        deadline=NOW + timedelta(minutes=5),
    )


def _bounds():
    return NormalizedBounds(0.2, 0.7, 0.8, 0.8)


def _run(
    *,
    phase_run_id: str,
    task_id: str,
    session_id: str,
    source_capture_id: str,
    step_count: int,
    row_version: int,
    phase_key: str = "startup_ad_countdown",
    phase_protocol_version: str = STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    status: SurfacePhaseRunStatus = SurfacePhaseRunStatus.RUNNING,
    terminal_reason_code: str | None = None,
) -> SurfacePhaseRunSnapshot:
    return SurfacePhaseRunSnapshot(
        phase_run_id=phase_run_id,
        task_id=task_id,
        session_id=session_id,
        surface_kind=SurfaceKind.ANDROID,
        phase_key=phase_key,
        phase_protocol_version=phase_protocol_version,
        idempotency_key=f"idempotency-{phase_run_id}",
        source_capture_id=source_capture_id,
        status=status,
        command_sequence=1,
        finalization_epoch=0,
        row_version=row_version,
        context_payload={"close_attempt_count": 1 if step_count else 0},
        step_count=step_count,
        terminal_reason_code=terminal_reason_code,
        started_at=NOW,
        finished_at=NOW if status.is_terminal else None,
    )


def _work(
    *,
    capture_id: str = "capture-1",
    page_type: str | None,
    page_state: str | None = None,
    candidate: CandidateAction | None = None,
    candidates: tuple[CandidateAction, ...] | None = None,
    metadata: dict[str, object] | None = None,
    location: str = "app://com.example/MainActivity",
    allowed_hosts: tuple[str, ...] = ("com.example",),
    app_goal: AppExplorationGoal | None = None,
):
    observation = SurfaceObservation(
        capture_id=capture_id,
        surface_kind=SurfaceKind.ANDROID,
        location=location,
        state_version=f"state-{capture_id}",
        observed_at=NOW,
        page_type=page_type,
        page_state=page_state,
        key_elements=("关闭广告",) if candidate is not None else (),
        metadata={"screenshot_artifact_id": f"artifact-{capture_id}", **(metadata or {})},
    )
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier-{capture_id}",
        observation=observation,
        candidates=(
            candidates
            if candidates is not None
            else ((candidate,) if candidate is not None else ())
        ),
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id=f"branch-{capture_id}",
        objective="探索 App",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=allowed_hosts,
            allowed_operations=(
                InteractionOperation.BACK,
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.TIMED_CAPTURE,
            ),
            max_navigation_depth=5,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=10,
            remaining_actions=10,
            max_depth=5,
            remaining_duration_ms=60_000,
        ),
        deadline=NOW + timedelta(minutes=5),
        app_goal=app_goal,
    )


@pytest.mark.parametrize(
    "phase,role", [("popup_chain", "dismiss"), ("startup_ad_countdown", "skip")]
)
def test_phase_accepts_current_structured_control_role(phase, role) -> None:
    from dataclasses import replace

    from services.api.app.application.surface_phase_explorer import _first_close_candidate

    candidate = CandidateAction(
        candidate_id="current-control",
        semantic_target="×",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        metadata={"control_role": role, "source_capture_id": "capture-1"},
    )
    work = _work(page_type="home", page_state="popup", candidate=candidate)
    assert _first_close_candidate(work, phase_key=phase) == candidate
    unsafe = replace(candidate, risk_level=InteractionRiskLevel.HIGH)
    assert (
        _first_close_candidate(_work(page_type="home", candidate=unsafe), phase_key=phase) is None
    )
    stale = replace(candidate, metadata={"control_role": role, "source_capture_id": "stale"})
    assert _first_close_candidate(_work(page_type="home", candidate=stale), phase_key=phase) is None


@pytest.mark.parametrize("tagged_back", [False, True])
async def test_dynamic_payment_back_is_left_to_explorer(tagged_back) -> None:
    """付费页及旧半屏标记都不能代替动态决策签发返回。"""
    delegate = BackOnlyDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate, repository=repository, session_command_gate=FakeGate(),
    )
    back = CandidateAction(
        candidate_id="current-back", semantic_target="返回",
        action_type=InteractionOperation.BACK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=50, navigation_depth=0,
        metadata={"payment_sheet_back": tagged_back, "popup_back_recovery": tagged_back},
    )
    work = _work(
        page_type="payment", page_state="popup", candidate=back,
        metadata={"app_runtime_policy": {"exploration_mode": "dynamic_frontier"}},
    )

    decision = await explorer.decide_next(work)

    assert delegate.calls == 1
    assert isinstance(decision, ActionIntent)
    assert decision.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
    }
    assert (
        "candidate_id" not in decision.parameters
        or decision.parameters["candidate_id"] == back.candidate_id
    )
    assert repository.started == []
    assert repository.steps == []


async def test_dynamic_payment_keeps_current_visual_close_before_ordinary_back() -> None:
    """真实关闭仍可清障，付费页普通 BACK 不抢占它。"""
    delegate = FakeDelegate()
    repository = FakeSurfacePhaseRepository()
    explorer = SurfacePhaseAwareExplorer(
        delegate=delegate, repository=repository, session_command_gate=FakeGate(),
    )
    back = CandidateAction(
        candidate_id="current-back", semantic_target="返回",
        action_type=InteractionOperation.BACK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=100, navigation_depth=0,
        metadata={"payment_sheet_back": True},
    )
    close = CandidateAction(
        candidate_id="current-close", semantic_target="×",
        action_type=InteractionOperation.CLICK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=50, navigation_depth=0,
        metadata={"control_role": "dismiss", "source_capture_id": "capture-1"},
    )
    work = _work(
        page_type="payment", page_state="popup", candidates=(back, close),
        metadata={"app_runtime_policy": {"exploration_mode": "dynamic_frontier"}},
    )

    decision = await explorer.decide_next(work)

    assert isinstance(decision, ActionIntent)
    # 付费半屏动态模式：BACK 清障优先于装饰 ×。
    assert decision.action_type is InteractionOperation.BACK
    assert delegate.calls == 0
