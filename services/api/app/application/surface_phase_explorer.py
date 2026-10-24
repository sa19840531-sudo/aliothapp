"""W17-W20 Surface 阶段机优先的 Explorer 装饰器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from services.api.app.application.app_coverage_page_seek import (
    coverage_incomplete_allows_bottom_nav_through_overlay,
    overlay_budget_allows_real_coverage_seek,
)
from services.api.app.application.app_obstruction_controls import is_visual_obstruction_control
from services.api.app.application.app_package_boundary import (
    work_within_app_package_boundary,
)
from services.api.app.application.app_script_curriculum import (
    AppCurriculumProgress,
    build_app_curriculum_progress,
    extract_playbook_id_from_objective,
    resolve_playbook_by_id,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    attach_synthetic_touch_contracts,
)
from services.api.app.application.capture_coordination import SessionCommandGate
from services.api.app.application.explorer_turn import ExplorerDecisionPort
from services.api.app.application.surface_phase_action_handoff import (
    SurfacePhaseActionHandoffKind,
)
from services.api.app.application.surface_phase_runtime import SurfacePhaseRepository
from services.api.app.application.surface_phase_turn_orchestrator import (
    SurfacePhaseTurnOrchestrator,
)
from services.api.app.domain.exploration import (
    AUTH_GATE_PHASE_PROTOCOL_VERSION,
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
)
from services.api.app.domain.exploration.age_confirm import is_age_confirm_candidate
from services.api.app.domain.interaction import (
    TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V2,
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
    build_timed_observation_id,
)
from services.api.app.domain.protocol import stable_protocol_hash

_PHASE_PROTOCOL_BY_KEY = {
    "startup_ad_countdown": STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    "popup_chain": POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    "auth_gate": AUTH_GATE_PHASE_PROTOCOL_VERSION,
    "system_dialog": SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
}
_STARTUP_STATES = frozenset({"countdown", "startup_ad"})
_POPUP_STATES = frozenset({"popup", "modal", "dialog", "overlay"})
_AUTH_STATES = frozenset({"auth_gate", "auth_required", "login_required", "requires_auth"})
_SYSTEM_DIALOG_STATES = frozenset({"system_dialog", "permission_dialog", "browser_permission"})
_DIALOG_ACTION_TYPES = frozenset(
    {
        InteractionOperation.BACK,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }
)
_SAFE_DIALOG_DISMISS_MARKERS = (
    "下次",
    "稍后",
    "暂不",
    "拒绝",
    "不允许",
    "取消",
    "跳过",
    "以后",
    "关闭",
    "dismiss",
    "deny",
    "decline",
    "not now",
    "later",
    "cancel",
    "skip",
    "close",
)
_UNSAFE_DIALOG_GRANT_MARKERS = (
    "去开启",
    "立即开启",
    "允许",
    "授权",
    "allow",
    "grant",
    "open settings",
)
_DIALOG_GRANT_NEGATION_MARKERS = (
    "暂不",
    "暂时不",
    "不要",
    "不应",
    "不再",
    "不得",
    "不允许",
    "不授权",
    "无须",
    "拒绝",
    "禁止",
    "取消",
    "do not",
    "don't",
    "deny",
    "decline",
    "cancel",
)
_DIALOG_GRANT_CLAUSE_SEPARATORS = (
    "并",
    "然后",
    "且",
    "和",
    "以及",
    "同时",
    "，",
    "。",
    "；",
    ",",
    ";",
    "and",
    "then",
)
_STARTUP_AD_CLOSE_MARKERS = (
    "关闭",
    "跳过",
    "进入",
    "下次",
    "稍后",
    "close",
    "dismiss",
    "skip",
    "enter",
    "later",
)
_PIXEL_CROSS_CLOSE_MARKER = "当前截图唯一孤立 X 像素结构校准"
_PERMISSION_DIALOG_MARKERS = (
    "permission",
    "权限",
    "file_access",
    "storage",
    "camera",
    "microphone",
    "record_audio",
    "notification",
)


class SurfacePhaseRunRepository(SurfacePhaseRepository, Protocol):
    """阶段装饰器需要的持久化能力。"""

    async def start_run(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind,
        phase_key: str,
        phase_protocol_version: str,
        idempotency_key: str,
        source_capture_id: str,
        context_payload: Mapping[str, Any],
        permit,
    ) -> SurfacePhaseRunSnapshot: ...

    async def get_run(self, *, phase_run_id: str) -> SurfacePhaseRunSnapshot | None: ...


class SurfacePhaseAwareExplorer:
    """在普通 Explorer 前优先处理启动广告、弹窗、认证门禁和系统弹窗。"""

    def __init__(
        self,
        *,
        delegate: ExplorerDecisionPort,
        repository: SurfacePhaseRunRepository,
        session_command_gate: SessionCommandGate,
    ) -> None:
        self._delegate = delegate
        self._repository = repository
        self._orchestrator = SurfacePhaseTurnOrchestrator(repository=repository)
        self._session_command_gate = session_command_gate

    async def decide_next(
        self,
        work,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal:
        if not isinstance(work, ExplorationWork):
            return await self._delegate.decide_next(work)
        ad_landing_decision = await self._try_signed_ad_landing(work)
        if ad_landing_decision is not None:
            return ad_landing_decision
        phase_decision = await self._try_surface_phase(work)
        if phase_decision is None:
            return await self._delegate.decide_next(work)
        return phase_decision

    async def decide_deterministic(
        self,
        work,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal | None:
        """首广告取证优先一次，其余情况先执行阶段机。"""

        if not isinstance(work, ExplorationWork):
            return None
        ad_landing_decision = await self._try_signed_ad_landing(work)
        if ad_landing_decision is not None:
            return ad_landing_decision
        phase_decision = await self._try_surface_phase(work)
        if phase_decision is not None:
            return phase_decision
        decide = getattr(self._delegate, "decide_deterministic", None)
        if not callable(decide):
            return None
        return await decide(work)

    async def _try_signed_ad_landing(
        self,
        work: ExplorationWork,
    ) -> ActionIntent | None:
        """让平台签名的首广告取证先于启动广告／弹窗清障阶段执行。"""

        metadata = work.frontier.observation.metadata
        # 一生一次：已调度过则禁止再截广告源／落地，把回合留给覆盖巡检。
        raw_attempts = metadata.get("app_ad_landing_attempt_count")
        if (
            isinstance(raw_attempts, int)
            and not isinstance(raw_attempts, bool)
            and raw_attempts > 0
        ):
            return None
        if coverage_incomplete_allows_bottom_nav_through_overlay(
            coverage_metadata=metadata,
        ):
            return None
        signed_candidate_ids: set[str] = set()
        for candidate in work.frontier.candidates:
            parameters = candidate.metadata.get("action_parameters")
            if (
                candidate.metadata.get("app_ad_landing_consistency") is True
                and isinstance(parameters, Mapping)
                and parameters.get("app_ad_landing_contract")
                == "app-ad-landing-consistency.v1"
            ):
                signed_candidate_ids.add(candidate.candidate_id)
        if not signed_candidate_ids:
            return None
        decide = getattr(self._delegate, "decide_deterministic", None)
        if not callable(decide):
            return None
        decision = await decide(work)
        if not isinstance(decision, ActionIntent):
            return None
        candidate_id = decision.parameters.get("candidate_id")
        if candidate_id not in signed_candidate_ids:
            return None
        return decision

    async def decide_after_deterministic(
        self,
        work,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal:
        """阶段机已由 Runner 检查，本次只调用普通 Explorer。"""

        return await self._delegate.decide_next(work)

    async def _try_surface_phase(
        self,
        work: ExplorationWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal | None:
        # 外部包的 popup／浏览器页只能先由普通 Explorer 的包边界门禁签发
        # BACK；Surface 阶段不得把翻译条、弹层等视觉候选抢在恢复前交给 Runtime。
        if not work_within_app_package_boundary(work):
            return None
        if (
            _uses_dynamic_app_frontier(work)
            and _work_is_payment_or_paywall_surface(work)
            and _first_close_candidate(work, phase_key="popup_chain") is None
            and _first_age_confirm_candidate(work) is None
        ):
            # 付费页不证明存在可关闭半屏，普通返回和导航交给有限探索决策。
            return None
        run = await self._load_or_start_phase_run(work)
        if run is None:
            return None
        page_detection = _page_detection_from_work(
            work,
            phase_key=run.phase_key,
        )
        turn = await self._orchestrator.run_turn(
            run=run,
            page_detection_result=page_detection,
            observed_at=work.frontier.observation.observed_at,
            action_deadline=work.deadline,
        )
        finished = turn.turn_result.finished_run
        if finished is not None:
            if finished.status is SurfacePhaseRunStatus.COMPLETED:
                # 开屏刚转到弹窗等 follow-up：同帧立刻开下一阶段，
                # 禁止落到普通 Explorer 点「关闭弹层下方X」。
                next_work = _work_without_surface_phase_run_id(work)
                next_key = _classify_phase(next_work)
                if next_key is not None and next_key != run.phase_key:
                    return await self._try_surface_phase(next_work)
                return None
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BLOCKED,
                reason=_blocked_phase_reason(finished),
            )
        if turn.action_handoff is None:
            return None
        if turn.action_handoff.kind is SurfacePhaseActionHandoffKind.ACTION:
            intent = turn.action_handoff.action_intent
            if intent is None:
                return None
            return _normalize_phase_action_intent(
                intent,
                work=work,
                phase_run_id=turn.action_handoff.phase_run_id,
                decision_id=turn.action_handoff.decision_id,
                original_capture_roles=_capture_roles_from_intent(intent),
            )
        if turn.action_handoff.kind is SurfacePhaseActionHandoffKind.TIMED_CAPTURE:
            request = turn.action_handoff.timed_capture
            if request is None:
                return None
            return TimedObservationIntent(
                task_id=request.task_id,
                session_id=request.session_id,
                phase_run_id=request.phase_run_id,
                phase_key=request.phase_key,
                decision_id=request.decision_id,
                observation_id=build_timed_observation_id(
                    phase_run_id=request.phase_run_id,
                    decision_id=request.decision_id,
                    source_capture_id=request.source_capture_id,
                ),
                source_capture_id=request.source_capture_id,
                surface_kind=work.surface_kind,
                delay_ms=request.delay_ms,
                directive=CaptureDirective(
                    role="observation",
                    artifact_types=request.directive.artifact_types,
                    inherit_parent_semantics=True,
                ),
                deadline=work.deadline,
                protocol_version=TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V2,
            )
        return None

    async def _load_or_start_phase_run(
        self,
        work: ExplorationWork,
    ) -> SurfacePhaseRunSnapshot | None:
        metadata = dict(work.frontier.observation.metadata)
        phase_run_id = _optional_text(metadata.get("surface_phase_run_id"))
        if phase_run_id is not None:
            run = await self._repository.get_run(phase_run_id=phase_run_id)
            if run is not None and run.status is SurfacePhaseRunStatus.RUNNING:
                if not _surface_phase_matches_current_curriculum(
                    work,
                    phase_key=run.phase_key,
                ):
                    return None
                counter_keys = {
                    "startup_ad_countdown": (
                        "countdown_observation_count",
                        "close_attempt_count",
                    ),
                    "popup_chain": (
                        "closed_popup_count",
                        "reobserve_count",
                        "same_popup_attempt_count",
                    ),
                    "auth_gate": ("dismiss_attempt_count", "reobserve_count"),
                    "system_dialog": ("handled_dialog_count", "reobserve_count"),
                }.get(run.phase_key, ())
                if counter_keys:
                    context_payload = dict(run.context_payload)
                    for key in counter_keys:
                        value = metadata.get(f"surface_phase_{key}")
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            context_payload[key] = value
                    run = replace(run, context_payload=context_payload)
                return run

        phase_key = _classify_phase(work)
        if phase_key is None:
            return None
        if not _surface_phase_matches_current_curriculum(work, phase_key=phase_key):
            return None
        protocol = _PHASE_PROTOCOL_BY_KEY[phase_key]
        context = _initial_context(phase_key)
        idempotency_key = "surface-phase:" + stable_protocol_hash(
            {
                "task_id": work.task_id,
                "session_id": work.session_id,
                "phase_key": phase_key,
                "source_capture_id": work.source_capture_id,
            }
        )
        async with self._session_command_gate.hold(
            task_id=work.task_id,
            session_id=work.session_id,
            surface_kind=work.surface_kind,
        ) as permit:
            return await self._repository.start_run(
                task_id=work.task_id,
                session_id=work.session_id,
                surface_kind=work.surface_kind,
                phase_key=phase_key,
                phase_protocol_version=protocol,
                idempotency_key=idempotency_key,
                source_capture_id=work.source_capture_id,
                context_payload=context,
                permit=permit,
            )


def _classify_phase(work: ExplorationWork) -> str | None:
    observation = work.frontier.observation
    page_type = _normalized(observation.page_type)
    page_state = _normalized(observation.page_state)
    facts = _surface_facts_for_observation(observation)
    dialog_kind = _normalized(facts.get("dialog_kind"))
    # 系统权限弹窗可叠在开屏之上：必须先于 startup_ad 消化，避免被倒计时阶段吞掉。
    if _is_system_dialog_observation(observation):
        return "system_dialog"
    # 年龄确认虽常带 popup_visible，但仍属认证门禁清障，优先走 auth_gate。
    if (
        dialog_kind in {"age_verification", "age_confirm", "age_gate"}
        or page_type in _AUTH_STATES
        or page_state in _AUTH_STATES
    ):
        return "auth_gate"
    # 线路恢复证据：startup_ad 文案但无遮挡事实时，交还普通 Explorer，不进广告阶段。
    if _has_startup_connection_recovery_evidence(work):
        return None
    # 开屏硬优先于 popup_chain：Vision 常给 startup_ad／全屏推广打 overlay，
    # 若先走弹窗会点底栏「<」，开屏上系统返回直接退出到桌面。
    if page_type == "startup_ad" or page_state in _STARTUP_STATES:
        return "startup_ad_countdown"
    roles = {
        candidate.metadata.get("control_role")
        for candidate in work.frontier.candidates
        if _candidate_is_current_and_safe(candidate, work)
    }
    if "skip" in roles:
        return "startup_ad_countdown"
    # 全屏开屏被标 unknown／overlay 且有「进入」：必须先于 popup_chain。
    if _work_has_startup_enter_signal(work):
        return "startup_ad_countdown"
    if _is_popup_observation(observation):
        return "popup_chain"
    if "dismiss" in roles:
        return "popup_chain"
    return None


def _has_startup_connection_recovery_evidence(work: ExplorationWork) -> bool:
    """当前帧有低风险线路恢复事实时，不把 startup_ad 文本误分类交给广告阶段。"""

    facts = _surface_facts_for_observation(work.frontier.observation)
    topmost_ad = facts.get("topmost_ad_popup")
    if (
        work.frontier.observation.overlay_signature is not None
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
        or facts.get("countdown_remaining_ms") is not None
        or (isinstance(topmost_ad, Mapping) and topmost_ad.get("is_ad") is True)
    ):
        return False
    markers = (
        "备用线路",
        "切换线路",
        "重新检测",
        "重试连接",
        "重新连接",
        "网络重试",
        "backup line",
        "alternate line",
        "retry connection",
        "reconnect",
    )
    for candidate in work.frontier.candidates:
        if candidate.metadata.get("startup_connection_recovery_evidence") is True:
            return True
        if (
            candidate.risk_level is InteractionRiskLevel.LOW
            and candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        ):
            terms = " ".join(
                (
                    candidate.semantic_target,
                    str(candidate.metadata.get("purpose") or ""),
                    str(candidate.metadata.get("expected_state_hint") or ""),
                )
            ).casefold()
            if any(marker.casefold() in terms for marker in markers):
                return True
    return False


def _page_detection_from_work(
    work: ExplorationWork,
    *,
    phase_key: str,
) -> dict[str, Any]:
    observation = work.frontier.observation
    metadata = dict(observation.metadata)
    surface_facts = _surface_facts_for_observation(observation)

    def surface_fact(key: str) -> Any:
        """优先读取视觉分析事实，同时兼容旧任务的顶层元数据。"""

        return surface_facts.get(key, metadata.get(key))

    page_type = _optional_text(observation.page_type)
    page_state = _optional_text(observation.page_state)
    if page_type in _POPUP_STATES and page_state is None:
        page_state = "popup"
    if page_type in _AUTH_STATES and page_state is None:
        page_state = page_type
    if page_type in _SYSTEM_DIALOG_STATES and page_state is None:
        page_state = page_type
    # 已判定走开屏阶段时，只把「仍像开屏」的 overlay／unknown 归一成 startup_ad，
    # 避免误 followup → cloud_back 退桌面。真弹窗／官方公告不得再伪装成开屏；
    # home／业务页不得强行改回 startup_ad，否则阶段永远完不成、点不到「我知道了」。
    if phase_key == "startup_ad_countdown":
        normalized_type = (page_type or "").casefold()
        normalized_state = (page_state or "").casefold()
        if _is_clear_post_startup_popup_frame(work, page_type=page_type, page_state=page_state):
            if normalized_type not in _POPUP_STATES:
                page_type = "popup"
            if normalized_state not in {"popup", "modal", "dialog"}:
                page_state = "popup"
        elif normalized_type in {"", "unknown", "none", "overlay"} and normalized_state not in {
            "startup_ad",
            "countdown",
            "splash",
            "popup",
            "modal",
            "dialog",
            "stable",
            "home",
        }:
            page_type = "startup_ad"
            page_state = "startup_ad"
    system_dialog = _is_system_dialog_observation(observation)
    age_confirm_candidate = _first_age_confirm_candidate(work)
    close_candidate = (
        _first_close_candidate(work, phase_key=phase_key) or age_confirm_candidate
    )
    dialog_candidate = _first_safe_dialog_candidate(work) if system_dialog else None
    action_candidate = dialog_candidate if system_dialog else close_candidate
    permission_dialog = _is_permission_dialog_observation(
        page_type=page_type,
        page_state=page_state,
        dialog_kind=surface_fact("dialog_kind"),
    )
    action_target = (
        dialog_candidate.semantic_target
        if dialog_candidate is not None
        else surface_fact("action_semantic_target")
    )
    if permission_dialog:
        # 权限弹窗只允许点击当前截图中明确识别出的低风险拒绝/稍后候选。
        # 即使模型误报 allow，也不能把“去开启”等授权入口交给 Runtime。
        action_control_visible = dialog_candidate is not None
        action_control_enabled = dialog_candidate is not None
        action_target = dialog_candidate.semantic_target if dialog_candidate else None
    else:
        action_control_visible = bool(surface_fact("action_control_visible")) or (
            action_candidate is not None
        )
        action_control_enabled = bool(surface_fact("action_control_enabled")) or (
            action_candidate is not None
        )
    action_policy = surface_fact("action_policy")
    if dialog_candidate is not None and _is_safe_dialog_candidate(dialog_candidate):
        action_policy = "deny"
    evidence_ids = [
        item
        for item in (
            metadata.get("screenshot_artifact_id"),
            metadata.get("ui_snapshot_artifact_id"),
        )
        if isinstance(item, str) and item.strip()
    ]
    return {
        "page_capture_id": work.source_capture_id,
        "evidence_artifact_ids": evidence_ids,
        "page_type": page_type,
        "page_state": page_state,
        "countdown_remaining_ms": _nonnegative_int(surface_fact("countdown_remaining_ms")),
        "close_control_visible": bool(surface_fact("close_control_visible"))
        or close_candidate is not None,
        "close_control_enabled": bool(surface_fact("close_control_enabled"))
        or close_candidate is not None,
        "close_semantic_target": (
            close_candidate.semantic_target
            if close_candidate is not None
            else surface_fact("close_semantic_target")
        ),
        "dismiss_control_visible": bool(surface_fact("dismiss_control_visible"))
        or close_candidate is not None,
        "dismiss_control_enabled": bool(surface_fact("dismiss_control_enabled"))
        or close_candidate is not None,
        "dismiss_semantic_target": (
            close_candidate.semantic_target
            if close_candidate is not None
            else surface_fact("dismiss_semantic_target")
        ),
        "action_control_visible": (
            action_control_visible
            if permission_dialog
            else bool(surface_fact("action_control_visible")) or action_control_visible
        ),
        "action_control_enabled": (
            action_control_enabled
            if permission_dialog
            else bool(surface_fact("action_control_enabled")) or action_control_enabled
        ),
        "action_semantic_target": action_target,
        "top_popup_signature": observation.overlay_signature,
        "popup_visible": (
            bool(surface_fact("popup_visible"))
            or page_state in _POPUP_STATES
            or (phase_key == "popup_chain" and close_candidate is not None)
        ),
        "auth_gate_visible": surface_fact("auth_gate_visible"),
        "login_form_visible": surface_fact("login_form_visible"),
        "requires_auth": surface_fact("requires_auth"),
        "guest_entry_visible": surface_fact("guest_entry_visible"),
        "guest_entry_enabled": surface_fact("guest_entry_enabled"),
        "guest_entry_semantic_target": surface_fact("guest_entry_semantic_target"),
        "dialog_visible": surface_fact("dialog_visible"),
        "dialog_kind": surface_fact("dialog_kind"),
        "action_policy": action_policy,
        "candidate_actions": [
            {
                "action": candidate.action_type.value,
                "target": candidate.semantic_target,
                "purpose": candidate.metadata.get("purpose"),
                "target_keywords": candidate.metadata.get("target_keywords", ()),
            }
            for candidate in work.frontier.candidates
        ],
    }


def _normalize_phase_action_intent(
    intent: ActionIntent,
    *,
    work: ExplorationWork,
    phase_run_id: str,
    decision_id: str,
    original_capture_roles: tuple[str, ...],
) -> ActionIntent:
    candidate = _match_candidate(work, intent)
    parameters = dict(intent.parameters)
    if candidate is not None:
        parameters["candidate_id"] = candidate.candidate_id
        action_type = candidate.action_type
        attach_synthetic_touch_contracts(parameters, candidate.metadata)
        # 合成合同校验 intent.purpose；阶段机默认文案不得覆盖候选用途。
        purpose = str(candidate.metadata.get("purpose") or intent.purpose)
    else:
        action_type = intent.action_type
        purpose = intent.purpose
    parameters["surface_phase_run_id"] = phase_run_id
    parameters["surface_phase_key"] = parameters.get("phase_key")
    parameters["surface_phase_decision_id"] = decision_id
    parameters["surface_phase_capture_roles"] = list(original_capture_roles)
    if work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        curriculum_phase_id = _current_app_curriculum_phase_id(work)
        if curriculum_phase_id is not None:
            parameters["app_curriculum_phase_id"] = curriculum_phase_id
    return replace(
        intent,
        action_type=action_type,
        purpose=purpose,
        capture_profile=_app_safe_capture_profile(intent.capture_profile),
        parameters=parameters,
    )


def _current_app_curriculum_phase_id(work: ExplorationWork) -> str | None:
    """读取当前 App 课纲游标，供 Surface Phase 动作绑定同一推进上下文。"""

    raw_progress = work.frontier.observation.metadata.get("app_curriculum_progress")
    if isinstance(raw_progress, Mapping):
        progress = AppCurriculumProgress.from_dict(raw_progress)
    else:
        playbook = resolve_playbook_by_id(
            extract_playbook_id_from_objective(work.objective)
        )
        if playbook is None:
            return None
        progress = build_app_curriculum_progress(playbook)
    return progress.current_phase_id


def _surface_phase_matches_current_curriculum(
    work: ExplorationWork,
    *,
    phase_key: str,
) -> bool:
    """广告与弹层是全局遮挡，必须先于业务课纲清空。"""

    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return True
    if phase_key not in {"startup_ad_countdown", "popup_chain"}:
        return True
    if phase_key == "popup_chain" and _has_safe_missing_post_publish_overlay_branch(work):
        # 发帖类型选择层是当前业务页内的安全分支，不是待关闭的广告。若继续让
        # popup_chain 抢占，阶段机会反复签发 BACK，普通 Explorer 永远无法进入
        # 缺失的 post_publish 编辑页。这里只让当前截图的低风险选择入口让路；
        # 最终提交发布仍不满足下方谓词，也不会绕过副作用门禁。
        return False
    if phase_key == "popup_chain" and _has_real_overlay_yield_seek(work):
        # 弹层反复关不掉且当前帧已有真实底栏／覆盖寻页时，让普通 Explorer
        # 执行寻页，避免 popup_chain 永久签发关层直至任务 review。
        return False
    # 当前截图已经由视觉层确认存在广告／弹层时，Surface Phase 只能签发
    # 当前帧低风险关闭候选或定时重观察，不会放行业务导航和广告本体点击。
    # 因此它必须能在课纲完成后再次重入，也必须能先清掉意外提前出现的遮挡。
    # 课纲匹配仍由 _next_curriculum_progress 决定是否推进，清障动作不能伪造进度。
    return True


def _has_real_overlay_yield_seek(work: ExplorationWork) -> bool:
    """弹层达让路帧数，或覆盖缺口未完且有真实底栏时，交还普通 Explorer。

    与 `app_screenshot_exploration._retain_only_obstruction_recovery_candidates`
    对齐：覆盖分母仍有缺口时不必等满 6 帧，否则 popup_chain 会反复关层误点外链，
    把已排队的底栏 sibling 拖到技术失败后整批取消。

    真弹窗（page_state=popup／popup_visible）在未满让路帧数前不得提早让路，
    否则普通 Explorer 会跳过 Surface Phase 的 cascade 关层。
    """

    observation = work.frontier.observation
    metadata = dict(observation.metadata)
    # observation.metadata 偶发缺顶层 required 时，回退 app_goal，避免提早让路永久关死。
    if "required_page_types" not in metadata and work.app_goal is not None:
        metadata["required_page_types"] = list(work.app_goal.required_page_types)
    coverage_scope_declared = work.app_goal is not None or any(
        key in metadata
        for key in (
            "required_page_types",
            "app_flow_contract",
            "app_curriculum_progress",
        )
    )
    early_bottom = coverage_scope_declared and (
        coverage_incomplete_allows_bottom_nav_through_overlay(
            coverage_metadata=metadata,
        )
    )
    page_state = str(observation.page_state or "").strip().casefold()
    facts = observation.metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    full_budget = overlay_budget_allows_real_coverage_seek(
        page_type=observation.page_type,
        coverage_metadata=metadata,
    )
    hard_popup = (
        page_state in _POPUP_STATES
        or dialog_kind
        in {
            "ad_popup",
            "promo_popup",
            "promo_ad",
            "modal",
            "dialog",
        }
        or facts.get("popup_visible") is True
        or observation.overlay_signature is not None
    )
    # 真弹窗未满让路帧：必须 popup_chain → 底栏「<」，禁止 Explorer 点下方 X。
    if hard_popup and not full_budget:
        return False
    # 详情返回误标 overlay、且无 ad_popup：直接交还 Explorer 点底栏，禁止 popup_chain 瞎关。
    soft_overlay = page_state == "overlay" and dialog_kind not in {
        "ad_popup",
        "promo_popup",
        "promo_ad",
        "modal",
        "dialog",
        "system_dialog",
        "age_verification",
        "age_confirm",
        "age_gate",
    }
    if soft_overlay and early_bottom:
        return True
    if not full_budget and not early_bottom:
        return False
    page_type = str(observation.page_type or "").strip().casefold()
    # Surface Phase Work 常只保留 dismiss；根业务页 + 覆盖缺口未完时直接让路，
    # 不要求当前阶段机候选池已含底栏（全局 Frontier 可能已排队 sibling）。
    if early_bottom and page_type in {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
    }:
        return True
    for candidate in work.frontier.candidates:
        if candidate.metadata.get("coverage_page_seek_contract") is not None:
            continue
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        if candidate.risk_level not in {
            InteractionRiskLevel.LOW,
            InteractionRiskLevel.MEDIUM,
        }:
            continue
        if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
            continue
        if full_budget:
            if (
                candidate.metadata.get("home_category_grid_seek") is True
                or candidate.metadata.get("home_top_category_tab_seek") is True
                or candidate.metadata.get("bottom_navigation_tab_seek") is True
                or candidate.metadata.get("coverage_page_seek") is True
                or candidate.metadata.get("frontier_scope") == "app"
            ):
                return True
            continue
        # 提早让路：只放行底栏，不把分栏目／一般覆盖寻页提前插队。
        if candidate.metadata.get("home_category_grid_seek") is True:
            continue
        if candidate.metadata.get("home_top_category_tab_seek") is True:
            continue
        if (
            candidate.metadata.get("bottom_navigation_tab_seek") is True
            or candidate.metadata.get("frontier_scope") == "app"
        ):
            return True
    return False


def _has_safe_missing_post_publish_overlay_branch(work: ExplorationWork) -> bool:
    """判断当前弹层是否包含进入缺失发帖编辑页的安全业务入口。"""

    if work.app_goal is None:
        return False
    observation = work.frontier.observation
    if _normalized(observation.page_type) not in {"post_list", "content_list"}:
        return False
    required = {item.casefold() for item in work.app_goal.required_page_types}
    covered = {
        str(item).strip().casefold()
        for item in observation.metadata.get("covered_page_types", ())
        if isinstance(item, str) and item.strip()
    }
    if "post_publish" not in required or "post_publish" in covered:
        return False
    for candidate in work.frontier.candidates:
        if (
            candidate.action_type
            not in {InteractionOperation.CLICK, InteractionOperation.TAP}
            or candidate.risk_level is not InteractionRiskLevel.LOW
            or candidate.metadata.get("frontier_scope") not in {"local", "page"}
            or _normalized(candidate.metadata.get("expected_next_page_type"))
            != "post_publish"
            or not _candidate_matches_current_capture(candidate, work)
        ):
            continue
        terms = " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ).casefold()
        if is_visual_obstruction_control(candidate) or any(
            marker in terms
            for marker in (
                "提交发布",
                "确认发布",
                "立即发布",
                "submit",
                "confirm publish",
            )
        ):
            continue
        if any(marker in terms for marker in ("图文帖子", "视频帖子", "发帖", "post")):
            return True
    return False


def _app_safe_capture_profile(profile: CaptureProfile) -> CaptureProfile:
    return CaptureProfile(
        profile_id=profile.profile_id,
        before=CaptureDirective(
            role="before_action",
            artifact_types=profile.before.artifact_types,
            inherit_parent_semantics=profile.before.inherit_parent_semantics,
        ),
        after=CaptureDirective(
            role="after_action",
            artifact_types=profile.after.artifact_types,
            inherit_parent_semantics=profile.after.inherit_parent_semantics,
        ),
    )


def _blocked_phase_reason(finished: SurfacePhaseRunSnapshot) -> str:
    if finished.terminal_reason_code == "supplemental_evidence_disabled":
        return (
            "evidence_insufficient：Surface 阶段需要补充观测，"
            "但补采功能当前已关闭（supplemental_evidence_disabled）"
        )
    return f"Surface 阶段 {finished.phase_key} 阻断：{finished.terminal_reason_code}"


def _capture_roles_from_intent(intent: ActionIntent) -> tuple[str, ...]:
    return (intent.capture_profile.before.role, intent.capture_profile.after.role)


def _match_candidate(work: ExplorationWork, intent: ActionIntent):
    for candidate in work.frontier.candidates:
        current = (
            _candidate_is_current_and_safe(candidate, work)
            or (
            is_age_confirm_candidate(
                action_type=candidate.action_type,
                risk_level=candidate.risk_level,
                semantic_target=candidate.semantic_target,
                purpose=candidate.metadata.get("purpose"),
                expected_state_hint=candidate.metadata.get("expected_state_hint"),
            )
        )
            and _candidate_is_current_for_age_confirm(candidate, work)
        )
        if (
            current
            and _phase_action_types_match(candidate.action_type, intent.action_type)
            and candidate.semantic_target == intent.semantic_target
        ):
            return candidate
    return None


def _is_startup_enter_or_skip_candidate(candidate: CandidateAction) -> bool:
    """开屏阶段只认进入／跳过，不认弹层下方／角落 X。"""

    if candidate.metadata.get("startup_ad_skip_fallback") is True:
        return True
    if str(candidate.metadata.get("expected_state_hint") or "").casefold() == "tap_skip":
        return True
    if candidate.metadata.get("control_role") == "skip":
        return True
    semantic = candidate.semantic_target.casefold()
    purpose = str(candidate.metadata.get("purpose") or "").casefold()
    return any(
        marker in semantic or marker in purpose
        for marker in ("进入", "跳过", "skip", "enter", "entry")
    ) and not (
        candidate.metadata.get("popup_close_corner_fallback") is True
        or candidate.metadata.get("popup_close_bottom_x_fallback") is True
        or str(candidate.metadata.get("expected_state_hint") or "").casefold()
        == "close_popup_x"
    )


def _is_popup_corner_or_bottom_x_candidate(candidate: CandidateAction) -> bool:
    return (
        candidate.metadata.get("popup_close_corner_fallback") is True
        or candidate.metadata.get("popup_close_bottom_x_fallback") is True
        or str(candidate.metadata.get("expected_state_hint") or "").casefold()
        == "close_popup_x"
        or "关闭弹层下方" in candidate.semantic_target
        or "关闭弹层右上" in candidate.semantic_target
    )


def _is_notice_ack_candidate(candidate: CandidateAction) -> bool:
    """官方公告确认钮：点「我知道了」等，不走广告 cloud_back／找 ×。"""

    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if _is_popup_corner_or_bottom_x_candidate(candidate):
        return False
    try:
        from services.api.app.domain.app_product.popup_close import (
            is_notice_ack_semantic_target,
        )
    except Exception:  # noqa: BLE001
        is_notice_ack_semantic_target = None  # type: ignore[assignment]
    blob = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    )
    if is_notice_ack_semantic_target is not None:
        return bool(is_notice_ack_semantic_target(blob))
    return "我知道了" in blob or "知道了" in blob


def _is_clear_post_startup_popup_frame(
    work: ExplorationWork,
    *,
    page_type: str | None,
    page_state: str | None,
) -> bool:
    """开屏阶段内已出现真弹窗／公告：不得再伪装成 startup_ad。"""

    normalized_type = (page_type or "").casefold()
    normalized_state = (page_state or "").casefold()
    if normalized_state in {"popup", "modal", "dialog"}:
        return True
    if normalized_type in {"popup", "modal", "dialog"}:
        return True
    observation = work.frontier.observation
    blob = " ".join(
        (
            str(observation.overlay_signature or ""),
            " ".join(observation.key_elements or ()),
            str(dict(observation.metadata).get("visual_summary") or ""),
        )
    )
    notice_markers = ("官方公告", "我知道了", "知道了", "system_notice", "notice_ack")
    ad_popup_markers = (
        "关闭弹层下方",
        "关闭弹层右上",
        "宫格广告",
        "广告宫格",
        "grid_ad",
        "圆形关闭",
        "bottom_close_x",
        "promo_grid",
    )
    if any(marker in blob for marker in notice_markers):
        return True
    if any(marker in blob for marker in ad_popup_markers):
        return True
    return any(
        _is_notice_ack_candidate(candidate)
        for candidate in work.frontier.candidates
        if _candidate_is_current_and_safe(candidate, work)
    )


def _work_has_startup_enter_signal(work: ExplorationWork) -> bool:
    """根帧全屏推广被标 unknown／overlay 时，凭进入／跳过候选或文案仍走开屏阶段。"""

    observation = work.frontier.observation
    page_type = _normalized(observation.page_type)
    page_state = _normalized(observation.page_state)
    if any(
        candidate.navigation_depth > 0
        for candidate in work.frontier.candidates
    ) and not any(
        candidate.navigation_depth <= 0
        for candidate in work.frontier.candidates
    ):
        return False
    ambiguous_root = page_type in {"", "unknown", "none", "startup_ad", "splash", "overlay"}
    startup_like_state = page_state in {"", "unknown", "none", *_STARTUP_STATES}
    for candidate in work.frontier.candidates:
        if not _candidate_is_current_and_safe(candidate, work):
            continue
        if not _is_startup_enter_or_skip_candidate(candidate):
            continue
        # 业务页「进入图文发帖」等不得伪装成开屏进入信号。
        semantic = candidate.semantic_target
        purpose = str(candidate.metadata.get("purpose") or "")
        blob_c = f"{semantic} {purpose}"
        has_skip_or_splash = any(
            marker in blob_c
            for marker in ("跳过", "开屏", "首屏", "startup", "splash", "skip")
        )
        if ambiguous_root or startup_like_state or has_skip_or_splash:
            return True
    blob = " ".join(
        (
            page_type or "",
            page_state or "",
            str(observation.overlay_signature or ""),
            " ".join(observation.key_elements or ()),
            str(observation.metadata.get("visual_summary") or ""),
        )
    )
    # 真弹窗宫格／下方 X：不是开屏。
    if any(
        marker in blob
        for marker in (
            "关闭弹层",
            "宫格广告",
            "圆形关闭",
            "bottom_close_x",
            "grid_ad_popup",
            "grid_promo_popup",
        )
    ):
        return False
    has_enter = any(
        marker in blob for marker in ("进入", "跳过", "开屏", "首屏", "startup", "splash")
    )
    has_fullscreen_promo = any(
        marker in blob
        for marker in ("全屏", "推广广告", "赌博", "博彩", "未见关闭")
    )
    if page_state in _POPUP_STATES or page_type in _POPUP_STATES:
        # overlay／popup 标签但有进入／全屏推广：按开屏，避免 cloud_back 退桌面。
        return has_enter or (has_fullscreen_promo and "关闭弹层" not in blob)
    if page_type not in {"", "unknown", "none", "startup_ad", "splash"}:
        if page_state not in {"", "unknown", "none", *_STARTUP_STATES}:
            return False
    return has_enter or has_fullscreen_promo


def _work_without_surface_phase_run_id(work: ExplorationWork) -> ExplorationWork:
    """去掉已结束阶段的 run id，便于同帧启动下一 Surface 阶段。"""

    observation = work.frontier.observation
    metadata = dict(observation.metadata)
    metadata.pop("surface_phase_run_id", None)
    metadata.pop("surface_phase_key", None)
    new_observation = replace(observation, metadata=metadata)
    new_frontier = replace(work.frontier, observation=new_observation)
    return replace(work, frontier=new_frontier)


def _first_close_candidate(
    work: ExplorationWork,
    *,
    phase_key: str,
):
    matches = []
    paywall_precheck = (
        not _uses_dynamic_app_frontier(work)
        and _work_is_payment_or_paywall_surface(work)
    )
    dynamic_paywall = (
        _uses_dynamic_app_frontier(work)
        and _work_is_payment_or_paywall_surface(work)
    )
    for candidate in work.frontier.candidates:
        if not _candidate_is_current_and_safe(candidate, work):
            continue
        if not _candidate_matches_close_phase(
            candidate, phase_key=phase_key, page_type=work.frontier.observation.page_type,
            work=work,
        ):
            continue
        semantic_text = (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
        )
        if (
            is_visual_obstruction_control(candidate)
            or candidate.metadata.get("popup_back_recovery") is True
            or candidate.metadata.get("payment_sheet_back") is True
            or (
                candidate.action_type is InteractionOperation.BACK
                and (
                    "返回关闭付费" in candidate.semantic_target
                    or "返回关闭付费" in str(candidate.metadata.get("purpose") or "")
                    or paywall_precheck
                )
            )
            or any(
                marker.casefold() in text.casefold()
                for text in semantic_text
                for marker in _STARTUP_AD_CLOSE_MARKERS
            )
        ):
            matches.append(candidate)
    paywall_surface = paywall_precheck
    if dynamic_paywall:
        # 动态付费：仅 BACK 交给 Explorer；有装饰 × 时 BACK 优先清障。
        back_tier = tuple(
            candidate
            for candidate in matches
            if candidate.metadata.get("payment_sheet_back") is True
            or candidate.metadata.get("popup_back_recovery") is True
            or candidate.action_type is InteractionOperation.BACK
        )
        click_tier = tuple(
            candidate
            for candidate in matches
            if candidate.action_type
            in {InteractionOperation.CLICK, InteractionOperation.TAP}
        )
        if not click_tier:
            return None
        tiers = (back_tier, click_tier, tuple(matches)) if back_tier else (
            click_tier,
            tuple(matches),
        )
    elif paywall_surface:
        # 付费半屏优先 BACK，禁止像素／角落假关闭抢占。
        tiers = (
            tuple(
                candidate
                for candidate in matches
                if candidate.metadata.get("payment_sheet_back") is True
                or candidate.metadata.get("popup_back_recovery") is True
                or candidate.action_type is InteractionOperation.BACK
            ),
            tuple(matches),
        )
    elif phase_key == "startup_ad_countdown":
        # 开屏：进入／跳过优先；弹层下方／右上 X 一律不进候选。
        matches = [
            candidate
            for candidate in matches
            if not _is_popup_corner_or_bottom_x_candidate(candidate)
        ]
        tiers = (
            tuple(
                candidate
                for candidate in matches
                if _is_startup_enter_or_skip_candidate(candidate)
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
            ),
            tuple(matches),
        )
    elif phase_key == "popup_chain":
        # Watcher cascade：公告/文案 → 几何安全 X → 蒙层 → 底栏「<」连点两下。
        tiers = (
            tuple(
                candidate
                for candidate in matches
                if _is_notice_ack_candidate(candidate)
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.action_type
                in {InteractionOperation.CLICK, InteractionOperation.TAP}
                and (
                    _is_popup_corner_or_bottom_x_candidate(candidate)
                    or "关闭" in candidate.semantic_target
                    or "關閉" in candidate.semantic_target
                    or "跳过" in candidate.semantic_target
                    or "跳過" in candidate.semantic_target
                    or str(candidate.metadata.get("control_role") or "").casefold()
                    in {"dismiss", "skip"}
                )
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.action_type
                in {InteractionOperation.CLICK, InteractionOperation.TAP}
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.action_type is InteractionOperation.BACK
                or "底部导航栏" in candidate.semantic_target
                or "cloud_back"
                in str(candidate.metadata.get("purpose") or "").casefold()
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.metadata.get("popup_back_recovery") is True
            ),
            tuple(matches),
        )
    else:
        tiers = (
            tuple(
                candidate
                for candidate in matches
                if candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
                and _PIXEL_CROSS_CLOSE_MARKER
                in str(candidate.metadata.get("purpose") or "")
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.metadata.get("popup_back_recovery") is True
            ),
            tuple(
                candidate
                for candidate in matches
                if candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
            ),
            tuple(matches),
        )
    ordered: list[CandidateAction] = []
    seen: set[str] = set()
    for tier in tiers:
        for candidate in tier:
            if candidate.candidate_id not in seen:
                seen.add(candidate.candidate_id)
                ordered.append(candidate)
    if not ordered:
        return None

    same_attempts = work.frontier.observation.metadata.get(
        "surface_phase_same_popup_attempt_count",
        0,
    )
    last_identity = work.frontier.observation.metadata.get(
        "surface_phase_last_obstruction_control_identity"
    )
    if (
        isinstance(same_attempts, int)
        and not isinstance(same_attempts, bool)
        and same_attempts > 0
        and isinstance(last_identity, str)
        and last_identity.strip()
    ):
        alternate = next(
            (
                candidate
                for candidate in ordered
                if _obstruction_control_identity(candidate) != last_identity
            ),
            None,
        )
        if alternate is not None:
            return alternate
    return ordered[0]


def _obstruction_control_identity(candidate: CandidateAction) -> str:
    value = candidate.metadata.get("obstruction_control_identity")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "obstruction-control-fallback:" + stable_protocol_hash(
        {
            "action_type": candidate.action_type.value,
            "control_role": candidate.metadata.get("control_role"),
            "semantic_target": candidate.semantic_target.casefold(),
            "expected_state_hint": candidate.metadata.get("expected_state_hint"),
        }
    )


def _candidate_matches_close_phase(
    candidate, *, phase_key: str, page_type: str | None = None,
    work: ExplorationWork | None = None,
) -> bool:
    """避免上一层阶段误消费下一层广告或弹窗候选。"""

    expected_hint = str(candidate.metadata.get("expected_state_hint") or "").casefold()
    semantic_target = candidate.semantic_target.casefold()
    purpose = str(candidate.metadata.get("purpose") or "").casefold()

    # 开屏与弹窗关层硬分离：弹层下方／右上 X 不得进入 startup_ad_countdown。
    if phase_key == "startup_ad_countdown" and _is_popup_corner_or_bottom_x_candidate(
        candidate
    ):
        return False

    if is_visual_obstruction_control(candidate):
        if phase_key == "startup_ad_countdown":
            # 开屏只接受 skip／进入，不接受弹窗 dismiss 圆 X。
            return _is_startup_enter_or_skip_candidate(candidate)
        return phase_key == "popup_chain"

    if (
        work is not None
        and _uses_dynamic_app_frontier(work)
        and _work_is_payment_or_paywall_surface(work)
        and candidate.action_type is InteractionOperation.BACK
    ):
        # 动态模式禁止无契约的普通 BACK；显式 payment_sheet_back 仍可清障。
        if candidate.metadata.get("payment_sheet_back") is not True:
            return False

    if phase_key == "popup_chain":
        if candidate.metadata.get("popup_back_recovery") is True:
            return True
        if candidate.metadata.get("payment_sheet_back") is True:
            return True
        paywall_surface = work is not None and _work_is_payment_or_paywall_surface(work)
        # 首页／开屏无明确恢复契约的 BACK 可能退出 App；业务弹窗的显式关闭仍可用。
        # 付费半屏／尊享卡允许 BACK（常无 X）。
        if candidate.action_type is InteractionOperation.BACK:
            if paywall_surface or _normalized(page_type) == "payment":
                return True
            # 产品 cloud_back：底栏「<」关弹窗（非 keyevent 4），home／开屏页也允许。
            if (
                "底部导航栏" in candidate.semantic_target
                or "cloud_back" in purpose
            ):
                return True
            if _normalized(page_type) in {
                "", "home", "startup_ad", "guide_page",
            }:
                return False
        if expected_hint == "tap_skip" or semantic_target == "系统返回关闭启动广告":
            return False
        if "返回关闭弹窗" in semantic_target or "返回关闭弹窗" in purpose:
            return paywall_surface
        if "返回关闭付费" in semantic_target or "返回关闭付费" in purpose:
            return True
        if paywall_surface and expected_hint == "close_popup_x":
            return False
        return expected_hint == "close_popup_x" or any(
            marker.casefold() in semantic_target or marker.casefold() in purpose
            for marker in _SAFE_DIALOG_DISMISS_MARKERS
        )
    if phase_key == "startup_ad_countdown":
        if candidate.metadata.get("popup_back_recovery") is True:
            return False
        if _is_startup_enter_or_skip_candidate(candidate):
            return True
        # 禁止仅因「关闭」二字把弹层 X 当成开屏进入。
        return expected_hint == "tap_skip" or any(
            marker.casefold() in semantic_target or marker.casefold() in purpose
            for marker in ("进入", "跳过", "skip", "enter", "entry")
        )
    return True


def _uses_dynamic_app_frontier(work: ExplorationWork) -> bool:
    policy = work.frontier.observation.metadata.get("app_runtime_policy")
    return isinstance(policy, Mapping) and policy.get("exploration_mode") == "dynamic_frontier"


def _work_is_payment_or_paywall_surface(work: ExplorationWork) -> bool:
    """从当前观测判断是否为付费半屏／解锁墙。"""

    observation = work.frontier.observation
    page_type = _normalized(observation.page_type)
    if page_type == "payment":
        return True
    facts = _surface_facts_for_observation(observation)
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {
        "unlock_paywall",
        "paywall",
        "purchase",
        "vip_unlock",
        "vip_purchase",
        "payment",
        "membership",
    }:
        return True
    summary = " ".join(
        (
            str(facts.get("dialog_title") or ""),
            str(facts.get("popup_title") or ""),
            str(observation.overlay_signature or ""),
        )
    )
    return any(
        marker in summary
        for marker in ("尊享卡", "立即去抢购", "限时特价", "解锁暗网")
    )


def _first_age_confirm_candidate(work: ExplorationWork):
    """识别「已满18岁」确认进入；允许 medium，因 Vision 常把年龄门禁标为中风险。"""

    for candidate in work.frontier.candidates:
        if not _candidate_is_current_for_age_confirm(candidate, work):
            continue
        if is_age_confirm_candidate(
            action_type=candidate.action_type,
            risk_level=candidate.risk_level,
            semantic_target=candidate.semantic_target,
            purpose=candidate.metadata.get("purpose"),
            expected_state_hint=candidate.metadata.get("expected_state_hint"),
        ):
            return candidate
    return None


def _first_safe_dialog_candidate(work: ExplorationWork):
    """从当前截图候选中选择低风险拒绝/稍后动作，不猜测坐标或授权入口。"""

    for candidate in work.frontier.candidates:
        if _candidate_is_current_and_safe(candidate, work) and _is_safe_dialog_candidate(
            candidate
        ):
            return candidate
    return None


def _is_safe_dialog_candidate(candidate) -> bool:
    target = candidate.semantic_target.casefold()
    purpose = str(candidate.metadata.get("purpose") or "").casefold()
    # 目标文本本身是动作证据，出现未被否定的授权词时一律拒绝；目的说明
    # 可以描述“暂不授权”这类拒绝动作，因此只拒绝其中未被否定的授权词。
    if _contains_unnegated_dialog_grant_marker(target) or _contains_unnegated_dialog_grant_marker(
        purpose
    ):
        return False
    return any(
        marker.casefold() in target or marker.casefold() in purpose
        for marker in _SAFE_DIALOG_DISMISS_MARKERS
    )


def _contains_unnegated_dialog_grant_marker(text: str) -> bool:
    """判断文本是否包含没有被同一语义分句否定的授权词。"""

    for marker in _UNSAFE_DIALOG_GRANT_MARKERS:
        normalized_marker = marker.casefold()
        search_from = 0
        while True:
            marker_start = text.find(normalized_marker, search_from)
            if marker_start < 0:
                break
            clause_prefix = _dialog_grant_clause_prefix(text, marker_start)
            normalized_prefix = clause_prefix.rstrip()
            directly_negated = normalized_prefix.endswith(("不", "not"))
            explicitly_negated = any(
                negation.casefold() in normalized_prefix
                for negation in _DIALOG_GRANT_NEGATION_MARKERS
            )
            if not directly_negated and not explicitly_negated:
                return True
            search_from = marker_start + len(normalized_marker)
    return False


def _dialog_grant_clause_prefix(text: str, marker_start: int) -> str:
    """返回授权词前、且未跨越并列或标点分隔符的语义分句。"""

    prefix = text[:marker_start]
    separator_start = max(
        (prefix.rfind(separator), separator)
        for separator in _DIALOG_GRANT_CLAUSE_SEPARATORS
    )
    if separator_start[0] < 0:
        return prefix
    return prefix[separator_start[0] + len(separator_start[1]) :]


def _candidate_is_current_and_safe(candidate, work: ExplorationWork) -> bool:
    if candidate.action_type not in _DIALOG_ACTION_TYPES:
        return False
    if candidate.risk_level is not InteractionRiskLevel.LOW:
        return False
    return _candidate_matches_current_capture(candidate, work)


def _candidate_is_current_for_age_confirm(candidate, work: ExplorationWork) -> bool:
    if candidate.action_type not in _DIALOG_ACTION_TYPES:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    return _candidate_matches_current_capture(candidate, work)


def _candidate_matches_current_capture(candidate, work: ExplorationWork) -> bool:
    if (
        candidate.expected_surface_kind is not None
        and candidate.expected_surface_kind is not work.surface_kind
    ):
        return False
    candidate_capture_id = candidate.metadata.get("source_capture_id")
    return candidate_capture_id in {None, work.source_capture_id}


def _phase_action_types_match(
    candidate_action_type: InteractionOperation,
    intent_action_type: InteractionOperation,
) -> bool:
    if candidate_action_type is intent_action_type:
        return True
    return {candidate_action_type, intent_action_type} == {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    } or (
        candidate_action_type is InteractionOperation.BACK
        and intent_action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
    )


def _is_system_dialog_observation(observation) -> bool:
    page_type = _normalized(observation.page_type)
    page_state = _normalized(observation.page_state)
    facts = _surface_facts_for_observation(observation)
    return (
        page_type in _SYSTEM_DIALOG_STATES
        or page_state in _SYSTEM_DIALOG_STATES
        or (
            facts.get("dialog_visible") is True
            and _is_permission_dialog_observation(
                page_type=page_type,
                page_state=page_state,
                dialog_kind=facts.get("dialog_kind"),
            )
        )
    )


def _is_popup_observation(observation) -> bool:
    page_type = _normalized(observation.page_type)
    page_state = _normalized(observation.page_state)
    # 开屏／倒计时不是弹窗：即使 popup_visible／overlay 为真也不得进 popup_chain。
    if page_type == "startup_ad" or page_state in _STARTUP_STATES:
        return False
    meta = dict(observation.metadata)
    blob = " ".join(
        (
            page_type or "",
            page_state or "",
            str(observation.overlay_signature or ""),
            " ".join(observation.key_elements or ()),
            str(meta.get("visual_summary") or ""),
        )
    )
    # 全屏开屏被标 overlay 且有「进入」：禁止当弹窗点「<」退桌面。
    if (
        any(marker in blob for marker in ("进入", "跳过", "开屏", "首屏"))
        and not any(
            marker in blob
            for marker in ("关闭弹层", "宫格广告", "圆形关闭", "grid_ad_popup")
        )
    ):
        return False
    facts = _surface_facts_for_observation(observation)
    return (
        page_type in _POPUP_STATES
        or page_state in _POPUP_STATES
        or facts.get("popup_visible") is True
        or observation.overlay_signature is not None
    )


def _surface_facts_for_observation(observation) -> dict[str, Any]:
    metadata = dict(observation.metadata)
    raw_surface_facts = metadata.get("surface_facts")
    return dict(raw_surface_facts) if isinstance(raw_surface_facts, Mapping) else {}


def _is_permission_dialog_observation(
    *,
    page_type: str | None,
    page_state: str | None,
    dialog_kind: object,
) -> bool:
    normalized_page_type = _normalized(page_type)
    normalized_page_state = _normalized(page_state)
    if normalized_page_type in {"permission_dialog", "browser_permission"}:
        return True
    if normalized_page_state in {"permission_dialog", "browser_permission"}:
        return True
    normalized_kind = _normalized(dialog_kind)
    return any(marker in normalized_kind for marker in _PERMISSION_DIALOG_MARKERS)


def _initial_context(phase_key: str) -> dict[str, object]:
    if phase_key == "startup_ad_countdown":
        return {
            "countdown_observation_count": 0,
            "close_attempt_count": 0,
        }
    if phase_key == "popup_chain":
        return {
            "closed_popup_count": 0,
            "reobserve_count": 0,
            "same_popup_attempt_count": 0,
        }
    if phase_key == "auth_gate":
        return {
            "dismiss_attempt_count": 0,
            "reobserve_count": 0,
        }
    if phase_key == "system_dialog":
        return {
            "handled_dialog_count": 0,
            "reobserve_count": 0,
        }
    raise ValueError(f"不支持的 Surface 阶段：{phase_key}")


def _optional_text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _normalized(value: object) -> str:
    text = _optional_text(value)
    return text.casefold().replace("-", "_") if text is not None else ""


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None
