"""APP 统一入口：有界主流程、无状态单帧工具、封闭决策输出。"""

from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.application.app_bounded_dismiss_tool import (
    _detect_dismiss_phase,
    _should_yield_to_business_popup,
    try_bounded_dismiss_tool,
)
from services.api.app.application.app_flow_decisions import flow_passive_decision
from services.api.app.application.app_flow_phase_budget import (
    charge_phase_time,
    phase_key,
    recover_phase_limit,
)
from services.api.app.application.app_tangguo_flow_driver import (
    read_tangguo_flow_state,
    try_tangguo_flow_driver,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    SurfaceKind,
    TimedObservationIntent,
)

MAX_ROUNDS = 160
MAX_PHASE_ROUNDS = 4
MAX_PAYMENT_ROUNDS = 6
MAX_PHASE_SECONDS = 600
MAX_FLOW_SECONDS = 3600
_ACTIONS = frozenset({
    InteractionOperation.CLICK, InteractionOperation.TAP, InteractionOperation.BACK,
    InteractionOperation.SCROLL, InteractionOperation.OBSERVE,
})
# 影片取证后已回首页：清障不得再挡住上方分类／底栏推进。
_HOME_CURRICULUM_PHASES = frozenset({
    "back_home",
    "top_tabs",
    "top_tab_restore",
    "bottom_tabs",
    "final_home",
})


def _allow_flow_after_dismiss(work, state, *, dismiss_phase) -> bool:
    """清障无动作时是否交还课纲；首页课纲阶段即使仍标弹层也要推进。"""

    if not dismiss_phase:
        return True
    page_type = str(work.frontier.observation.page_type or "").casefold()
    if page_type == "home" and state.phase in _HOME_CURRICULUM_PHASES:
        return True
    return False


def require_app_state_machine_decision(work, *, now=None):
    """APP 阶段控制权只属于状态机；无决策或非法输出不得退回自由规划。"""
    if not isinstance(work, ExplorationWork) or work.surface_kind not in {
        SurfaceKind.ANDROID, SurfaceKind.IOS,
    }:
        return None
    decision = decide_app_three_contracts(work, now=now)
    if isinstance(decision, (ActionIntent, TimedObservationIntent, ExplorationTerminal)):
        return decision
    return ExplorationTerminal(
        work_id=work.work_id, task_id=work.task_id,
        status=ExplorationTerminalStatus.BLOCKED,
        reason="app_state_machine_decision_missing_or_invalid",
    )


def decide_app_three_contracts(work, *, now=None, scripted=None):
    """每帧只产出一个动作、等待或终态；APP 不回退自由对话规划。"""
    if not isinstance(work, ExplorationWork) or work.surface_kind not in {
        SurfaceKind.ANDROID, SurfaceKind.IOS,
    }:
        return None
    if scripted is not None:
        from services.api.app.domain.exploration import ExplorationTerminalStatus

        return ExplorationTerminal(
            work_id=work.work_id, task_id=work.task_id,
            status=ExplorationTerminalStatus.BLOCKED, reason="legacy_product_script_disabled",
        )
    cached = getattr(work, "_three_contract_decision", None)
    if cached is not None:
        return cached
    now = now or datetime.now(UTC)
    state = read_tangguo_flow_state(work)
    state.budgets.device_actions_max = min(state.budgets.device_actions_max, 100)
    state.ensure_started_at(now_iso=now.isoformat())
    runtime = state.contract_runtime
    phase = state.phase
    key = phase_key(state)
    dismiss_phase = _detect_dismiss_phase(work)
    if _should_yield_to_business_popup(work):
        dismiss_phase = None
    if dismiss_phase:
        key = "dismiss:" + dismiss_phase + ":" + str(
            work.frontier.observation.overlay_signature or "",
        )
    counters = runtime.setdefault("rounds_by_phase", {})
    counters[key] = int(counters.get(key, 0)) + 1
    runtime["rounds"] = int(runtime.get("rounds", 0)) + 1
    reason = None
    try:
        active_seconds = charge_phase_time(runtime, key, now)
    except (TypeError, ValueError):
        active_seconds = MAX_PHASE_SECONDS
    if now >= work.deadline or work.budget.remaining_actions <= 0:
        reason = "work_budget_exhausted"
    elif runtime["rounds"] > MAX_ROUNDS:
        reason = "flow_round_limit"
    elif counters[key] > (
        (3 if dismiss_phase == "splash" else 2) if dismiss_phase
        else (MAX_PAYMENT_ROUNDS if phase == "payment_alipay" else MAX_PHASE_ROUNDS)
    ):
        reason = "phase_round_limit:" + phase
    else:
        try:
            elapsed = (now - datetime.fromisoformat(state.started_at_iso)).total_seconds()
            if elapsed >= MAX_FLOW_SECONDS:
                reason = "flow_timeout"
            elif active_seconds >= MAX_PHASE_SECONDS:
                reason = "phase_timeout:" + phase
        except (TypeError, ValueError):
            reason = "invalid_flow_clock"
    if reason:
        local = reason.startswith(("phase_timeout:", "phase_round_limit:"))
        recovered = local and not dismiss_phase and recover_phase_limit(state, reason)
        # 影片取证后的首页课纲：清障超限交还上方分类／底栏，禁止整单收口空转。
        # 开屏／首页确认阶段仍保持「无关闭点则收口」，避免点穿弹层后方业务。
        home_video_done = state.stage_results.get("home_video") in {
            "passed", "blocked", "failed",
        }
        if (
            not recovered
            and local
            and dismiss_phase
            and (state.phase in _HOME_CURRICULUM_PHASES or home_video_done)
        ):
            counters[key] = 0
            elapsed_map = runtime.setdefault("phase_elapsed_seconds", {})
            elapsed_map[key] = 0.0
            state.recovery_events.append(reason)
            recovered = True
            dismiss_phase = None
            key = phase_key(state)
            reason = None
        if not recovered and reason:
            state.mark_stage(phase, "blocked")
            state.stop_reason = reason
            state.advance("done")

    observation = replace(work.frontier.observation, metadata={
        **work.frontier.observation.metadata, "tangguo_flow_state": state.to_dict(),
    })
    from services.api.app.application.app_product_orchestration_policy import (
        is_fixed_script_parameters,
    )

    candidates = tuple(item for item in work.frontier.candidates
                       if not is_fixed_script_parameters({
                           **item.metadata, "candidate_id": item.candidate_id,
                       }))
    current = replace(work, frontier=replace(
        work.frontier, observation=observation, candidates=candidates,
    ))
    object.__setattr__(current, "_app_three_contracts", True)
    object.__setattr__(current, "_app_flow_now", now)
    action = None
    if state.phase != "done":
        action = try_bounded_dismiss_tool(current)
        if action is not None:
            if state.consume_device_action():
                action = replace(action, parameters={
                    **action.parameters, "tangguo_flow_state": state.to_dict(),
                })
            else:
                state.stop_reason = "budget_device_actions_exhausted"
                state.advance("done")
                action = None
        elif _allow_flow_after_dismiss(current, state, dismiss_phase=dismiss_phase):
            # 清障无动作（found:false／步数用尽／让路）必须交还课纲。
            # 已回首页的 back_home／分类／底栏阶段，即使帧上仍标 popup 也不得空转。
            action = try_tangguo_flow_driver(current)
            state = read_tangguo_flow_state(current)
    if isinstance(action, ActionIntent):
        if action.action_type not in _ACTIONS or (
            action.action_type not in work.boundaries.allowed_operations
        ):
            state.stop_reason = "action_outside_contract"
            state.mark_stage(phase, "blocked")
            state.advance("done")
            action = None
        else:
            action = replace(action, parameters={
                **action.parameters,
                "app_exploration_contract": "bounded-stateless-constrained.v1",
            })
    # 动作可能切换到子流程；下一帧耗时归属动作后的阶段，不继续计在父阶段。
    state.contract_runtime["active_phase_key"] = (
        key if dismiss_phase else phase_key(state)
    )
    state.contract_runtime["active_since"] = now.isoformat()
    if isinstance(action, ActionIntent):
        action = replace(action, parameters={
            **action.parameters, "tangguo_flow_state": state.to_dict(),
        })
    decision = action if action is not None else flow_passive_decision(current, state)
    object.__setattr__(work, "_tangguo_flow_state_cache", state)
    object.__setattr__(work, "_three_contract_decision", decision)
    return decision
