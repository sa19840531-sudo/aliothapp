"""基于 Microsoft Agent Framework 的 V2 Explorer Agent。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from agent_framework import Agent, Content, ContextProvider, Message, SupportsChatGetResponse
from pydantic import ValidationError

from services.api.app.agents.explorer.instructions import (
    build_explorer_system_instructions,
)
from services.api.app.agents.explorer.models import (
    ExplorerDecisionEnvelopeModel,
    ExplorerDecisionModel,
)
from services.api.app.agents.skills.loader import (
    AppWorkflowSkillConfigurationError,
    validate_app_workflow_skill_content,
)
from services.api.app.application.app_candidate_eligibility import (
    APP_EXPLORATION_OPERATIONS,
    app_candidate_rejection_reason,
    eligible_app_candidates,
)
from services.api.app.application.app_coverage_page_seek import (
    candidate_targets_detail_page,
    coverage_incomplete_allows_bottom_nav_through_overlay,
    overlay_budget_allows_real_coverage_seek,
    playbook_bottom_tabs_hard_incomplete,
    uncovered_required_page_rank,
)
from services.api.app.application.app_obstruction_controls import is_obstructed_surface
from services.api.app.application.app_semantic_progress import (
    SHORT_DRAMA_IN_TAB_TOP_LEFT_BEFORE_TAB_ADVANCE,
    SHORT_DRAMA_IN_TAB_TOP_MARKERS,
    SHORT_DRAMA_PLAYER_PAGE_TYPES,
    SHORT_DRAMA_TOP_LEFT_BACK_BEFORE_TAB_ADVANCE,
    SHORT_DRAMA_UNLOCK_POLL_MS,
    anwang_membership_landing_detected,
    anwang_unlock_cta_text,
    is_anwang_bottom_tab_label,
    membership_center_surface_detected,
    short_drama_bottom_nav_label_hits,
    short_drama_bottom_nav_visible_on_frame,
    short_drama_center_replay_control_detected,
    short_drama_end_seen,
    short_drama_exit_pending,
    short_drama_immersive_player_chrome,
    short_drama_in_tab_finished_for_advance,
    short_drama_playback_end_detected,
    short_drama_playback_observe_count,
    short_drama_playback_progress_near_end,
    short_drama_playback_started_at,
    short_drama_reentry_candidate_blocked,
    short_drama_should_tap_top_left_back,
    short_drama_top_left_back_arrow_visible,
    short_drama_top_left_back_attempt_count,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    attach_synthetic_touch_contracts,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.h5_exploration_observation import (
    evaluate_h5_navigation_candidate,
    h5_candidate_purpose,
)
from services.api.app.domain.exploration import (
    AppExplorationTerminalEvaluator,
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
    ExplorerWork,
    ProbeWork,
    SideEffectPolicy,
    build_action_fingerprint,
)
from services.api.app.domain.exploration.age_confirm import (
    is_age_confirm_candidate,
    observation_needs_age_confirm,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5TaskFrontierSnapshot,
    h5_frontier_completion_blockers_are_explainable,
)
from services.api.app.domain.inspection import EvidenceSideEffectPolicy
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
    build_timed_observation_id,
)
from services.api.app.domain.interaction.command_policy import (
    SERVER_MINIMUM_RISK_BY_OPERATION,
)
from services.api.app.domain.interaction.timed_observation import (
    TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION,
)
from services.api.app.domain.protocol import thaw_json_mapping

EXPLORER_AGENT_NAME = "ExplorerAgent"
_READ_ONLY_OPERATIONS = frozenset(
    {
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.HOVER,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.WARM_RESTORE,
        InteractionOperation.REFRESH,
        InteractionOperation.OBSERVE,
        InteractionOperation.TIMED_CAPTURE,
        InteractionOperation.SEEK_MEDIA,
    }
)
_APP_EXPLORATION_OPERATIONS = APP_EXPLORATION_OPERATIONS
_PROHIBITED_POLICY_OPERATIONS = frozenset(
    {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
)


class ExplorerAgentOutputError(RuntimeError):
    """模型输出无法安全映射到当前 Frontier。

    模型输出异常属非确定性瞬时故障：重试会携带新的 Frontier 快照重新提示，
    故标记 retryable，由单轮调度转 RETRY_WAIT 消化，不终结整场 Run。
    """

    retryable = True
    error_code = "H5_EXPLORER_OUTPUT_INVALID"


class ExplorerPrematureTerminalError(ExplorerAgentOutputError):
    """模型在仍有可执行候选时错误声明非完成终态。"""

    error_code = "H5_EXPLORER_PREMATURE_TERMINAL"


class ExplorerModelUnavailableError(RuntimeError):
    """模型调用未能取得响应，供受控平台候选决定是否安全降级。

    网关不可用属瞬时故障：无平台兜底候选时由单轮调度转 RETRY_WAIT 续跑，
    不终结整场 Run。
    """

    retryable = True
    error_code = "H5_EXPLORER_MODEL_UNAVAILABLE"


# SDK 层请求超时（ALIOTH_LLM_TIMEOUT_SECONDS，默认 60s）是第一道防线；此兜底
# 仅在网关半开连接（对端已 FIN、SDK 超时未触发）时防止协程无限挂起，须显著
# 大于正常单轮模型耗时。真实事故：连续两轮任务的重试 Run 挂死 20+ 分钟。
_EXPLORER_MODEL_CALL_TIMEOUT_SECONDS = 180.0


class WorkflowSkillLoader(Protocol):
    """Explorer 在模型调用前所需的确定性 Skill 加载端口。"""

    async def load_skill(self, skill_name: str) -> str: ...


class MafExplorerAgent:
    """让 MAF Agent 选择候选，并由确定性代码生成完整 ActionIntent。"""

    def __init__(
        self,
        *,
        client: SupportsChatGetResponse | None = None,
        agent: Agent | None = None,
        context_providers: Sequence[ContextProvider] | None = None,
        required_workflow_skill: str | None = None,
        required_workflow_skills: Sequence[str] | None = None,
        system_instructions_builder: Callable[..., str] = build_explorer_system_instructions,
        workflow_skill_loader: WorkflowSkillLoader | None = None,
        app_terminal_evaluator: AppExplorationTerminalEvaluator | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (client is None) == (agent is None):
            raise ValueError("client 与 agent 必须且只能提供一个")
        providers = tuple(context_providers or ())
        self._now = now or (lambda: datetime.now(UTC))
        self._app_terminal_evaluator = (
            app_terminal_evaluator
            if app_terminal_evaluator is not None
            else AppExplorationTerminalEvaluator(now=self._now)
        )
        skills: list[str] = []
        if required_workflow_skills:
            skills.extend(
                str(item).strip() for item in required_workflow_skills if str(item).strip()
            )
        elif required_workflow_skill:
            skills.append(required_workflow_skill)
        self._required_workflow_skills = tuple(skills)
        # 兼容旧字段：主 skill 仍取列表首项（instructions 门禁文案）
        self._required_workflow_skill = skills[0] if skills else None
        self._workflow_skill_loader = workflow_skill_loader or _find_workflow_skill_loader(
            providers
        )
        self._loaded_workflow_skill: str | None = None
        self._workflow_skill_lock = asyncio.Lock()
        self._agent = agent or _build_framework_agent(
            client,
            context_providers=providers,
            required_workflow_skill=self._required_workflow_skill,
            system_instructions_builder=system_instructions_builder,
        )

    @property
    def framework_agent(self) -> Agent:
        """暴露只读框架实例，供启动检查和架构测试确认真实 MAF 接线。"""
        return self._agent

    async def decide_deterministic(
        self,
        work: ExplorerWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal | None:
        """在全局 Frontier 调度前选择当前截图中的安全缺页入口。"""

        if not isinstance(work, ExplorationWork):
            return None
        if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
            return None
        from services.api.app.application.app_three_contract_driver import (
            require_app_state_machine_decision,
        )

        app_decision = require_app_state_machine_decision(work, now=self._now())
        if app_decision is not None:
            return app_decision

        from services.api.app.application.app_bounded_dismiss_tool import (
            try_bounded_dismiss_tool,
        )

        # 无专属编排：①限步②无状态③约束输出清障，先于覆盖寻页。
        bounded = try_bounded_dismiss_tool(work)
        if isinstance(bounded, ActionIntent):
            return bounded
        from services.api.app.application.app_shortflow_driver import (
            try_app_shortflow_driver,
        )

        shortflow = try_app_shortflow_driver(work)
        if isinstance(shortflow, ActionIntent):
            return shortflow
        from services.api.app.application.app_tangguo_flow_driver import (
            try_agent_app_flow_driver,
        )

        agent_flow = try_agent_app_flow_driver(work)
        if agent_flow is not None:
            return agent_flow
        eligible, _ = _eligible_candidates(work)
        from services.api.app.application.app_product_capability_for_explorer import (
            rank_eligible_by_capability_facts,
        )

        eligible = list(
            rank_eligible_by_capability_facts(work=work, eligible=eligible)
        )
        return _deterministic_app_coverage_decision(
            work=work,
            eligible=eligible,
            now=self._now(),
        )

    async def decide_next(
        self,
        work: ExplorerWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal:
        """在确定性预算和边界门禁内选择下一条候选。"""
        if isinstance(work, ProbeWork):
            return _decide_probe(work, now=self._now())
        if not isinstance(work, ExplorationWork):
            raise TypeError("work 必须为 ExplorationWork 或 ProbeWork")

        from services.api.app.application.app_three_contract_driver import (
            require_app_state_machine_decision,
        )

        app_decision = require_app_state_machine_decision(work, now=self._now())
        if app_decision is not None:
            return app_decision

        from services.api.app.application.app_bounded_dismiss_tool import (
            try_bounded_dismiss_tool,
        )

        # 无专属编排：限步清障先于模型／动态 Frontier，避免弹层烧步。
        bounded = try_bounded_dismiss_tool(work)
        if isinstance(bounded, ActionIntent):
            return bounded
        from services.api.app.application.app_shortflow_driver import (
            try_app_shortflow_driver,
        )

        shortflow = try_app_shortflow_driver(work)
        if isinstance(shortflow, ActionIntent):
            return shortflow
        from services.api.app.application.app_tangguo_flow_driver import (
            try_agent_app_flow_driver,
        )

        agent_flow = try_agent_app_flow_driver(work)
        if agent_flow is not None:
            return agent_flow

        is_app = work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}
        if not is_app:
            terminal = _preflight_terminal(work, now=self._now())
            if terminal is not None:
                return terminal
        eligible, rejection_reasons = _eligible_candidates(work)
        app_coverage_summary: Mapping[str, object] = {}
        if is_app:
            proposed_terminal = (
                ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=(
                        ExplorationTerminalStatus.NO_PROGRESS
                        if not work.frontier.candidates
                        else ExplorationTerminalStatus.BOUNDED
                    ),
                    reason=_bounded_reason(rejection_reasons),
                )
                if not eligible
                else None
            )
            evaluation = self._app_terminal_evaluator.evaluate(
                work,
                eligible_candidate_count=len(eligible),
                proposed_terminal=proposed_terminal,
            )
            if evaluation.terminal is not None:
                return evaluation.terminal
            app_coverage_summary = evaluation.coverage.to_dict()
        if not eligible:
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=(
                    ExplorationTerminalStatus.NO_PROGRESS
                    if not work.frontier.candidates
                    else ExplorationTerminalStatus.BOUNDED
                ),
                reason=_bounded_reason(rejection_reasons),
                coverage_summary=app_coverage_summary,
            )

        if is_app:
            # 显式课纲兼容旧底栏门禁；动态 Frontier 必须把当前真实候选交给
            # 有界调度／模型，不能按产品底栏进度裁剪候选池。
            if not _uses_dynamic_app_frontier(work):
                eligible = list(
                    _filter_eligible_while_bottom_tabs_hard_incomplete(
                        work=work,
                        eligible=eligible,
                    )
                )
            from services.api.app.application.app_product_capability_for_explorer import (
                rank_eligible_by_capability_facts,
            )

            eligible = list(
                rank_eligible_by_capability_facts(work=work, eligible=eligible)
            )
            deterministic = _deterministic_app_coverage_decision(
                work=work,
                eligible=eligible,
                now=self._now(),
            )
            if deterministic is not None:
                return deterministic

        payload = _agent_input(
            work=work,
            candidates=eligible,
            app_coverage_summary=app_coverage_summary,
        )
        workflow_skill = await self._ensure_required_workflow_skill()
        options: dict[str, Any] = {"response_format": ExplorerDecisionEnvelopeModel}
        instruction_parts: list[str] = []
        if workflow_skill is not None:
            instruction_parts.append(
                "以下为已由运行时确定性加载并校验的 Workflow／产品能力 Skill，必须遵循：\n"
                + workflow_skill
            )
        capability = payload.get("app_product_capability_facts")
        if isinstance(capability, dict) and capability:
            instruction_parts.append(
                "以下为只读 Product 能力事实（禁止当作坐标执行授权；"
                "不得覆盖当前帧 candidate_id；无 Profile 则通用探索）：\n"
                + json.dumps(capability, ensure_ascii=False, default=str)
            )
        curriculum = payload.get("script_curriculum")
        if isinstance(curriculum, dict) and curriculum:
            hint = curriculum.get("current_phase_hint") or {}
            instruction_parts.append(
                "以下为本轮 App 脚本课纲（必须按 phases 推进多帧；purpose 写明 phase_id）：\n"
                + json.dumps(
                    {
                        "playbook_id": curriculum.get("playbook_id"),
                        "script_ref": curriculum.get("script_ref"),
                        "learning_rule": curriculum.get("learning_rule"),
                        "current_phase_hint": hint,
                        "forbid_hints": curriculum.get("forbid_hints"),
                        "tab_swipe_plan": curriculum.get("tab_swipe_plan"),
                        "phases": curriculum.get("phases"),
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        if instruction_parts:
            options["instructions"] = "\n\n".join(instruction_parts)
        response: Any | None = None
        try:
            async with asyncio.timeout(_EXPLORER_MODEL_CALL_TIMEOUT_SECONDS):
                response = await self._agent.run(
                    Message(
                        "user",
                        [Content.from_text(json.dumps(payload, ensure_ascii=False, default=str))],
                    ),
                    options=options,
                )
            raw = response.value
            decision = _strict_explorer_decision(raw)
        except Exception as exc:  # noqa: BLE001 - App 侧 LLM 失败时只降级到有界安全候选
            if work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
                return _decide_app_fallback(work=work, eligible=eligible, error=exc)
            if isinstance(exc, (ValidationError, ValueError, TypeError)):
                diagnostic = _safe_explorer_response_diagnostic(
                    response=response,
                    error=exc,
                )
                raise ExplorerAgentOutputError(
                    f"Explorer 未返回合法结构化候选选择: {diagnostic}"
                ) from exc
            raise ExplorerModelUnavailableError(
                f"Explorer 模型服务不可用: error_type={type(exc).__name__}"
            ) from exc

        if decision.kind == "terminal":
            if is_app:
                proposed = _model_terminal_intent(work=work, decision=decision)
                evaluation = self._app_terminal_evaluator.evaluate(
                    work,
                    eligible_candidate_count=len(eligible),
                    proposed_terminal=proposed,
                )
                if evaluation.terminal is not None:
                    return evaluation.terminal
                # App：模型终态只是输入；未满足领域条件时只允许安全恢复动作。
                return _decide_app_fallback(
                    work=work,
                    eligible=eligible,
                    error=ExplorerAgentOutputError("模型提前返回终态"),
                )
            return _validated_model_terminal(work=work, decision=decision, eligible=eligible)
        candidate_by_id = {item.candidate_id: item for item in eligible}
        candidate = candidate_by_id.get(decision.candidate_id or "")
        if candidate is None:
            if work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
                return _decide_app_fallback(
                    work=work,
                    eligible=eligible,
                    error=ExplorerAgentOutputError("Explorer 选择了无效 candidate_id"),
                )
            raise ExplorerAgentOutputError("Explorer 选择了不属于可执行 Frontier 的 candidate_id")
        if work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
            # 模型若仍选中详情／付费入口：底栏未完时强制改走安全候选／BACK。
            # 动态 Frontier 同样禁止用内容卡拖死 AI／列表页。
            if (
                playbook_bottom_tabs_hard_incomplete(
                    coverage_metadata=work.frontier.observation.metadata,
                )
                and not _candidate_allowed_while_bottom_tabs_hard_incomplete(
                    work=work,
                    candidate=candidate,
                )
            ):
                # 下一栏可见时交由下方强制改写；否则走 fallback 安全池。
                visible_bottom_probe = [
                    item
                    for item in eligible
                    if item.metadata.get("bottom_navigation_tab_seek") is True
                    and _bottom_tab_bounds_in_safe_band(item, relax=True)
                ]
                if (
                    _pick_next_playbook_bottom_tab(
                        work=work,
                        visible_bottom=visible_bottom_probe,
                    )
                    is None
                ):
                    return _decide_app_fallback(
                        work=work,
                        eligible=eligible,
                        error=ExplorerAgentOutputError(
                            "底栏硬覆盖未完，禁止执行详情／付费下钻候选"
                        ),
                    )
            # 底栏未完：强制课纲下一栏；动态 Frontier 也不得放行内容卡／详情下钻。
            # 弹窗未满足时禁止改写成／执行底栏（含假 completed）。
            if playbook_bottom_tabs_hard_incomplete(
                coverage_metadata=work.frontier.observation.metadata,
            ):
                from services.api.app.application.app_exploration_flow_contract import (
                    popup_phase_blocks_bottom_tabs,
                )

                if popup_phase_blocks_bottom_tabs(
                    work.frontier.observation.metadata
                ):
                    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
                        return _decide_app_fallback(
                            work=work,
                            eligible=eligible,
                            error=ExplorerAgentOutputError(
                                "弹窗流程未满足，禁止执行底栏候选"
                            ),
                        )
                else:
                    visible_bottom = [
                        item
                        for item in eligible
                        if item.metadata.get("bottom_navigation_tab_seek") is True
                        and _bottom_tab_bounds_in_safe_band(item, relax=True)
                    ]
                    next_tab = _pick_next_playbook_bottom_tab(
                        work=work,
                        visible_bottom=visible_bottom,
                    )
                    if next_tab is None and visible_bottom:
                        next_tab = _fallback_visible_bottom_tab_while_yielding(
                            work=work,
                            visible_bottom=visible_bottom,
                        )
                    allowed_exception = (
                        _is_dynamic_age_confirm_clear(work=work, candidate=candidate)
                        or _is_dynamic_profile_credential_save(
                            work=work, candidate=candidate
                        )
                        or _is_dynamic_profile_credential_dismiss(
                            work=work, candidate=candidate
                        )
                        or (
                            _is_dismiss_like_candidate(candidate)
                            and not _should_prefer_bottom_tabs_over_dismiss(
                                work=work,
                                tabs_hard_incomplete=True,
                                visible_bottom=visible_bottom,
                            )
                        )
                    )
                    if next_tab is not None and not allowed_exception:
                        # AI／暗网等「截图后立刻下一栏」：无论模型点了内容卡或 OBSERVE，都切下一栏。
                        candidate = next_tab
                    elif (
                        next_tab is None
                        and not allowed_exception
                        and not _candidate_allowed_while_bottom_tabs_hard_incomplete(
                            work=work,
                            candidate=candidate,
                        )
                    ):
                        return _decide_app_fallback(
                            work=work,
                            eligible=eligible,
                            error=ExplorerAgentOutputError(
                                "底栏硬覆盖未完，禁止执行内容卡／详情下钻"
                            ),
                        )
            if _uses_dynamic_app_frontier(work):
                return _build_action_intent(
                    work=work,
                    candidate=candidate,
                    purpose=decision.purpose or "按动态 App Frontier 探索当前安全候选。",
                )
            # Playbook 只为模型提供提示；执行必须保留模型从当前截图候选中作出的选择。
            return _decide_app_curriculum_action(
                work=work,
                eligible=eligible,
                reason="按脚本课纲 prefer_hints 执行（对齐 untitled 多帧）",
                llm_candidate=candidate,
                llm_purpose=decision.purpose,
            )
        return _build_action_intent(
            work=work,
            candidate=candidate,
            purpose=(
                h5_candidate_purpose(candidate)
                if H5_TASK_FRONTIER_METADATA_KEY in work.frontier.observation.metadata
                else decision.purpose or "执行当前 Frontier 候选。"
            ),
        )

    async def _ensure_required_workflow_skill(self) -> str | None:
        """在首次模型调用前加载并校验完整 Workflow／课纲 Skill 正文。"""
        skill_names = self._required_workflow_skills
        if not skill_names:
            return None
        if self._loaded_workflow_skill is not None:
            return self._loaded_workflow_skill
        async with self._workflow_skill_lock:
            if self._loaded_workflow_skill is not None:
                return self._loaded_workflow_skill
            loader = self._workflow_skill_loader
            if loader is None:
                raise AppWorkflowSkillConfigurationError(
                    "Explorer 缺少 Skill Provider load_skill 门禁"
                )
            chunks: list[str] = []
            for skill_name in skill_names:
                try:
                    content = await loader.load_skill(skill_name)
                except AppWorkflowSkillConfigurationError:
                    raise
                except Exception as exc:
                    raise AppWorkflowSkillConfigurationError(
                        f"Explorer {skill_name} Skill 加载失败"
                    ) from exc
                if skill_name == "app-workflow":
                    validate_app_workflow_skill_content(content)
                elif skill_name == "app-product-capability":
                    if not isinstance(content, str) or not content.strip():
                        raise AppWorkflowSkillConfigurationError(
                            "Explorer app-product-capability Skill 正文为空"
                        )
                    required = (
                        "只读",
                        "禁止",
                        "grants_absolute_coordinates",
                        "candidate",
                    )
                    missing = [marker for marker in required if marker not in content]
                    if missing:
                        raise AppWorkflowSkillConfigurationError(
                            "app-product-capability Skill 缺少强制边界："
                            + ", ".join(missing)
                        )
                elif not isinstance(content, str) or not content.strip():
                    raise AppWorkflowSkillConfigurationError(
                        f"Explorer 加载的 {skill_name} Skill 正文为空"
                    )
                chunks.append(f"## Skill: {skill_name}\n\n{content.strip()}")
            self._loaded_workflow_skill = "\n\n".join(chunks)
            return self._loaded_workflow_skill


def _strict_explorer_decision(raw: object) -> ExplorerDecisionModel:
    """规范 Framework 信封中的冗余变体字段，再执行严格领域校验。"""
    if isinstance(raw, ExplorerDecisionModel):
        return raw
    if isinstance(raw, ExplorerDecisionEnvelopeModel):
        envelope = raw
    elif isinstance(raw, str):
        envelope = ExplorerDecisionEnvelopeModel.model_validate_json(raw)
    else:
        envelope = ExplorerDecisionEnvelopeModel.model_validate(raw)

    payload = envelope.model_dump()
    if envelope.kind == "action":
        # 部分兼容端会为 action 同时生成解释性 reason；执行仍只信任当前 Frontier。
        payload["terminal_status"] = None
        payload["reason"] = None
    else:
        # terminal 分支不允许携带可执行候选，避免绕过确定性终态验证。
        payload["candidate_id"] = None
        payload["purpose"] = None
    return ExplorerDecisionModel.model_validate(payload)


def _safe_explorer_response_diagnostic(
    *,
    response: object | None,
    error: Exception,
) -> str:
    """只记录模型响应形状和校验位置，不写入原始响应值。"""
    parts = [
        f"error_type={type(error).__name__}",
        f"response_type={type(response).__name__ if response is not None else 'none'}",
    ]
    if isinstance(error, ValidationError):
        issues = []
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[:8]:
            location = ".".join(str(value) for value in item.get("loc", ())) or "$"
            issues.append(f"{location}:{item.get('type', 'unknown')}")
        if issues:
            parts.append("validation_issues=" + ",".join(issues))

    text = getattr(response, "text", None)
    if not isinstance(text, str):
        return ";".join(parts)
    normalized = text.strip()
    parts.extend(
        (
            f"text_length={len(text)}",
            f"code_fence={normalized.startswith(chr(96) * 3)}",
        )
    )
    try:
        decoded = json.loads(normalized)
    except json.JSONDecodeError as exc:
        parts.append(f"json_error_position={exc.pos}")
        return ";".join(parts)
    parts.append(f"json_type={type(decoded).__name__}")
    if isinstance(decoded, Mapping):
        keys = sorted(str(key) for key in decoded)
        non_null_keys = sorted(str(key) for key, value in decoded.items() if value is not None)
        parts.append("json_keys=" + ",".join(keys[:20]))
        parts.append("json_non_null_keys=" + ",".join(non_null_keys[:20]))
        parts.append(f"json_key_count={len(keys)}")
    return ";".join(parts)


def _find_workflow_skill_loader(
    providers: Sequence[ContextProvider],
) -> WorkflowSkillLoader | None:
    for provider in providers:
        if callable(getattr(provider, "load_skill", None)):
            return cast(WorkflowSkillLoader, provider)
    return None


def _build_framework_agent(
    client: SupportsChatGetResponse | None,
    *,
    context_providers: Sequence[ContextProvider] | None = None,
    required_workflow_skill: str | None = None,
    system_instructions_builder: Callable[..., str] = build_explorer_system_instructions,
) -> Agent:
    if client is None:
        raise ValueError("client 不能为空")
    default_options = {"store": False} if getattr(client, "STORES_BY_DEFAULT", False) else None
    return Agent(
        client=client,
        instructions=system_instructions_builder(
            output_schema=ExplorerDecisionModel.model_json_schema(),
            required_workflow_skill=required_workflow_skill,
        ),
        name=EXPLORER_AGENT_NAME,
        tools=None,
        context_providers=list(context_providers or ()),
        default_options=default_options,
    )


def _decide_app_fallback(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    error: Exception,
) -> ActionIntent | ExplorationTerminal:
    """App 模型失败时按运行模式选择有界、可审计的安全降级。"""
    import logging

    logger = logging.getLogger(__name__)
    if _uses_dynamic_app_frontier(work):
        safe_candidates = tuple(
            item
            for item in eligible
            if item.action_type
            in {
                InteractionOperation.OBSERVE,
                InteractionOperation.SCROLL,
                InteractionOperation.BACK,
                InteractionOperation.WARM_RESTORE,
                InteractionOperation.TIMED_CAPTURE,
            }
            or (
                item.action_type
                in {InteractionOperation.CLICK, InteractionOperation.TAP}
                and (
                    item.risk_level is InteractionRiskLevel.LOW
                    or _popup_close_candidate_for_obstruction(
                        work=work,
                        eligible=(item,),
                    )
                    is item
                    or _is_dynamic_age_confirm_clear(work=work, candidate=item)
                )
            )
        )
        if not safe_candidates:
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason=(
                    f"Explorer 模型不可用（{type(error).__name__}），"
                    "当前动态 Frontier 没有低风险或可逆候选。"
                ),
            )
        chosen = max(safe_candidates, key=lambda item: _dynamic_app_candidate_rank(work, item))
        # 上方课纲未完：LLM 失败不得旁路切短剧／暗网／AI。
        home_content_now = _intent_for_home_content_entry(
            work=work,
            eligible=eligible,
        )
        if home_content_now is not None:
            return home_content_now
        if _home_top_curriculum_still_open(work):
            sample_now = _intent_for_home_top_category_sample(
                work=work,
                eligible=eligible,
            )
            if sample_now is not None:
                return sample_now
            home_top = _intent_for_home_top_category_tabs(
                work=work,
                eligible=eligible,
            )
            if home_top is not None:
                return home_top
            safe_candidates = tuple(
                item
                for item in safe_candidates
                if item.metadata.get("bottom_navigation_tab_seek") is not True
            )
            if safe_candidates:
                chosen = max(
                    safe_candidates,
                    key=lambda item: _dynamic_app_candidate_rank(work, item),
                )
        # 底栏未完：强制课纲下一栏（含模型失败后误选内容卡／OBSERVE 的情况）。
        elif playbook_bottom_tabs_hard_incomplete(
            coverage_metadata=work.frontier.observation.metadata,
        ):
            visible_bottom = [
                item
                for item in eligible
                if item.metadata.get("bottom_navigation_tab_seek") is True
                and _bottom_tab_bounds_in_safe_band(item, relax=True)
            ]
            next_tab = _pick_next_playbook_bottom_tab(
                work=work,
                visible_bottom=visible_bottom,
            )
            if next_tab is None and visible_bottom:
                next_tab = _fallback_visible_bottom_tab_while_yielding(
                    work=work,
                    visible_bottom=visible_bottom,
                )
            if next_tab is not None:
                chosen = next_tab
        logger.warning(
            "Explorer LLM 失败，改用动态 App Frontier 安全候选 candidate_id=%s err=%s: %s",
            chosen.candidate_id,
            type(error).__name__,
            error,
        )
        return _build_action_intent(
            work=work,
            candidate=chosen,
            purpose=(
                f"LLM 失败（{type(error).__name__}），按动态 Frontier 执行最高优先级"
                f"安全候选「{chosen.semantic_target}」。"
            ),
        )
    safe_candidates = tuple(
        item
        for item in eligible
        if item.action_type
        in {
            InteractionOperation.OBSERVE,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
            InteractionOperation.WARM_RESTORE,
        }
        or _is_current_curriculum_low_risk_click(work=work, candidate=item)
        or _is_trusted_popup_close_recovery(work=work, candidate=item)
        or _is_trusted_content_detail_list_restore(work=work, candidate=item)
    )
    if not safe_candidates:
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BOUNDED,
            reason=(
                f"Explorer 模型不可用（{type(error).__name__}），"
                "当前截图没有可安全恢复的 OBSERVE、SCROLL 或 BACK 候选。"
            ),
        )
    intent = _decide_app_curriculum_action(
        work=work,
        eligible=safe_candidates,
        reason=f"LLM 失败（{type(error).__name__}），按脚本课纲／Playbook 继续多帧",
    )
    logger.warning(
        "Explorer LLM 失败，改用 App Playbook 兜底 candidate_id=%s err=%s: %s",
        intent.parameters.get("candidate_id"),
        type(error).__name__,
        error,
    )
    return intent


def _uses_dynamic_app_frontier(work: ExplorationWork) -> bool:
    policy = work.frontier.observation.metadata.get("app_runtime_policy")
    return isinstance(policy, Mapping) and policy.get("exploration_mode") == "dynamic_frontier"


def _synthetic_system_back_candidate(*, work: ExplorationWork) -> CandidateAction:
    """当前帧无 BACK 候选时注入系统返回，用于详情／付费遮罩强制退出。"""

    purpose = "当前落在无根底栏的详情／付费遮罩；系统 BACK 返回后再巡检底栏。"
    capture_id = str(work.source_capture_id or work.frontier.observation.capture_id or "unknown")
    return CandidateAction(
        candidate_id=f"forced-system-back:{capture_id}",
        semantic_target="系统 BACK",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "back",
            "forced_detail_exit_back": True,
            "post_detail_exit_prefer_tabs": True,
        },
    )


def _with_post_detail_exit_prefer_tabs(candidate: CandidateAction) -> CandidateAction:
    """详情强制退出后，给下一帧打上「优先切栏」标记。"""

    return CandidateAction(
        candidate_id=candidate.candidate_id,
        semantic_target=candidate.semantic_target,
        action_type=candidate.action_type,
        risk_level=candidate.risk_level,
        exploration_value=candidate.exploration_value,
        navigation_depth=candidate.navigation_depth,
        expected_surface_kind=candidate.expected_surface_kind,
        prior_failure_count=candidate.prior_failure_count,
        metadata={
            **dict(candidate.metadata),
            "forced_detail_exit_back": True,
            "post_detail_exit_prefer_tabs": True,
        },
        protocol_version=candidate.protocol_version,
    )


def _observe_wait_for_next_playbook_tab(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> ActionIntent | None:
    """课纲下一栏本帧未见：观察等稳，禁止插值猜点。"""

    observe = next(
        (
            candidate
            for candidate in eligible
            if candidate.action_type
            in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        ),
        None,
    )
    if observe is None:
        return None
    return _build_action_intent(
        work=work,
        candidate=observe,
        purpose=(
            "课纲下一栏本帧未见真实底栏标签；观察等待 Vision／a11y 条带稳定后再切栏，"
            "禁止插值猜点。"
        ),
    )


def synthetic_popup_clearance_observe_intent(
    *,
    work: ExplorationWork,
    purpose: str | None = None,
) -> ActionIntent:
    """弹窗流程未满足且无关层／确定性动作时：合成零触控重采，禁止 Deferred 空转。"""

    from services.api.app.application.app_synthetic_touch_contracts import (
        popup_clearance_observe_contract,
    )

    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    effective_purpose = purpose or (
        "弹窗流程未满足且本帧无可靠关闭点；观察重采以续发关层候选，"
        "禁止 Frontier 排空收口。"
    )
    contract = popup_clearance_observe_contract(purpose=effective_purpose)
    candidate = CandidateAction(
        candidate_id=f"forced-popup-clearance-observe:{capture_id}",
        semantic_target="观察等待弹层关闭控件",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=40,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": effective_purpose,
            "frontier_scope": "local",
            "forced_popup_clearance_observe": True,
            "popup_clearance_observe_contract": dict(contract),
            "expected_state_hint": "popup_clearance_reobserve",
        },
    )
    return _build_action_intent(work=work, candidate=candidate, purpose=effective_purpose)


def synthetic_no_close_promo_scrim_dismiss_intent(
    *,
    work: ExplorationWork,
) -> ActionIntent:
    """无 X 终身卡／抢购层：点蒙层空白处关闭，禁止点抢购／假 X／只观察空等。"""

    from services.api.app.application.app_synthetic_touch_contracts import (
        ALT_NO_CLOSE_PROMO_SCRIM_BOUNDS,
        DEFAULT_NO_CLOSE_PROMO_SCRIM_BOUNDS,
        popup_scrim_dismiss_contract,
    )

    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    purpose = (
        "当前为无关闭 X 的促销／终身卡层；点击蒙层空白处关闭，"
        "禁止点立即抢购／底飘假 X；关闭见主页后再点上方分类。"
    )
    metadata = (
        work.frontier.observation.metadata
        if isinstance(work.frontier.observation.metadata, Mapping)
        else {}
    )
    # 首点卡片下方空白；连续无效后改点左上蒙层，避免总落在卡片图上。
    attempt = metadata.get("surface_phase_same_popup_attempt_count")
    noop = metadata.get("dismiss_noop_streak")
    use_alt = (
        (isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1)
        or (isinstance(noop, int) and not isinstance(noop, bool) and noop >= 1)
    )
    bounds = ALT_NO_CLOSE_PROMO_SCRIM_BOUNDS if use_alt else DEFAULT_NO_CLOSE_PROMO_SCRIM_BOUNDS
    contract = popup_scrim_dismiss_contract(bounds=bounds, purpose=purpose)
    candidate = CandidateAction(
        candidate_id=f"forced-popup-scrim-dismiss:{capture_id}",
        semantic_target="点击弹层蒙层空白处关闭",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "control_role": "dismiss",
            "expected_state_hint": "dismiss_popup_scrim",
            "normalized_bounds": bounds.to_dict(),
            "forced_popup_scrim_dismiss": True,
            "popup_scrim_dismiss_contract": dict(contract),
            "require_screenshot_evidence": True,
        },
    )
    return _build_action_intent(work=work, candidate=candidate, purpose=purpose)


def _home_detail_oscillation_yields_to_tabs(work: ExplorationWork) -> bool:
    """主页与详情来回点穿后，根页改切底栏，禁止再点假关层或已选上方分类。"""

    observation = work.frontier.observation
    page_type = str(observation.page_type or "").casefold()
    if page_type not in {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
    }:
        return False
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    bounce_raw = metadata.get("home_detail_bounce_count")
    bounce = (
        int(bounce_raw)
        if isinstance(bounce_raw, int) and not isinstance(bounce_raw, bool)
        else 0
    )
    # 详情 BACK 已打标，或页型来回至少一轮：下一帧必须切栏。
    # 上方课纲未完时仍须 True，才能跳过假关层去点国产／动漫；
    # 切短剧由 _home_top_category_blocks_bottom_tabs 挡住，不在这里关。
    return metadata.get("post_detail_exit_prefer_tabs") is True or bounce >= 2


def _should_prefer_bottom_tabs_over_dismiss(
    *,
    work: ExplorationWork,
    tabs_hard_incomplete: bool,
    visible_bottom: Sequence[CandidateAction],
) -> bool:
    """底栏未完且根页可见底栏时，切栏优先于假关层；真弹窗有安全关闭时仍先关。"""

    if not tabs_hard_incomplete or not visible_bottom:
        return False
    if _home_top_curriculum_still_open(work):
        return False
    observation = work.frontier.observation
    page_type = str(observation.page_type or "").casefold()
    if page_type not in {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
    }:
        return False
    # 主页↔详情空转：即使 Vision 仍标 popup，也交还底栏。
    if _home_detail_oscillation_yields_to_tabs(work):
        return True
    # 录屏验收：首屏跳过→弹窗逐个截图关底栏 X→再进主页切栏；真挡路弹窗不得被切栏抢先。
    if _blocking_popup_requires_dismiss(work):
        return False
    return True


def _is_dismiss_like_candidate(candidate: CandidateAction) -> bool:
    """候选是否像关层／关闭广告（含假 X）。"""

    role = str(candidate.metadata.get("control_role") or "").casefold()
    if role in {"dismiss", "skip"}:
        return True
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("semantic_name") or ""),
        )
    ).casefold()
    return any(
        marker in terms
        for marker in (
            "关闭",
            "關閉",
            "跳过",
            "跳過",
            "close",
            "dismiss",
            "skip",
            "close_popup",
            "popup_x",
        )
    )


def _candidate_allowed_while_bottom_tabs_hard_incomplete(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """底栏硬覆盖未完时仅允许关层／切栏／可逆动作，禁止内容卡下钻。"""

    tab_pool = tuple(
        item
        for item in work.frontier.candidates
        if item.metadata.get("bottom_navigation_tab_seek") is True
        and _bottom_tab_bounds_in_safe_band(item, relax=True)
    )
    # 底栏未完：课纲下一栏已可见或同帧像素可合成时禁止 OBSERVE；否则允许观察等稳。
    if candidate.action_type is InteractionOperation.OBSERVE:
        next_tab = _pick_next_playbook_bottom_tab(work=work, visible_bottom=tab_pool)
        if next_tab is not None:
            return False
    if candidate.action_type in {
        InteractionOperation.OBSERVE,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.WARM_RESTORE,
        InteractionOperation.TIMED_CAPTURE,
    }:
        return True
    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        # 挡路弹窗未清：底栏可见也不切（网格广告下仍能看见短剧标签）。
        # 清障让路预算已满时放行，避免关层空转占满业务时间。
        if (
            _blocking_popup_requires_dismiss(work)
            and not _home_popup_budget_allows_coverage_seek(work)
        ):
            return False
        return True
    # 主页上方分类课纲：底栏未完时仍须先巡最新／推荐…，禁止被「只许切栏」裁掉。
    if candidate.metadata.get("home_top_category_tab_seek") is True:
        if (
            _blocking_popup_requires_dismiss(work)
            and not _home_popup_budget_allows_coverage_seek(work)
        ):
            return False
        page_type = str(work.frontier.observation.page_type or "").casefold()
        return page_type == "home"
    if candidate.metadata.get("package_boundary_recovery") is True:
        return True
    if candidate.metadata.get("coverage_parent_restore") is True:
        return True
    if candidate.metadata.get("forced_detail_exit_back") is True:
        return True
    if _is_dynamic_age_confirm_clear(work=work, candidate=candidate):
        return True
    if _is_dynamic_profile_credential_save(work=work, candidate=candidate):
        return True
    if _is_dynamic_profile_credential_dismiss(work=work, candidate=candidate):
        return True
    # 受信恢复候选已经由包名、页面事实和课纲阶段共同校验，不能再被通用
    # 关闭按钮几何规则或“业务点击”兜底规则误删。
    if _is_trusted_popup_close_recovery(work=work, candidate=candidate):
        return True
    if _is_trusted_content_detail_list_restore(work=work, candidate=candidate):
        return True
    # 根页可见底栏时禁止假关层：关层点击常点穿进详情，再被强制 BACK 空转。
    if _is_dismiss_like_candidate(candidate):
        if _should_prefer_bottom_tabs_over_dismiss(
            work=work,
            tabs_hard_incomplete=True,
            visible_bottom=tab_pool,
        ):
            return False
        # 底栏未完：只放行顶角或底部广告条安全关闭。
        if not _dismiss_candidate_is_corner_safe_close(candidate):
            return False
        return True
    role = str(candidate.metadata.get("control_role") or "").casefold()
    if role in {"dismiss", "skip"}:
        return True
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    if any(
        marker in terms
        for marker in (
            "close_popup",
            "popup_x",
            "关闭",
            "跳过",
            "dismiss",
            "age_gate",
            "年龄",
        )
    ):
        return True
    # 显式详情／付费入口一律禁止。
    if candidate_targets_detail_page(candidate):
        return False
    if anwang_unlock_cta_text(
        " ".join(
            (
                str(candidate.semantic_target or ""),
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        )
    ):
        return False
    expected = str(candidate.metadata.get("expected_next_page_type") or "").casefold()
    if expected in {"content_detail", "video_detail", "payment", "post_detail"}:
        return False
    # 其余业务 CLICK（内容卡／宫格）在底栏未完前一律禁止。
    return candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }


def _filter_eligible_while_bottom_tabs_hard_incomplete(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> tuple[CandidateAction, ...]:
    """底栏硬覆盖未完时收紧可执行池，打断 home↔详情循环。"""

    if _uses_dynamic_app_frontier(work):
        return tuple(eligible)
    if not playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=work.frontier.observation.metadata,
    ):
        return tuple(eligible)
    filtered = tuple(
        item
        for item in eligible
        if _candidate_allowed_while_bottom_tabs_hard_incomplete(
            work=work,
            candidate=item,
        )
    )
    # 可逆动作已由上方谓词保留；空池不得凭空签发当前 Frontier 之外的动作。
    return filtered


def _deterministic_app_coverage_decision(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    now: datetime | None = None,
) -> ActionIntent | TimedObservationIntent | None:
    """当前帧的安全缺页入口优先于历史 sibling，避免离开后入口丢失。"""

    clock = now if isinstance(now, datetime) else datetime.now(UTC)
    package_recovery = next(
        (
            candidate
            for candidate in eligible
            if candidate.metadata.get("package_boundary_recovery") is True
            and candidate.action_type
            in {InteractionOperation.BACK, InteractionOperation.WARM_RESTORE}
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        ),
        None,
    )
    if package_recovery is not None:
        return _build_action_intent(
            work=work,
            candidate=package_recovery,
            purpose="当前截图已越出目标 App，确定性恢复目标包后继续探索。",
        )
    # 关层曾点进浏览器／外链：回包后优先系统 BACK，禁止再点同一关闭 X 外跳。
    # 不得把 cloud_back／底栏「<」当成 recovery（否则 purpose 含「浏览器」→ keyevent 4 退桌面）。
    popup_back_recovery = next(
        (
            candidate
            for candidate in eligible
            if candidate.metadata.get("popup_back_recovery") is True
            and candidate.metadata.get("cloud_back_popup_close") is not True
            and "底部导航栏" not in candidate.semantic_target
            and candidate.action_type is InteractionOperation.BACK
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        ),
        None,
    )
    if popup_back_recovery is not None:
        return _build_action_intent(
            work=work,
            candidate=popup_back_recovery,
            purpose=(
                "关层曾外跳浏览器／外链；确定性系统 BACK 关闭遮挡，"
                "禁止再点广告关闭 X 或落地页「点击进入」。"
            ),
        )
    # 年龄门优先于开屏／弹窗广告：APP JSON UI Tree「我已满18岁」。
    age_confirm = next(
        (
            candidate
            for candidate in eligible
            if _is_dynamic_age_confirm_clear(work=work, candidate=candidate)
        ),
        None,
    )
    if age_confirm is not None:
        return _build_action_intent(
            work=work,
            candidate=age_confirm,
            purpose=(
                "当前帧为年龄确认门禁；按 APP UI Tree 特征确定性点击「我已满18岁」"
                "进入后再处理开屏／弹窗广告与底栏。"
            ),
        )
    profile_credential_dismiss = next(
        (
            candidate
            for candidate in eligible
            if _is_dynamic_profile_credential_dismiss(work=work, candidate=candidate)
        ),
        None,
    )
    if profile_credential_dismiss is None and _work_shows_profile_credential(work):
        if _profile_credential_save_phase_done(work):
            profile_credential_dismiss = _synthetic_profile_credential_dismiss_candidate(
                work=work
            )
    # 误进「我的」凭证：课纲未到时先回主页，禁止保存凭证抢跑。
    if (
        _raw_profile_credential_markers_on_screen(work)
        and not _profile_credential_curriculum_ready(work)
    ):
        escape = _escape_accidental_profile_credential_intent(
            work=work,
            eligible=eligible,
        )
        if escape is not None:
            return escape
    if (
        profile_credential_dismiss is not None
        and _profile_credential_curriculum_ready(work)
    ):
        return _build_action_intent(
            work=work,
            candidate=profile_credential_dismiss,
            purpose=(
                "当前帧账号凭证已保存成功（或已点过保存凭证）；"
                "确定性点击「我已保存」关闭弹层。"
            ),
        )
    profile_credential = next(
        (
            candidate
            for candidate in eligible
            if _is_dynamic_profile_credential_save(work=work, candidate=candidate)
        ),
        None,
    )
    if (
        profile_credential is None
        and _work_shows_profile_credential(work)
        and not _profile_credential_save_phase_done(work)
        and _profile_credential_curriculum_ready(work)
    ):
        profile_credential = _synthetic_profile_credential_save_candidate(work=work)
    if profile_credential is not None and _profile_credential_curriculum_ready(work):
        return _build_action_intent(
            work=work,
            candidate=profile_credential,
            purpose=(
                "当前帧为个人中心账号凭证弹层；先点蓝色「保存凭证」，"
                "保存成功后再点「我已保存」。"
            ),
        )
    # HJLL 广告网格会保留主页栏目，通用流程会先签发上栏／广告入口而遗漏圆 X。
    # 专属编排器在账号凭证之后、任何栏目或广告动作之前，按当前截图的像素 X
    # 或 Airtest 的固定 y=0.80 点关闭；没有 popup_visible 则绝不触发。
    if _is_hjll_airtest_work(work):
        popup_facts = work.frontier.observation.metadata.get("surface_facts")
        if (
            isinstance(popup_facts, Mapping)
            and popup_facts.get("popup_visible") is True
            and _hjll_popup_attempt_limit_reached(work)
        ):
            # 像素重绑与固定点共用上限，不能在较早的优先分支旁路预算。
            return None
        hjll_close = _synthetic_pixel_popup_close_candidate(work=work)
        if hjll_close is None:
            hjll_close = _synthetic_hjll_airtest_popup_close_candidate(work=work)
        if hjll_close is not None and (
            InteractionOperation.CLICK in work.boundaries.allowed_operations
            or InteractionOperation.TAP in work.boundaries.allowed_operations
        ):
            return _build_action_intent(
                work=work,
                candidate=hjll_close,
                purpose=(
                    "HJLL 已见遮挡弹窗；优先按圆 X 关闭后才执行上栏、"
                    "广告或底栏编排。"
                ),
            )
    ad_landing_capture = next(
        (
            candidate
            for candidate in eligible
            if candidate.metadata.get("app_ad_landing_consistency") is True
            and candidate.action_type
            in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        ),
        None,
    )
    metadata = work.frontier.observation.metadata
    raw_attempts = metadata.get("app_ad_landing_attempt_count")
    ad_landing_already_attempted = (
        isinstance(raw_attempts, int)
        and not isinstance(raw_attempts, bool)
        and raw_attempts > 0
    )
    # 关层已外跳过：禁止再为 alioth_17 主动点广告本体进浏览器。
    popup_external_redirects = metadata.get("dynamic_popup_external_redirect_count")
    popup_already_redirected = (
        isinstance(popup_external_redirects, int)
        and not isinstance(popup_external_redirects, bool)
        and popup_external_redirects > 0
    )
    if (
        ad_landing_capture is not None
        and not ad_landing_already_attempted
        and not popup_already_redirected
        and not coverage_incomplete_allows_bottom_nav_through_overlay(
            coverage_metadata=metadata,
        )
    ):
        return _build_action_intent(
            work=work,
            candidate=ad_landing_capture,
            purpose=(
                "确定性点击首个弹窗广告入口，连续留存广告源图与外部浏览器"
                "落地页图，供 alioth_17 一致性判定。"
            ),
        )
    post_publish_branch = _safe_missing_post_publish_overlay_branch(
        work=work,
        eligible=eligible,
    )
    if post_publish_branch is not None:
        return _build_action_intent(
            work=work,
            candidate=post_publish_branch,
            purpose=(
                "当前发帖类型选择层存在低风险业务入口，且 post_publish 尚未覆盖；"
                "确定性进入编辑页留证，不执行最终发布。"
            ),
        )
    parent_restore = _safe_coverage_parent_restore_branch(
        work=work,
        eligible=eligible,
    )
    if parent_restore is not None:
        return _build_action_intent(
            work=work,
            candidate=parent_restore,
            purpose=(
                "当前页是无根底栏的嵌套业务页；确定性执行当前帧真实返回入口，"
                "恢复父级导航后再继续缺页探索。"
            ),
        )
    # 详情／内页：底栏未巡完时先返回，禁止在无底栏页空点历史 sibling。
    # 付费解锁遮罩上 Vision 常只给「点返回」CLICK（校验必失败）；强制系统 BACK。
    # payment 页无论底栏是否完结都先 BACK，避免 home↔付费墙空转。
    observation = work.frontier.observation
    page_type_now = str(observation.page_type or "").casefold()
    metadata_now = (
        observation.metadata if isinstance(observation.metadata, Mapping) else {}
    )
    tabs_hard_incomplete = playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=metadata_now,
    )
    visible_bottom = [
        candidate
        for candidate in eligible
        if candidate.metadata.get("bottom_navigation_tab_seek") is True
        and candidate.metadata.get("coverage_page_seek_contract") is None
        and _bottom_tab_bounds_in_safe_band(
            candidate,
            relax=True,
        )
    ]
    # 糖果等首页影片入口：清障后先于底栏／会员返回。
    home_content_early = _intent_for_home_content_entry(
        work=work,
        eligible=eligible,
    )
    if home_content_early is not None:
        return home_content_early
    # 上方分类第一片取证：须先于会员中心通用返回，否则 NEED_BACK 落到会员中心
    # 会丢掉 sample_seek，回主页后 visited 写不进去，又空点已选中的「最新」。
    home_top_sample_early = _intent_for_home_top_category_sample(
        work=work,
        eligible=eligible,
    )
    if home_top_sample_early is not None:
        return home_top_sample_early
    # 会员中心全屏套餐页：有真实左上角返回则点之；否则语义 BACK（F02 禁止合成坐标）。
    if membership_center_surface_detected(
        _observation_frame_text_blob(work=work, metadata_now=metadata_now)
    ) or (
        page_type_now == "payment"
        and "会员中心" in str(metadata_now.get("visual_summary") or "")
    ):
        back = _authorized_short_drama_exit_back(work=work, eligible=eligible)
        purpose = (
            "会员中心先点当前帧左上角返回，禁止点套餐或底栏。"
            if back.action_type is not InteractionOperation.BACK
            else "会员中心无左上角视觉返回；使用授权语义 BACK，禁止合成坐标点击。"
        )
        return _build_action_intent(
            work=work,
            candidate=back,
            purpose=purpose,
        )
    # 暗网会员／「立即解锁」落地：只留证，禁止点解锁 CTA／系统 BACK；底栏未完则切下一栏。
    anwang_landing = _anwang_membership_landing_branch(
        work=work,
        eligible=eligible,
        tabs_hard_incomplete=tabs_hard_incomplete,
        page_type_now=page_type_now,
        metadata_now=metadata_now,
        visible_bottom=visible_bottom,
    )
    if anwang_landing is not None:
        return anwang_landing
    if _preview_ended_paywall_surface(work=work, metadata_now=metadata_now):
        # 中部广告格外链浏览器；点「<」也易偏到广告。付费／试看结束统一系统 BACK。
        back = _synthetic_system_back_candidate(work=work)
        return _build_action_intent(
            work=work,
            candidate=back,
            purpose="试看结束／预售付费页用系统 BACK 返回，禁止点广告格或抢购。",
        )
    # 没有真实底栏时才优先用当前返回候选恢复导航，仍禁止凭空构造 BACK。
    # 短剧待退出：禁止抢先系统 BACK（播放器常吞 BACK），交给下方左上角点击支路。
    # 真挡路弹窗：先关层截图，禁止详情／付费页抢先系统 BACK 逃逸。
    # 非短剧上下文：用系统 BACK（或帧内 BACK 候选），禁止合成左上角误点广告外跳。
    if (
        not visible_bottom
        and not short_drama_exit_pending(metadata_now)
        and not _blocking_popup_requires_dismiss(work)
        and page_type_now in {
            "content_detail",
            "video_detail",
            "post_detail",
            "settings",
            "payment",
        }
        and (
            tabs_hard_incomplete
            or page_type_now == "payment"
            or (
                _uses_dynamic_app_frontier(work)
                and any(item.action_type is InteractionOperation.BACK for item in eligible)
                and all(
                    item.action_type in {
                        InteractionOperation.BACK,
                        InteractionOperation.OBSERVE,
                        InteractionOperation.TIMED_CAPTURE,
                    }
                    for item in eligible
                )
            )
            or coverage_incomplete_allows_bottom_nav_through_overlay(
                coverage_metadata=metadata_now,
            )
        )
    ):
        eligible_back = next(
            (
                candidate
                for candidate in eligible
                if candidate.action_type is InteractionOperation.BACK
                and candidate.risk_level
                in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
            ),
            None,
        )
        back = eligible_back or _synthetic_system_back_candidate(work=work)
        back = _with_post_detail_exit_prefer_tabs(back)
        return _build_action_intent(
            work=work,
            candidate=back,
            purpose="系统 BACK 离开详情后继续探索，禁止点广告格。",
        )
    # 真挡路弹窗（popup/modal/ad）：必须先关并截图，禁止被「可见底栏」抢先。
    # 录屏：弹窗下方 X 逐个关掉后才到主页；软 overlay（详情返回误标）不走此支。
    # 禁止 incomplete+noop+visible_bottom 旁路——否则会在广告网格仍在时
    # 点穿底栏「短剧」并记假 completed（7c5e30d4／用户录屏证伪）。
    # 满帧／清障无业务进展／同层空点让路后，交还底栏与覆盖寻页，不再 return None。
    # 主页↔详情已来回：假关层会点进影片再 BACK，不得再关。
    oscillation_yield = _home_detail_oscillation_yields_to_tabs(work)
    if (
        _blocking_popup_requires_dismiss(work)
        and not short_drama_exit_pending(metadata_now)
        and not oscillation_yield
    ):
        # 账号凭证：禁止走无 X 促销／关层支，必须由上方凭证二段式收口。
        if _work_shows_profile_credential(work):
            if _profile_credential_save_phase_done(work):
                dismiss = _synthetic_profile_credential_dismiss_candidate(work=work)
                if dismiss is not None:
                    return _build_action_intent(
                        work=work,
                        candidate=dismiss,
                        purpose=(
                            "账号凭证弹层仍在；已点过保存凭证，"
                            "确定性点击「我已保存」关闭，禁止点下方 X。"
                        ),
                    )
            else:
                save = _synthetic_profile_credential_save_candidate(work=work)
                if save is not None:
                    return _build_action_intent(
                        work=work,
                        candidate=save,
                        purpose=(
                            "账号凭证弹层仍在；先点蓝色「保存凭证」，"
                            "禁止点下方 X 逃逸。"
                        ),
                    )
        if (
            _verified_circular_popup_x_needs_rebind(work)
            and not _is_hjll_airtest_work(work)
        ):
            return synthetic_popup_clearance_observe_intent(
                work=work,
                purpose=(
                    "已验证的弹层圆形 X 连续未生效；重新截图重绑关闭控件，"
                    "禁止降级为促销蒙层点击或系统返回。"
                ),
            )
        # 无 X 终身卡／抢购促销：点蒙层空白处关闭，禁止点抢购／假关闭／提前点上方栏。
        # 短剧待退出的解锁／付费层跳过本支，交给下方左上角返回。
        # 国庆尊享卡／立即去抢购半屏：课纲不应进入；蒙层点会打在卡片或抢购键上，必须立刻 BACK。
        if _work_is_no_close_wait_promo(work):
            allowed = work.boundaries.allowed_operations
            if _is_hjll_airtest_work(work):
                # HJLL 已废弃「无 X 就点蒙层／系统 BACK」旧流程。
                # Airtest 只允许重截图后点中央下方圆 X；未重绑则观察，交给无进展保护收口。
                close = _synthetic_hjll_airtest_popup_close_candidate(work=work)
                if close is not None and (
                    InteractionOperation.CLICK in allowed
                    or InteractionOperation.TAP in allowed
                ):
                    return _build_action_intent(
                        work=work,
                        candidate=close,
                        purpose="HJLL 促销弹窗按 Airtest 中央下方圆 X 关闭；禁止点蒙层、抢购或系统 BACK。",
                    )
                if (
                    InteractionOperation.OBSERVE in allowed
                    or InteractionOperation.TIMED_CAPTURE in allowed
                ):
                    return synthetic_popup_clearance_observe_intent(
                        work=work,
                        purpose="HJLL 促销弹窗未重绑到中央圆 X；重新截图，禁止旧蒙层点击。",
                    )
                return None
            if _promo_paywall_requires_system_back(work) and (
                InteractionOperation.BACK in allowed
            ):
                back = _synthetic_system_back_candidate(work=work)
                return _build_action_intent(
                    work=work,
                    candidate=back,
                    purpose=(
                        "当前为尊享卡／立即抢购付费半屏，课纲不应进入此页；"
                        "系统返回离开播放器，禁止点抢购或蒙层，回到主页再续上方分类。"
                    ),
                )
            attempt_raw = metadata_now.get("surface_phase_same_popup_attempt_count")
            noop_raw = metadata_now.get("dismiss_noop_streak")
            attempt = (
                int(attempt_raw)
                if isinstance(attempt_raw, int) and not isinstance(attempt_raw, bool)
                else 0
            )
            noop = (
                int(noop_raw)
                if isinstance(noop_raw, int) and not isinstance(noop_raw, bool)
                else 0
            )
            # 蒙层空白连点仍不消：改系统返回，避免整夜空转关层。
            if (
                (attempt >= 3 or noop >= 2)
                and InteractionOperation.BACK in allowed
            ):
                back = _synthetic_system_back_candidate(work=work)
                return _build_action_intent(
                    work=work,
                    candidate=back,
                    purpose=(
                        "无关闭 X 的促销／终身卡层空白点击多次无效；"
                        "改系统返回离开遮罩，再继续底栏／上方分类。"
                    ),
                )
            if InteractionOperation.CLICK in allowed or InteractionOperation.TAP in allowed:
                return synthetic_no_close_promo_scrim_dismiss_intent(work=work)
            if (
                InteractionOperation.OBSERVE in allowed
                or InteractionOperation.TIMED_CAPTURE in allowed
            ):
                return synthetic_popup_clearance_observe_intent(
                    work=work,
                    purpose=(
                        "当前为无关闭 X 的促销／终身卡层且本帧不允许点击；"
                        "观察重采后再点蒙层空白处关闭。"
                    ),
                )
            return None
        popup_close = _popup_close_candidate_for_obstruction(
            work=work, eligible=eligible
        )
        if popup_close is not None:
            # cloud_back 必须把底栏／cloud_back 写进 purpose，避免驱动只见
            # 「遮挡弹层」文案后退化成 keyevent 4 退桌面。
            if (
                popup_close.metadata.get("cloud_back_popup_close") is True
                or "底部导航栏" in str(popup_close.semantic_target or "")
                or "cloud_back"
                in str(popup_close.metadata.get("purpose") or "").casefold()
            ):
                close_purpose = (
                    "弹窗广告仍在：A/B/C 不可用，连点两下底部导航栏最右侧「<」关窗"
                    "（cloud_back_tap_twice；中间禁止截图；"
                    "禁止当开屏点进入／禁止keyevent4）"
                )
            else:
                close_purpose = (
                    "当前业务页仍有遮挡弹层；按 Watcher cascade "
                    "（文案→几何安全X→蒙层）关闭并截图，再巡检底栏与覆盖缺口。"
                )
            return _build_action_intent(
                work=work,
                candidate=popup_close,
                purpose=close_purpose,
            )
        # 有挡路弹窗但尚无可靠关闭点：未满让路预算前不空点底栏（会被弹层吞掉）。
        # 允许 OBSERVE 时强制重采续发关层；仅 CLICK 时交还动态模型（广告取证等）。
        if not _home_popup_budget_allows_coverage_seek(work):
            allowed = work.boundaries.allowed_operations
            if (
                InteractionOperation.OBSERVE in allowed
                or InteractionOperation.TIMED_CAPTURE in allowed
            ):
                return synthetic_popup_clearance_observe_intent(work=work)
            return None
    # 短剧播放器：见解锁付费层后先点左上角返回回根，再切下一栏（即使用户帧仍见底栏）。
    short_drama_exit = _short_drama_playback_exit_branch(
        work=work,
        eligible=eligible,
        tabs_hard_incomplete=tabs_hard_incomplete,
        page_type_now=page_type_now,
        metadata_now=metadata_now,
        now=clock,
    )
    if short_drama_exit is not None:
        return short_drama_exit
    # 糖果等：清障进首页后，有影片入口先点进（立即看／今日新剧），再巡底栏。
    home_content = _intent_for_home_content_entry(work=work, eligible=eligible)
    if home_content is not None:
        return home_content
    # 主页上方分类未完：先于底栏切短剧（课纲 home_top_category_tabs → bottom_tabs）。
    # 主页↔详情空转：跳过假关层，但仍须走 Airtest 上方课纲下一栏（推荐／国产／动漫…），
    # 禁止反复点已访栏；上方课纲完或本帧无法签发后再交还底栏。
    # 每栏须先点列表第一片取证并 BACK，才记已访并切下一栏（详情页支路已早签发）。
    home_top_sample = _intent_for_home_top_category_sample(
        work=work,
        eligible=eligible,
    )
    if home_top_sample is not None:
        return home_top_sample
    bounce_raw = metadata_now.get("home_detail_bounce_count")
    bounce_count = (
        int(bounce_raw)
        if isinstance(bounce_raw, int) and not isinstance(bounce_raw, bool)
        else 0
    )
    allow_home_top_through_false_popup = oscillation_yield or (
        _home_top_curriculum_still_open(work)
        and (
            metadata_now.get("post_detail_exit_prefer_tabs") is True
            or bounce_count >= 2
        )
    )
    home_top = _intent_for_home_top_category_tabs(
        work=work,
        eligible=eligible,
        allow_through_false_popup=allow_home_top_through_false_popup,
    )
    if home_top is not None:
        return home_top
    if _home_top_category_blocks_bottom_tabs(work):
        # incomplete 但本帧仍无法签发（无槽位等）：观察等稳，禁止切短剧。
        # 主页↔详情空转也不得借 oscillation 跳过上方课纲。
        wait = _observe_wait_for_next_playbook_tab(work=work, eligible=eligible)
        if wait is not None:
            return wait
        return None
    yield_through_overlay = _home_popup_budget_allows_coverage_seek(work)
    # 弹层已清／软遮挡／清障让路：底栏硬覆盖未完 + 根页可见底栏 → 按课纲下一栏切栏。
    # 禁止中部假关层点穿进详情（4013f0ff 空转）。
    prefer_tabs_over_dismiss = _should_prefer_bottom_tabs_over_dismiss(
        work=work,
        tabs_hard_incomplete=tabs_hard_incomplete,
        visible_bottom=visible_bottom,
    )
    if _home_top_curriculum_still_open(work):
        # 国产／动漫／漫画未点完：禁止 oscillation／可见底栏旁路切短剧。
        return None
    if prefer_tabs_over_dismiss or oscillation_yield or (
        visible_bottom
        and tabs_hard_incomplete
        and (not _blocking_popup_requires_dismiss(work) or yield_through_overlay)
    ):
        chosen = _pick_next_playbook_bottom_tab(
            work=work,
            visible_bottom=visible_bottom,
        )
        if chosen is None and yield_through_overlay:
            chosen = _fallback_visible_bottom_tab_while_yielding(
                work=work,
                visible_bottom=visible_bottom,
            )
        if chosen is not None:
            purpose = (
                "主页与详情已来回点穿且底栏未完；停止假关层与上方分类空点，"
                "按课纲下一栏切栏并截图留证。"
                if oscillation_yield
                else (
                    "弹层已清或清障已让路且底栏硬覆盖未完；按课纲下一栏切栏并截图留证"
                    "（主页只截不点）。"
                )
            )
            return _build_action_intent(
                work=work,
                candidate=chosen,
                purpose=purpose,
            )
        # 下一栏本帧未见：观察等稳，禁止插值猜点／乱点其它栏（34f8317f 死锁）。
        wait = _observe_wait_for_next_playbook_tab(work=work, eligible=eligible)
        if wait is not None:
            return wait
    # 无挡路弹窗／已满帧让路：底栏可见则优先切栏（换栏后若出新广告再关）。
    if visible_bottom and coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=metadata_now,
    ):
        chosen = _pick_next_playbook_bottom_tab(
            work=work,
            visible_bottom=visible_bottom,
        )
        if chosen is None and yield_through_overlay:
            chosen = _fallback_visible_bottom_tab_while_yielding(
                work=work,
                visible_bottom=visible_bottom,
            )
        if chosen is not None:
            return _build_action_intent(
                work=work,
                candidate=chosen,
                purpose=(
                    "当前根业务页底栏可见且尚未巡检完；"
                    "按课纲下一栏确定性点击并截图留证。"
                ),
            )
        if tabs_hard_incomplete:
            wait = _observe_wait_for_next_playbook_tab(work=work, eligible=eligible)
            if wait is not None:
                return wait
    # 底栏硬覆盖未完且 seek 池空：先试同帧像素合成下一栏，失败再观察等稳。
    if tabs_hard_incomplete and not visible_bottom:
        if not _blocking_popup_requires_dismiss(work):
            chosen = _pick_next_playbook_bottom_tab(
                work=work,
                visible_bottom=(),
            )
            if chosen is not None:
                return _build_action_intent(
                    work=work,
                    candidate=chosen,
                    purpose=(
                        "弹层已清且底栏硬覆盖未完；UI Tree 未见底栏但同帧像素已校准"
                        "课纲下一栏，按像素槽切栏并截图留证。"
                    ),
                )
        wait = _observe_wait_for_next_playbook_tab(work=work, eligible=eligible)
        if wait is not None:
            return wait
    coverage_branch = _safe_missing_current_frame_coverage_branch(
        work=work,
        eligible=eligible,
    )
    if coverage_branch is None:
        return None
    if coverage_branch.metadata.get("home_top_category_tab_seek") is True:
        purpose = (
            "当前主页仍有未点过的上方分类（最新／国产／动漫／漫画）；"
            "确定性点击该分类并截图留证，完成后再切底栏。"
        )
    elif coverage_branch.metadata.get("home_category_grid_seek") is True:
        purpose = (
            "当前帧可见未点过分栏目／宫格入口；"
            "确定性点击该入口并截图留证，再继续巡检其余分栏目。"
        )
    elif coverage_branch.metadata.get("bottom_navigation_tab_seek") is True:
        purpose = (
            "当前根业务页仍有未点过的底栏 Tab；"
            "确定性点击该底栏入口并截图留证，再继续巡检其余底栏。"
        )
    else:
        purpose = (
            "当前截图已有低风险入口直达尚未覆盖页型；"
            "确定性执行连续寻页动作，省略重复的 Explorer 模型选路。"
        )
    return _build_action_intent(
        work=work,
        candidate=coverage_branch,
        purpose=purpose,
    )


def _observation_visual_analysis_proxy(
    observation: object,
    extra_candidates: Sequence[object] | None = None,
) -> SimpleNamespace:
    """把观察帧补成关层／上方栏判定所需的 Vision 分析代理。"""

    metadata = getattr(observation, "metadata", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    facts = metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}
    return SimpleNamespace(
        page_state=getattr(observation, "page_state", None),
        visual_page_type=getattr(observation, "page_type", None),
        visual_summary=metadata.get("visual_summary") or "",
        key_elements=metadata.get("key_elements") or (),
        surface_facts=facts,
        candidates=tuple(extra_candidates or ()),
        overlay_signature=getattr(observation, "overlay_signature", None),
    )


def _intent_for_home_content_entry(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> ActionIntent | None:
    """清障后首页：有影片入口先点进；再交底栏语意相对巡检。

    动态 Frontier 也生效（糖果短剧试点）；挡路弹窗时不抢清障。
    """

    from services.api.app.application.app_ui_tree_features import (
        home_content_entry_enabled,
        home_content_entry_first_card_bounds,
        home_content_entry_forbid_markers,
        home_content_entry_prefer_markers,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    if not home_content_entry_enabled(package_name=package_name):
        return None
    if str(observation.page_type or "").casefold() != "home":
        return None
    page_state = str(observation.page_state or "").casefold()
    # 开屏／倒计时硬阶段不抢。底部飘条常被 Vision 标 popup，但仍须点「立即看」；
    # 仅中央硬广告／契约广告在未满帧让路前挡进片（23:41 QC）。
    if page_state in {"startup_ad", "countdown", "splash"}:
        return None
    yield_through = _home_popup_budget_allows_coverage_seek(work)
    if _has_center_hard_popup_dismiss(work) and not yield_through:
        return None
    facts = metadata.get("surface_facts") if isinstance(metadata, Mapping) else None
    if not isinstance(facts, Mapping):
        facts = {}
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if (
        dialog_kind
        in {
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
        and not yield_through
    ):
        return None
    # 已取到详情／播放页证据则让路底栏，避免反复点进同一片。
    visited = metadata.get("visited_page_types") or metadata.get("evidenced_page_types")
    if isinstance(visited, (list, tuple, set, frozenset)):
        folded = {str(item).casefold() for item in visited if str(item or "").strip()}
        if folded & {"video_detail", "content_detail", "payment"}:
            return None
    flow = metadata.get("app_flow_contract")
    if isinstance(flow, Mapping):
        missing = flow.get("missing_reasons")
        # home 未到齐时仍可点进；若业务页型已满足影片类则跳过
        if flow.get("home_arrived") is True and isinstance(missing, (list, tuple)):
            missing_fold = {str(item).casefold() for item in missing}
            if not any(
                token in reason
                for reason in missing_fold
                for token in ("video", "content_detail", "page_types")
            ):
                # 仍可能缺 profile 等；仅当明确不再缺影片类时跳过
                pass
    prefer = home_content_entry_prefer_markers(package_name=package_name)
    forbid = home_content_entry_forbid_markers(package_name=package_name)
    scored: list[tuple[int, CandidateAction]] = []
    for candidate in eligible:
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
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            continue
        if candidate.metadata.get("home_top_category_tab_seek") is True:
            continue
        if candidate.metadata.get("home_category_grid_seek") is True:
            continue
        target = str(candidate.semantic_target or "").strip()
        blob = " ".join(
            (
                target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("text") or ""),
                str(candidate.metadata.get("content_desc") or ""),
            )
        )
        # 上方分类豆不得当影片入口（QC 9ced42b8：prefer「最新」误点顶栏后 Frontier 崩）。
        if "上方分类" in blob or "上方分類" in blob:
            continue
        if any(marker and marker in blob for marker in forbid):
            continue
        bounds = candidate.metadata.get("normalized_bounds")
        center_y = None
        if isinstance(bounds, Mapping):
            try:
                top = float(bounds.get("top"))
                bottom = float(bounds.get("bottom"))
                center_y = (top + bottom) / 2.0
            except (TypeError, ValueError):
                center_y = None
        if center_y is not None and center_y <= 0.28:
            bare = target
            for prefix in ("上方分类「", "上方分類「"):
                if bare.startswith(prefix) and bare.endswith("」"):
                    bare = bare[len(prefix) : -1].strip()
                    break
            top_hints = (
                "推荐",
                "推薦",
                "原创",
                "原創",
                "短剧",
                "短劇",
                "漫画",
                "漫畫",
                "最新",
                "热门",
                "熱門",
            )
            if bare in top_hints or any(h == bare for h in top_hints):
                continue
        score = 0
        for marker in prefer:
            if marker and marker.casefold() in blob.casefold():
                score += 40 if marker in {"立即看", "立即觀看"} else 20
        expected = str(candidate.metadata.get("expected_page_type") or "").casefold()
        if expected in {"video_detail", "content_detail"}:
            score += 30
        hint = str(candidate.metadata.get("expected_state_hint") or "").casefold()
        # 整词语义，禁止子串 play⊂display／player。
        if hint in {"play", "watch", "detail"} or hint.endswith("_detail") or "video" in hint:
            score += 15
        if score <= 0:
            continue
        scored.append((score + int(candidate.exploration_value), candidate))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = scored[0][1]
        return _build_action_intent(
            work=work,
            candidate=CandidateAction(
                candidate_id=chosen.candidate_id,
                semantic_target=chosen.semantic_target,
                action_type=chosen.action_type,
                risk_level=chosen.risk_level,
                exploration_value=max(chosen.exploration_value, 90),
                navigation_depth=chosen.navigation_depth,
                expected_surface_kind=chosen.expected_surface_kind,
                prior_failure_count=chosen.prior_failure_count,
                metadata={
                    **dict(chosen.metadata),
                    "home_content_entry_seek": True,
                    "require_screenshot_evidence": True,
                    "purpose": (
                        f"首页影片入口「{chosen.semantic_target}」可点则点进取证；"
                        "完成后回首页再巡底栏（语意图标+相对五等分）。"
                    ),
                },
                protocol_version=chosen.protocol_version,
            ),
            purpose=(
                f"首页有影片入口：点「{chosen.semantic_target}」进入取证；"
                "再底栏语意图标+相对位置巡检。"
            ),
        )
    bounds = home_content_entry_first_card_bounds(package_name=package_name)
    if bounds is None:
        return None
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    ).strip() or "unknown"
    synthetic = CandidateAction(
        candidate_id=f"forced-pixel-home-content-entry:{capture_id}",
        semantic_target="首页今日新剧第一片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": "首页影片区相对第一卡点进取证；禁止绝对px乱点",
            "frontier_scope": "local",
            "home_content_entry_seek": True,
            "forced_pixel_home_content_entry": True,
            "require_screenshot_evidence": True,
            "expected_state_hint": "home_content_entry_first_card",
            "normalized_bounds": dict(bounds),
        },
    )
    return _build_action_intent(
        work=work,
        candidate=synthetic,
        purpose=(
            "首页无可语义「立即看」时：相对点击今日新剧第一卡取证；"
            "完成后底栏语意图标+相对五等分。"
        ),
    )


def _intent_for_home_top_category_sample(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    force_label: str | None = None,
) -> ActionIntent | None:
    """上方分类「第一片取证 → BACK」微流程优先于切下一栏。

    force_label：画面已停在课纲下一栏（如「最新」）但尚未 begin sample 时，
    直接点列表第一片，禁止空点已选中栏或跳底栏。
    """

    if _uses_dynamic_app_frontier(work):
        return None
    from services.api.app.application.app_coverage_page_seek import (
        HOME_TOP_SAMPLE_PHASE_NEED_BACK,
        HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
        HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
        home_top_category_sample_in_progress,
        home_top_category_sample_label,
        home_top_category_sample_phase,
    )
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_sample_evidence_page_types,
        home_top_category_sample_first_card_bounds,
        home_top_category_sample_first_entry_enabled,
        home_top_category_sample_match_texts,
        home_top_category_sample_target_bounds,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    if not home_top_category_sample_first_entry_enabled(package_name=package_name):
        return None
    force = str(force_label or "").strip() or None
    if not home_top_category_sample_in_progress(metadata):
        if force is None:
            return None
        phase = HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
        sample_label = force
    else:
        phase = home_top_category_sample_phase(metadata)
        sample_label = home_top_category_sample_label(metadata) or ""
    page_type = str(observation.page_type or "").casefold()
    evidence_types = home_top_category_sample_evidence_page_types(
        package_name=package_name,
    )

    if phase in {
        HOME_TOP_SAMPLE_PHASE_NEED_BACK,
        HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
    } and page_type in evidence_types:
        # 取证页禁止点抢购／解锁 CTA；need_evidence 时若本帧已有 after 截图路径则直接 BACK。
        if phase == HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE:
            observe = next(
                (
                    candidate
                    for candidate in eligible
                    if candidate.action_type is InteractionOperation.OBSERVE
                    and candidate.risk_level
                    in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
                ),
                None,
            )
            if observe is not None and metadata.get(
                "home_top_category_sample_evidence_ready"
            ) is not True:
                return _build_action_intent(
                    work=work,
                    candidate=CandidateAction(
                        candidate_id=observe.candidate_id,
                        semantic_target=observe.semantic_target,
                        action_type=observe.action_type,
                        risk_level=observe.risk_level,
                        exploration_value=observe.exploration_value,
                        navigation_depth=observe.navigation_depth,
                        expected_surface_kind=observe.expected_surface_kind,
                        prior_failure_count=observe.prior_failure_count,
                        metadata={
                            **dict(observe.metadata),
                            "home_top_category_sample_seek": True,
                            "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
                            "require_screenshot_evidence": True,
                        },
                        protocol_version=observe.protocol_version,
                    ),
                    purpose=(
                        f"上方分类「{sample_label}」第一片已进入取证页；"
                        "截图留证后立即 BACK，禁止点抢购／解锁。"
                    ),
                )
        # 取证后的试看结束／付费页广告格密集：系统 BACK，禁止点「<」／广告（易外跳浏览器）。
        back = _synthetic_system_back_candidate(work=work)
        return _build_action_intent(
            work=work,
            candidate=CandidateAction(
                candidate_id=back.candidate_id,
                semantic_target=back.semantic_target,
                action_type=back.action_type,
                risk_level=back.risk_level,
                exploration_value=back.exploration_value,
                navigation_depth=back.navigation_depth,
                expected_surface_kind=back.expected_surface_kind,
                prior_failure_count=back.prior_failure_count,
                metadata={
                    **dict(back.metadata),
                    "purpose": (
                        f"上方分类「{sample_label}」第一片取证完成；"
                        "系统 BACK 返回主页，再记该栏已访并切下一栏；禁止点抢购／广告。"
                    ),
                    "home_top_category_sample_seek": True,
                    "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_BACK,
                    "forced_home_top_sample_back": True,
                    "forced_detail_exit_back": True,
                    "post_detail_exit_prefer_tabs": False,
                },
                protocol_version=back.protocol_version,
            ),
            purpose=(
                f"上方分类「{sample_label}」第一片取证完成；"
                "系统 BACK 返回主页，再记该栏已访并切下一栏；禁止点抢购／广告。"
            ),
        )

    # 取证中误入非 home／非取证页：先 BACK 回主页再重试第一片。
    if (
        phase == HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
        and page_type
        and page_type != "home"
        and page_type not in evidence_types
    ):
        back = _authorized_short_drama_exit_back(work=work, eligible=eligible)
        return _build_action_intent(
            work=work,
            candidate=CandidateAction(
                candidate_id=back.candidate_id,
                semantic_target=back.semantic_target,
                action_type=back.action_type,
                risk_level=back.risk_level,
                exploration_value=back.exploration_value,
                navigation_depth=back.navigation_depth,
                expected_surface_kind=back.expected_surface_kind,
                prior_failure_count=back.prior_failure_count,
                metadata={
                    **dict(back.metadata),
                    "home_top_category_sample_seek": True,
                    "home_top_category_sample_phase": phase,
                    "forced_home_top_sample_back": True,
                    "short_drama_top_left_back": back.action_type
                    is not InteractionOperation.BACK,
                },
                protocol_version=back.protocol_version,
            ),
            purpose=(
                f"上方分类「{sample_label}」取证中误入非列表页；"
                "有视觉「<」则点之，否则语义 BACK 回主页后重试列表第一片。"
            ),
        )

    if phase == HOME_TOP_SAMPLE_PHASE_NEED_BACK and page_type == "home":
        # 已回主页：交还上方课纲点国产／动漫／漫画，禁止观察等底栏短剧。
        return None
    if (
        phase == HOME_TOP_SAMPLE_PHASE_NEED_BACK
        and page_type
        and page_type != "home"
        and page_type not in evidence_types
    ):
        # 取证 BACK 落到会员中心／其它页：继续回主页，禁止切短剧。
        back = _authorized_short_drama_exit_back(work=work, eligible=eligible)
        return _build_action_intent(
            work=work,
            candidate=CandidateAction(
                candidate_id=back.candidate_id,
                semantic_target=back.semantic_target,
                action_type=back.action_type,
                risk_level=back.risk_level,
                exploration_value=back.exploration_value,
                navigation_depth=back.navigation_depth,
                expected_surface_kind=back.expected_surface_kind,
                prior_failure_count=back.prior_failure_count,
                metadata={
                    **dict(back.metadata),
                    "purpose": (
                        f"上方分类「{sample_label}」取证返回误入非主页；"
                        "有视觉「<」则点之，否则语义 BACK 回主页后再切课纲下一栏。"
                    ),
                    "home_top_category_sample_seek": True,
                    "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_BACK,
                    "forced_home_top_sample_back": True,
                    "short_drama_top_left_back": back.action_type
                    is not InteractionOperation.BACK,
                    "post_detail_exit_prefer_tabs": False,
                },
                protocol_version=back.protocol_version,
            ),
            purpose=(
                f"上方分类「{sample_label}」取证返回误入非主页；"
                "有视觉「<」则点之，否则语义 BACK 回主页后再切课纲下一栏，禁止切短剧。"
            ),
        )
    if phase != HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD:
        return None
    if page_type != "home":
        return None
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
    )

    analysis_proxy = _observation_visual_analysis_proxy(
        observation, extra_candidates=eligible
    )
    # 真挡路才跳过取证；软浮层 + popup_satisfied 未清不得卡死第一片。
    if analysis_blocks_home_top_category_tabs(analysis_proxy):
        return None
    # 真挡路才让路；软浮层误标 popup 时 _blocking 已放行，此处不再二次卡死取证。
    if _blocking_popup_requires_dismiss(work) and analysis_blocks_home_top_category_tabs(
        analysis_proxy
    ):
        return None

    # 槽位错位点到日本AV／OnlyFans 后：禁止在错误栏点影片；先点回课纲栏或滑回。
    try:
        from services.api.app.application.app_coverage_page_seek import (
            _candidate_matches_home_top_category_label,
            home_top_category_seek_selection_matches,
            resolve_selected_home_top_category_label,
        )

        selected_now = resolve_selected_home_top_category_label(
            analysis_proxy,
            package_name=package_name,
        )
        if selected_now and sample_label and not home_top_category_seek_selection_matches(
            expected_label=sample_label,
            selected_label=selected_now,
            package_name=package_name,
        ):
            if _home_top_selected_is_far_skip(selected_now) or _home_top_category_strip_scrolled(
                work
            ):
                retarget_pool = [
                    candidate
                    for candidate in eligible
                    if candidate.action_type
                    in {InteractionOperation.CLICK, InteractionOperation.TAP}
                    and candidate.risk_level
                    in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
                    and (
                        candidate.metadata.get("home_top_category_tab_seek") is True
                        or candidate.metadata.get("home_top_category_pixel_visual") is True
                    )
                    and _candidate_matches_home_top_category_label(
                        candidate,
                        next_label=sample_label,
                        package_name=package_name,
                        visual=None,
                    )
                ]
                if retarget_pool:
                    chosen = min(retarget_pool, key=_home_category_grid_selection_key)
                    return _build_action_intent(
                        work=work,
                        candidate=chosen,
                        purpose=(
                            f"上方分类取证目标是「{sample_label}」，"
                            f"当前却停在「{selected_now}」；先点回课纲栏，禁止在日本AV／OnlyFans 点影片。"
                        ),
                    )
                reset = _intent_for_home_top_strip_reset(work=work)
                if reset is not None:
                    return reset
                return None
    except Exception:  # noqa: BLE001
        pass

    # HJLL 不是由通用候选池猜「第一片」：沿用录制脚本按栏目量测的固定 ADB
    # 安全点。这里仍保留主页／遮罩／阶段校验，但不会因 OCR 或卡片框漂移换点。
    if _is_hjll_airtest_work(work):
        fixed_bounds = home_top_category_sample_target_bounds(
            sample_label,
            package_name=package_name,
        )
        if fixed_bounds is not None:
            capture_id = str(
                work.source_capture_id or observation.capture_id or "unknown"
            ).strip() or "unknown"
            fixed = CandidateAction(
                candidate_id=(
                    f"hjll-orchestrated-top-sample:{capture_id}:{sample_label}"
                ),
                semantic_target=f"HJLL 上方分类「{sample_label}」第一部影片",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=work.surface_kind,
                metadata={
                    "purpose": (
                        f"按 HJLL 录制脚本固定落点点击「{sample_label}」第一部影片并截图取证"
                    ),
                    "frontier_scope": "app",
                    "home_top_category_sample_seek": True,
                    "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
                    "home_top_category_sample_label": sample_label,
                    "home_top_category_sample_fixed_airtest": True,
                    "hjll_product_orchestrated_point": True,
                    "require_screenshot_evidence": True,
                    "expected_state_hint": "home_top_sample_first_card",
                    "normalized_bounds": dict(fixed_bounds),
                },
            )
            return _build_action_intent(
                work=work,
                candidate=fixed,
                purpose=(
                    f"HJLL 产品编排：点击「{sample_label}」固定第一片，"
                    "留证后以返回步骤回主页；通用 Agent 不重新选择卡片。"
                ),
            )

    # 列表取证入口：HJLL Airtest 一律点当前栏的第一部影片。
    # 通用产品仍可声明特定文案；无论哪种都不得把上方分类误当内容卡。
    match_texts = home_top_category_sample_match_texts(
        sample_label,
        package_name=package_name,
    )
    content_pool = [
        candidate
        for candidate in eligible
        if candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and candidate.risk_level
        in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        and candidate.metadata.get("home_top_category_tab_seek") is not True
        and candidate.metadata.get("bottom_navigation_tab_seek") is not True
        and _home_top_sample_content_card_candidate(
            candidate,
            allow_lower_grid=True,
        )
        and not _home_top_sample_looks_like_home_chip(candidate)
    ]
    matched_pool = [
        candidate
        for candidate in content_pool
        if match_texts
        and _home_top_sample_candidate_matches_texts(candidate, match_texts)
    ]
    first_card_pool = [
        candidate
        for candidate in content_pool
        if _home_top_sample_first_card_candidate(candidate)
    ]
    # 声明特定文案的产品不得退化为无关第一片；HJLL 新流程不声明此旧特例。
    chosen_pool = matched_pool if match_texts else first_card_pool
    # 严格第一片漏标：再用与底栏相同的「同帧视觉像素」宽松左上内容卡。
    if not chosen_pool and not match_texts:
        vision_pool = [
            candidate
            for candidate in content_pool
            if _home_top_sample_vision_pixel_first_card_candidate(candidate)
        ]
        chosen_pool = vision_pool
    # 语义「首卡／视频卡片」：池内有 Vision 命名但框略偏时仍可点。
    if not chosen_pool and not match_texts:
        semantic_pool = [
            candidate
            for candidate in eligible
            if candidate.action_type
            in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
            and candidate.metadata.get("home_top_category_tab_seek") is not True
            and candidate.metadata.get("bottom_navigation_tab_seek") is not True
            and _home_top_sample_semantic_first_card_candidate(candidate)
        ]
        chosen_pool = semantic_pool
    # 特定文案漏标时才放宽中下部列表带；HJLL 不再使用旧漫画 3D 特例。
    if not chosen_pool and match_texts:
        loose_pool = [
            candidate
            for candidate in eligible
            if candidate.action_type
            in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
            and candidate.metadata.get("home_top_category_tab_seek") is not True
            and candidate.metadata.get("bottom_navigation_tab_seek") is not True
            and _home_top_sample_candidate_matches_texts(candidate, match_texts)
            and _home_top_sample_manga_match_band_candidate(candidate)
        ]
        chosen_pool = loose_pool
    if chosen_pool:
        chosen = min(chosen_pool, key=_home_category_grid_selection_key)
        match_desc = "／".join(match_texts[:2]) if match_texts else "列表左上第一片"
        # 漫画 3D／漏标第一片：一律走底栏同款视觉像素点法。
        pixel_visual = bool(match_texts) or (
            not _home_top_sample_first_card_candidate(chosen)
            and _home_top_sample_vision_pixel_first_card_candidate(chosen)
        ) or chosen.metadata.get("home_top_sample_pixel_visual") is True
        return _build_action_intent(
            work=work,
            candidate=CandidateAction(
                candidate_id=chosen.candidate_id,
                semantic_target=chosen.semantic_target,
                action_type=chosen.action_type,
                risk_level=chosen.risk_level,
                exploration_value=chosen.exploration_value,
                navigation_depth=chosen.navigation_depth,
                expected_surface_kind=chosen.expected_surface_kind,
                prior_failure_count=chosen.prior_failure_count,
                metadata={
                    **dict(chosen.metadata),
                    "home_top_category_sample_seek": True,
                    "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
                    "home_top_category_sample_label": sample_label,
                    "home_top_category_sample_match_texts": list(match_texts),
                    "require_screenshot_evidence": True,
                    "home_top_sample_pixel_visual": pixel_visual,
                    "forced_pixel_home_top_sample": pixel_visual,
                    "expected_state_hint": (
                        "home_top_sample_match_text"
                        if match_texts
                        else "home_top_sample_first_card"
                    ),
                },
                protocol_version=chosen.protocol_version,
            ),
            purpose=(
                f"上方分类「{sample_label}」已选中；"
                + (
                    f"按底栏同款视觉像素点击红框「{match_desc}」进入第二屏取证，"
                    if match_texts
                    else (
                        "按同帧视觉像素点击列表左上第一片进入第二屏取证，"
                        if pixel_visual
                        else f"点击「{match_desc}」进入第二屏取证，"
                    )
                )
                + "截图后立即 BACK，禁止切下一栏或底栏。"
            ),
        )

    # 池空：仿底栏 forced-pixel-*，用课纲第一片槽／目标框合成视觉点击。
    pixel_intent = _synthetic_pixel_home_top_sample_first_card(
        work=work,
        sample_label=sample_label,
        match_texts=match_texts,
        package_name=package_name,
    )
    if pixel_intent is not None:
        return pixel_intent

    bounds = home_top_category_sample_target_bounds(
        sample_label,
        package_name=package_name,
    )
    if bounds is None and not match_texts:
        bounds = home_top_category_sample_first_card_bounds(package_name=package_name)
    if bounds is None:
        return None
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    ).strip() or "unknown"
    match_desc = "／".join(match_texts[:2]) if match_texts else "列表第一片"
    synthetic = CandidateAction(
        candidate_id=(
            f"forced-pixel-home-top-sample-match:{capture_id}:{sample_label}"
            if match_texts
            else f"forced-pixel-home-top-sample-first-card:{capture_id}:{sample_label}"
        ),
        semantic_target=f"上方分类「{sample_label}」{match_desc}",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": (
                f"点击上方分类「{sample_label}」{match_desc}并截图取证；"
                "同帧视觉像素合成（仿底栏）"
            ),
            "frontier_scope": "local",
            "home_top_category_sample_seek": True,
            "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
            "home_top_category_sample_label": sample_label,
            "home_top_category_sample_match_texts": list(match_texts),
            "home_top_category_sample_first_card_synthetic": not bool(match_texts),
            "home_top_category_sample_match_synthetic": bool(match_texts),
            "home_top_sample_pixel_visual": True,
            "forced_pixel_home_top_sample": True,
            "require_screenshot_evidence": True,
            "expected_state_hint": (
                "home_top_sample_match_text"
                if match_texts
                else "home_top_sample_first_card"
            ),
            "normalized_bounds": dict(bounds),
        },
    )
    return _build_action_intent(
        work=work,
        candidate=synthetic,
        purpose=(
            f"上方分类「{sample_label}」已选中且本帧漏标「{match_desc}」；"
            "按底栏同款视觉像素方式点击进入第二屏取证后 BACK。"
        ),
    )


def _home_top_sample_candidate_text(candidate: CandidateAction) -> str:
    return " ".join(
        str(item or "")
        for item in (
            candidate.semantic_target,
            candidate.metadata.get("purpose"),
            candidate.metadata.get("text"),
            candidate.metadata.get("content_desc"),
            candidate.metadata.get("label"),
            candidate.metadata.get("ui_tree_text"),
            candidate.metadata.get("accessibility_text"),
        )
    )


def _home_top_sample_candidate_matches_texts(
    candidate: CandidateAction,
    match_texts: Sequence[str],
) -> bool:
    if candidate.metadata.get("home_top_category_tab_seek") is True:
        return False
    haystack = _home_top_sample_candidate_text(candidate)
    folded = haystack.casefold()
    if any(
        token in haystack
        for token in ("欧美", "歐美", "制服", "OnlyFans", "日本AV", "乱伦", "亂倫")
    ):
        return False
    raw = candidate.metadata.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            top = float(raw.get("top"))
        except (TypeError, ValueError):
            top = 1.0
        # 上方分类条约 y<0.18；3D漫画只在漫画页列表。
        if top < 0.40:
            return False
    for token in match_texts:
        text = str(token or "").strip()
        if not text:
            continue
        if text in haystack or text.casefold() in folded:
            return True
        # 「3D漫画」也匹配标题里的「3D漫畫」等繁简变体已由调用方列入 match_texts。
        compact = text.replace(" ", "").casefold()
        if compact and compact in folded.replace(" ", ""):
            return True
    return False


def _home_top_sample_content_card_candidate(
    candidate: CandidateAction,
    *,
    allow_lower_grid: bool = False,
) -> bool:
    """池内候选是否像内容列表卡（避开上方栏／底栏／抢购）。"""

    text = _home_top_sample_candidate_text(candidate)
    blocked = (
        "抢购",
        "搶購",
        "解锁",
        "解鎖",
        "立即",
        "会员",
        "會員",
        "搜索",
        "搜尋",
        "欧美",
        "歐美",
        "制服",
        "OnlyFans",
        "日本AV",
        "乱伦",
        "亂倫",
    )
    if any(token in text for token in blocked):
        return False
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return False
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return False
    max_top = 0.88 if allow_lower_grid else 0.62
    max_bottom = 0.95 if allow_lower_grid else 0.82
    return (
        0.0 <= left <= 0.72
        and right <= 0.98
        and 0.20 <= top <= max_top
        and bottom <= max_bottom
        and (right - left) >= 0.08
        and (bottom - top) >= 0.05
    )


def _home_top_sample_looks_like_home_chip(candidate: CandidateAction) -> bool:
    """推荐页分类豆／筛条：矮、偏上，不得当成第一片影片。"""

    text = _home_top_sample_candidate_text(candidate)
    chip_tokens = (
        "热门推荐",
        "熱門推薦",
        "最新上架",
        "最新好评",
        "最新好評",
        "最多收藏",
        "国庆抽奖",
        "國慶抽獎",
        "切换",
        "切換",
    )
    if any(token in text for token in chip_tokens):
        return True
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return False
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return False
    height = bottom - top
    width = right - left
    return top <= 0.50 and height < 0.10 and width <= 0.36


def _home_top_sample_first_card_candidate(candidate: CandidateAction) -> bool:
    """池内候选是否像列表左上第一片（避开上方栏／底栏／抢购）。"""

    if not _home_top_sample_content_card_candidate(candidate):
        return False
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return False
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return False
    # 左上视频卡：旧「今日最新」约 y=0.28；现窗分类豆下约 y=0.52。矮分类豆另滤。
    return (
        0.0 <= left <= 0.55
        and right <= 0.62
        and 0.26 <= top <= 0.72
        and bottom <= 0.88
        and (right - left) >= 0.12
        and (bottom - top) >= 0.10
    )


def _home_top_sample_semantic_first_card_candidate(candidate: CandidateAction) -> bool:
    """Vision 已命名「首卡／视频卡片」时，即使框略偏也允许作为第一片取证入口。"""

    text = _home_top_sample_candidate_text(candidate)
    blocked = (
        "抢购",
        "搶購",
        "解锁",
        "解鎖",
        "乱伦",
        "亂倫",
        "日本AV",
        "欧美",
        "歐美",
        "制服",
        "OnlyFans",
        "搜索",
        "搜尋",
    )
    if any(token in text for token in blocked):
        return False
    tokens = (
        "首卡",
        "第一片",
        "视频卡片",
        "視頻卡片",
        "热门推荐视频",
        "熱門推薦視頻",
        "列表左上",
    )
    if not any(token in text for token in tokens):
        return False
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return True
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return True
    return (
        0.0 <= left <= 0.72
        and right <= 0.98
        and 0.18 <= top <= 0.75
        and bottom <= 0.88
        and (right - left) >= 0.08
        and (bottom - top) >= 0.05
    )


def _home_top_sample_manga_match_band_candidate(candidate: CandidateAction) -> bool:
    """漫画红框「3D漫画」常在中下部横滑列表；允许比普通内容卡更低的条带。"""

    text = _home_top_sample_candidate_text(candidate)
    blocked = (
        "抢购",
        "搶購",
        "解锁",
        "解鎖",
        "立即",
        "会员",
        "會員",
        "搜索",
        "搜尋",
        "欧美",
        "歐美",
        "制服",
        "OnlyFans",
        "日本AV",
        "乱伦",
        "亂倫",
    )
    if any(token in text for token in blocked):
        return False
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return True
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return True
    return (
        0.0 <= left <= 0.85
        and right <= 1.0
        and 0.40 <= top <= 0.90
        and bottom <= 0.96
        and (right - left) >= 0.06
        and (bottom - top) >= 0.03
    )


def _home_top_sample_vision_pixel_first_card_candidate(
    candidate: CandidateAction,
) -> bool:
    """仿底栏视觉：同帧漏标严格第一片时，放宽左上内容卡仍可点。"""

    if not _home_top_sample_content_card_candidate(
        candidate, allow_lower_grid=True
    ):
        return False
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return False
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return False
    # 左列视频卡：比严格第一片略宽，仍避开底栏与右侧浮层。
    return (
        0.0 <= left <= 0.58
        and right <= 0.70
        and 0.26 <= top <= 0.74
        and bottom <= 0.90
        and (right - left) >= 0.10
        and (bottom - top) >= 0.08
    )


def _synthetic_pixel_home_top_sample_first_card(
    *,
    work: ExplorationWork,
    sample_label: str,
    match_texts: Sequence[str],
    package_name: str | None,
) -> ActionIntent | None:
    """池空时仿底栏 forced-pixel：从同帧候选挑左上片，否则返回 None 交课纲槽。"""

    del package_name
    eligible = [
        candidate
        for candidate in work.frontier.candidates
        if candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and candidate.risk_level
        in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        and candidate.metadata.get("home_top_category_tab_seek") is not True
        and candidate.metadata.get("bottom_navigation_tab_seek") is not True
    ]
    if match_texts:
        vision_hits = [
            candidate
            for candidate in eligible
            if (
                _home_top_sample_content_card_candidate(
                    candidate, allow_lower_grid=True
                )
                or _home_top_sample_manga_match_band_candidate(candidate)
            )
            and _home_top_sample_candidate_matches_texts(candidate, match_texts)
        ]
    else:
        vision_hits = [
            candidate
            for candidate in eligible
            if _home_top_sample_vision_pixel_first_card_candidate(candidate)
        ]
    if not vision_hits:
        return None
    chosen = min(vision_hits, key=_home_category_grid_selection_key)
    match_desc = "／".join(match_texts[:2]) if match_texts else "列表左上第一片"
    return _build_action_intent(
        work=work,
        candidate=CandidateAction(
            candidate_id=chosen.candidate_id,
            semantic_target=chosen.semantic_target,
            action_type=chosen.action_type,
            risk_level=chosen.risk_level,
            exploration_value=chosen.exploration_value,
            navigation_depth=chosen.navigation_depth,
            expected_surface_kind=chosen.expected_surface_kind,
            prior_failure_count=chosen.prior_failure_count,
            metadata={
                **dict(chosen.metadata),
                "home_top_category_sample_seek": True,
                "home_top_category_sample_phase": "need_first_card",
                "home_top_category_sample_label": sample_label,
                "home_top_category_sample_match_texts": list(match_texts),
                "home_top_sample_pixel_visual": True,
                "forced_pixel_home_top_sample": True,
                "require_screenshot_evidence": True,
                "expected_state_hint": (
                    "home_top_sample_match_text"
                    if match_texts
                    else "home_top_sample_first_card"
                ),
            },
            protocol_version=chosen.protocol_version,
        ),
        purpose=(
            f"上方分类「{sample_label}」池内漏标；"
            f"按底栏同款同帧视觉像素点击「{match_desc}」取证后 BACK。"
        ),
    )


_HOME_TOP_STRIP_OFF_CURRICULUM_MARKERS = (
    "制服",
    "OnlyFans",
    "onlyfans",
    "Only Fans",
    "欧美",
    "歐美",
    "华语原创",
    "華語原創",
    "日本AV",
    "日本av",
    "三级片",
    "三級片",
)
_HOME_TOP_STRIP_FAR_SKIP_LABELS = frozenset(
    {
        "日本AV",
        "日本av",
        "制服",
        "OnlyFans",
        "onlyfans",
        "Only Fans",
        "欧美",
        "歐美",
        "华语原创",
        "華語原創",
        "三级片",
        "三級片",
    }
)
_HOME_TOP_STRIP_DEFAULT_SKIP_LABELS = frozenset(
    {
        "推荐",
        "推薦",
        "乱伦",
        "亂倫",
        "乱伦综",
        "亂倫綜",
        "精选短剧",
        "精選短劇",
    }
)


def _home_top_category_frame_blob(work: ExplorationWork) -> str:
    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = metadata.get("surface_facts") if isinstance(metadata.get("surface_facts"), Mapping) else {}
    selected = ""
    if isinstance(facts, Mapping):
        selected = str(
            facts.get("home_top_selected_label")
            or facts.get("pixel_home_top_selected_label")
            or ""
        )
    return " ".join(
        (
            str(metadata.get("ocr_text") or ""),
            str(metadata.get("visual_summary") or ""),
            str(getattr(observation, "ocr_text", "") or ""),
            selected,
        )
    )


def _home_top_selected_is_far_skip(selected: str) -> bool:
    """右侧滑出栏（日本AV／制服等）才算离课纲；推荐／乱伦是默认条，不算滑走。"""

    text = str(selected or "").strip()
    if not text or text in _HOME_TOP_STRIP_DEFAULT_SKIP_LABELS:
        return False
    return text in _HOME_TOP_STRIP_FAR_SKIP_LABELS


def _home_top_category_strip_scrolled(work: ExplorationWork) -> bool:
    """上方栏已滑到制服／OnlyFans／日本AV：槽位下标不再等于动漫／漫画。

    默认主页停在「推荐」、条上仍见「最新」时不得当滑走，否则会空滑、永远不点最新。
    """

    blob = _home_top_category_frame_blob(work)
    if any(marker in blob for marker in _HOME_TOP_STRIP_OFF_CURRICULUM_MARKERS):
        return True
    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = metadata.get("surface_facts") if isinstance(metadata.get("surface_facts"), Mapping) else {}
    selected = ""
    selected_slot = None
    if isinstance(facts, Mapping):
        selected = str(
            facts.get("home_top_selected_label")
            or facts.get("pixel_home_top_selected_label")
            or ""
        ).strip()
        cal = facts.get("pixel_home_top_category_calibration")
        if isinstance(cal, Mapping):
            raw_slot = cal.get("selected_slot_index")
            if isinstance(raw_slot, int):
                selected_slot = raw_slot
        raw_fact_slot = facts.get("home_top_selected_slot_index")
        if selected_slot is None and isinstance(raw_fact_slot, int):
            selected_slot = raw_fact_slot
    if _home_top_selected_is_far_skip(selected):
        return True
    if selected_slot == 6 and selected not in _HOME_TOP_STRIP_DEFAULT_SKIP_LABELS:
        return True
    if "日本AV" in blob and "最新" not in blob and "今日最新" not in blob:
        return True
    return False


def _home_top_label_visible_on_frame(work: ExplorationWork, label: str) -> bool:
    """课纲下一栏若还在当前上方条上，必须直接点，禁止空滑。"""

    text = str(label or "").strip()
    if not text:
        return False
    blob = _home_top_category_frame_blob(work)
    if text in blob:
        return True
    try:
        from services.api.app.application.app_ui_tree_features import (
            home_top_category_tab_aliases_for_label,
        )
        from services.api.app.application.app_visual_home_top_category_calibration import (
            visible_home_top_strip_labels,
        )

        aliases = home_top_category_tab_aliases_for_label(text)
        if any(alias in blob for alias in aliases):
            return True
        return text in visible_home_top_strip_labels(blob)
    except Exception:  # noqa: BLE001
        return False


def _intent_for_home_top_strip_reset(*, work: ExplorationWork) -> ActionIntent | None:
    """条带滑离最新时先横向滑回，禁止按固定槽位点制服当动漫。"""

    observation = work.frontier.observation
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    ).strip() or "unknown"
    allowed = set(work.boundaries.allowed_operations or ())
    if InteractionOperation.SCROLL in allowed or not allowed:
        candidate = CandidateAction(
            candidate_id=f"forced-home-top-strip-reset:{capture_id}",
            semantic_target="上方分类条滑回最新",
            action_type=InteractionOperation.SCROLL,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata={
                "purpose": (
                    "上方分类条已滑离最新／国产／动漫／漫画；"
                    "先在分类条上横向滑回课纲栏，禁止点制服／欧美／OnlyFans／日本AV。"
                ),
                "frontier_scope": "local",
                "home_top_category_strip_reset": True,
                "require_screenshot_evidence": True,
                "normalized_bounds": {
                    "left": 0.12,
                    "top": 0.11,
                    "right": 0.88,
                    "bottom": 0.17,
                },
                "action_parameters": {
                    "from": {"x": 0.18, "y": 0.135},
                    "to": {"x": 0.82, "y": 0.135},
                    "duration_ms": 420,
                },
            },
        )
        return _build_action_intent(
            work=work,
            candidate=candidate,
            purpose=str(candidate.metadata.get("purpose") or ""),
        )
    return _observe_wait_for_next_playbook_tab(work=work, eligible=())


def _intent_for_home_top_category_tabs(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    allow_through_false_popup: bool = False,
) -> ActionIntent | None:
    """主页上方分类课纲未完时签发下一分类点击；弹窗挡路或非主页则跳过。

    allow_through_false_popup：主页↔详情空转后 Vision 仍标 popup 时，仍允许点
    课纲下一栏（国产／动漫…），禁止再点已访的「最新」或默认推荐。
    若当前栏第一片取证未完成，不得签发下一栏。
    """

    if _uses_dynamic_app_frontier(work):
        return None
    observation = work.frontier.observation
    if str(observation.page_type or "").casefold() != "home":
        return None
    from services.api.app.application.app_coverage_page_seek import (
        HOME_TOP_SAMPLE_PHASE_NEED_BACK,
        VISITED_HOME_TOP_CATEGORY_TABS_KEY,
        _candidate_matches_home_top_category_label,
        _next_unvisited_home_top_category_tab_label,
        _visited_home_top_category_tabs,
        home_top_category_sample_in_progress,
        home_top_category_sample_label,
        home_top_category_sample_phase,
        home_top_category_tabs_incomplete,
    )
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
    )
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_tab_slot_bounds,
        ordered_home_top_category_tab_labels,
    )

    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    # 当前栏取证未完成：禁止切下一上方栏。
    # 例外：已 BACK 回主页但 visited 尚未写入——视本栏已取证，直接点国产／动漫／漫画。
    sample_done_on_home = (
        home_top_category_sample_in_progress(metadata)
        and home_top_category_sample_phase(metadata) == HOME_TOP_SAMPLE_PHASE_NEED_BACK
        and str(observation.page_type or "").casefold() == "home"
    )
    if home_top_category_sample_in_progress(metadata) and not sample_done_on_home:
        return None
    analysis_proxy = _observation_visual_analysis_proxy(
        observation, extra_candidates=eligible
    )
    # 当前帧仍有真挡路遮罩：真实上方栏目尚未露出。
    # 软浮层误标 popup 时 analysis_blocks 已放行；不得再用 popup_satisfied=false
    # 卡死课纲「最新」（国庆贴／侧浮层常见）。
    if not allow_through_false_popup:
        if analysis_blocks_home_top_category_tabs(analysis_proxy):
            return None
        # 真挡路才让路关层；page_state 误标 popup 但主页栏已露出时仍点「最新」。
        if _blocking_popup_requires_dismiss(
            work
        ) and analysis_blocks_home_top_category_tabs(analysis_proxy):
            return None
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    if not home_top_category_tabs_incomplete(
        coverage_metadata=metadata,
        package_name=package_name,
    ):
        return None
    ordered = ordered_home_top_category_tab_labels(package_name=package_name)
    if not ordered:
        progress = metadata.get("app_curriculum_progress")
        playbook_id = None
        if isinstance(progress, Mapping):
            raw_id = progress.get("playbook_id")
            if isinstance(raw_id, str) and raw_id.strip():
                playbook_id = raw_id.strip()
        ordered = ordered_home_top_category_tab_labels(
            playbook_id=playbook_id,
            package_name=package_name,
        )
    visited_labels = set(_visited_home_top_category_tabs(metadata))
    if sample_done_on_home:
        done_label = home_top_category_sample_label(metadata)
        if done_label:
            visited_labels.add(done_label)
    next_label = _next_unvisited_home_top_category_tab_label(
        visited_labels=visited_labels,
        ordered_labels=ordered,
    )
    if not next_label:
        return None
    if _home_top_category_strip_scrolled(work) and not _home_top_label_visible_on_frame(
        work, next_label
    ):
        return _intent_for_home_top_strip_reset(work=work)
    # 默认停在「推荐」时仍必须先点课纲第一栏「最新」，禁止把已选中栏当已访。
    # 若画面已停在课纲下一栏（如已点到「最新」）：直接点列表第一片取证，禁止空点已选中栏。
    selected_now = None
    try:
        from services.api.app.application.app_coverage_page_seek import (
            home_top_category_seek_selection_matches,
            resolve_selected_home_top_category_label,
        )
        from services.api.app.application.app_ui_tree_features import (
            home_top_category_sample_first_entry_enabled,
        )

        selected_now = resolve_selected_home_top_category_label(
            analysis_proxy,
            package_name=package_name,
        )
        if selected_now is None and next_label == "最新":
            # 今日最新滤签只在「最新」栏出现；像素未标 selected 时不得空点同一栏。
            frame_blob = " ".join(
                (
                    str(metadata.get("ocr_text") or ""),
                    str(metadata.get("visual_summary") or ""),
                    str(getattr(observation, "ocr_text", "") or ""),
                )
            )
            if "今日最新" in frame_blob:
                selected_now = "最新"
        if (
            selected_now
            and home_top_category_sample_first_entry_enabled(
                package_name=package_name,
            )
            and home_top_category_seek_selection_matches(
                expected_label=next_label,
                selected_label=selected_now,
                package_name=package_name,
            )
        ):
            sample_now = _intent_for_home_top_category_sample(
                work=work,
                eligible=eligible,
                force_label=next_label,
            )
            if sample_now is not None:
                return sample_now
            # 已选中且取证已回主页：视本栏已访，切国产／动漫／漫画，禁止再点「最新」。
            if sample_done_on_home or home_top_category_sample_phase(
                metadata
            ) == HOME_TOP_SAMPLE_PHASE_NEED_BACK:
                visited_labels.add(next_label)
                next_label = _next_unvisited_home_top_category_tab_label(
                    visited_labels=visited_labels,
                    ordered_labels=ordered,
                )
                if not next_label:
                    return None
            else:
                return None
    except Exception:  # noqa: BLE001
        selected_now = None

    # HJLL 的栏目坐标来自录制脚本；通用候选的 OCR／视觉框仅用于确认仍在主页，
    # 不参与改变这个产品步骤的落点，避免新增「精选短剧」后槽位误漂到乱伦等栏。
    if _is_hjll_airtest_work(work):
        fixed_slot = home_top_category_tab_slot_bounds(
            label=next_label,
            package_name=package_name,
        )
        if fixed_slot is not None:
            capture_id = str(
                work.source_capture_id or observation.capture_id or "unknown"
            ).strip() or "unknown"
            fixed = CandidateAction(
                candidate_id=f"hjll-orchestrated-top-tab:{capture_id}:{next_label}",
                semantic_target=f"HJLL 上方分类「{next_label}」",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=work.surface_kind,
                metadata={
                    "purpose": f"按 HJLL 录制脚本固定落点点击上方分类「{next_label}」",
                    "frontier_scope": "app",
                    "home_top_category_tab_seek": True,
                    "home_top_category_tab_fixed_airtest": True,
                    "hjll_product_orchestrated_point": True,
                    "require_screenshot_evidence": True,
                    "expected_state_hint": f"home_top_category:{next_label}",
                    "normalized_bounds": dict(fixed_slot),
                },
            )
            return _build_action_intent(
                work=work,
                candidate=fixed,
                purpose=(
                    f"HJLL 产品编排：按录制顺序点击「{next_label}」，"
                    "通用 Agent 不以候选框重新判定点位。"
                ),
            )

    top_safe = [
        candidate
        for candidate in eligible
        if candidate.metadata.get("home_top_category_tab_seek") is True
        and candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and candidate.risk_level
        in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        and _candidate_matches_home_top_category_label(
            candidate,
            next_label=next_label,
            package_name=package_name,
            visual=None,
        )
    ]
    if top_safe:
        # 优先同帧像素校准，其次 Vision 文字框，再次槽位合成。
        pixel_first = [
            item
            for item in top_safe
            if item.metadata.get("home_top_category_pixel_visual") is True
            or "视觉像素上方分类" in str(item.metadata.get("purpose") or "")
            or str(item.candidate_id or "").startswith("forced-pixel-home-top-category:")
        ]
        vision_first = [
            item
            for item in top_safe
            if item.metadata.get("home_top_category_vision_match") is True
            or item.metadata.get("home_top_category_slot_synthetic") is not True
        ]
        pool = pixel_first or vision_first or top_safe
        chosen = min(pool, key=_home_category_grid_selection_key)
        selected_note = (
            f"；当前画面停在「{selected_now}」不得当作已访"
            if selected_now
            and selected_now != "__skip__"
            and selected_now != next_label
            else ""
        )
        return _build_action_intent(
            work=work,
            candidate=chosen,
            purpose=(
                f"当前主页上方分类课纲下一栏为「{next_label}」{selected_note}；"
                "按当前帧视觉像素／文字框点击该分类并截图留证，"
                "禁止再点已访栏或跳过栏，"
                "必须先完成最新→推荐→国产→动漫→漫画取证后再切底栏。"
            ),
        )
    # 池内漏标：优先同帧像素校准合成，其次课纲 JSON 槽位。
    pixel_intent = _synthetic_pixel_home_top_category(work=work)
    if pixel_intent is not None:
        return pixel_intent
    if _home_top_category_strip_scrolled(work) and not _home_top_label_visible_on_frame(
        work, next_label
    ):
        return _intent_for_home_top_strip_reset(work=work)
    slot = home_top_category_tab_slot_bounds(
        label=next_label,
        package_name=package_name,
    )
    if slot is None:
        return None
    slot_bounds = dict(slot)
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    ).strip() or "unknown"
    synthetic = CandidateAction(
        candidate_id=f"forced-home-top-category:{capture_id}:{next_label}",
        semantic_target=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": f"点击主页上方分类「{next_label}」并截图留证；课纲槽位合成",
            "frontier_scope": "local",
            "home_top_category_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "home_top_category_slot_synthetic": True,
            "expected_state_hint": f"home_top_tab:{next_label}",
            "normalized_bounds": slot_bounds,
            VISITED_HOME_TOP_CATEGORY_TABS_KEY: list(
                metadata.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY) or ()
            )
            if isinstance(metadata.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY), (list, tuple))
            else [],
        },
    )
    return _build_action_intent(
        work=work,
        candidate=synthetic,
        purpose=(
            "当前主页上方分类课纲未完且本帧漏标；"
            f"按槽位合成点击「{next_label}」并截图留证，完成前禁止切底栏短剧。"
        ),
    )


def _synthetic_pixel_home_top_category(
    *,
    work: ExplorationWork,
) -> ActionIntent | None:
    """同帧像素上方分类已检出课纲下一栏时，合成 forced-pixel-home-top-category。"""

    from services.api.app.application.app_coverage_page_seek import (
        VISITED_HOME_TOP_CATEGORY_TABS_KEY,
        _next_unvisited_home_top_category_tab_label,
        _visited_home_top_category_tabs,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )
    from services.api.app.application.app_ui_tree_features import (
        ordered_home_top_category_tab_labels,
    )
    from services.api.app.application.app_visual_home_top_category_calibration import (
        extract_pixel_home_top_category_tabs,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    ordered = ordered_home_top_category_tab_labels(package_name=package_name)
    if not ordered:
        return None
    next_label = _next_unvisited_home_top_category_tab_label(
        visited_labels=_visited_home_top_category_tabs(metadata),
        ordered_labels=ordered,
    )
    if not next_label:
        return None
    if _home_top_category_strip_scrolled(work) and not _home_top_label_visible_on_frame(
        work, next_label
    ):
        return None
    facts = metadata.get("surface_facts")
    tabs = extract_pixel_home_top_category_tabs(
        facts if isinstance(facts, Mapping) else None
    )
    if not tabs and isinstance(facts, Mapping):
        tabs = extract_pixel_home_top_category_tabs({"surface_facts": facts})
    hit = next(
        (
            tab
            for tab in tabs
            if bottom_tab_label_already_visited(tab.label, {next_label})
        ),
        None,
    )
    if hit is None:
        return None
    frame_blob = " ".join(
        (
            str(metadata.get("ocr_text") or ""),
            str(metadata.get("visual_summary") or ""),
            str(getattr(observation, "ocr_text", "") or ""),
        )
    )
    if getattr(hit, "selected", False) is True or (
        next_label == "最新" and "今日最新" in frame_blob
    ):
        # 已停在该栏：禁止再合成空点「最新」。
        return None
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    )
    bounds = {
        "left": float(hit.bounds.left),
        "top": float(hit.bounds.top),
        "right": float(hit.bounds.right),
        "bottom": float(hit.bounds.bottom),
    }
    # 不再 skip 左右挪点：与底栏一样，点像素峰映射到的课纲标签中心。
    purpose = (
        f"点击主页上方分类「{next_label}」并截图留证；当前截图视觉像素上方分类校准"
    )
    synthetic = CandidateAction(
        candidate_id=f"forced-pixel-home-top-category:{capture_id}:{next_label}",
        semantic_target=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "home_top_category_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "home_top_category_pixel_visual": True,
            "forced_pixel_home_top_category": True,
            "expected_state_hint": f"home_top_tab:{next_label}",
            "normalized_bounds": bounds,
            VISITED_HOME_TOP_CATEGORY_TABS_KEY: list(
                metadata.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY) or ()
            )
            if isinstance(metadata.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY), (list, tuple))
            else [],
        },
    )
    return _build_action_intent(
        work=work,
        candidate=synthetic,
        purpose=(
            "当前主页上方分类课纲未完；"
            f"按同帧像素槽位点「{next_label}」并截图留证（对齐底栏视觉方式），"
            "完成前禁止切底栏短剧。"
        ),
    )


def _home_top_curriculum_still_open(work: ExplorationWork) -> bool:
    """上方分类课纲或当前栏第一片取证尚未完成。"""

    if _uses_dynamic_app_frontier(work):
        return False
    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    from services.api.app.application.app_coverage_page_seek import (
        home_top_category_sample_in_progress,
        home_top_category_tabs_incomplete,
    )

    if home_top_category_sample_in_progress(metadata):
        return True
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    return home_top_category_tabs_incomplete(
        coverage_metadata=metadata,
        package_name=package_name,
    )


def _home_top_category_blocks_bottom_tabs(work: ExplorationWork) -> bool:
    """主页上方分类未巡完时禁止切底栏（短剧等）。

    取证中、或国产／动漫／漫画尚未点过：任意页型都挡底栏。
    会员中心／详情不得借 page_type!=home 旁路切短剧（QC 83ce4a7b）。
    """

    return _home_top_curriculum_still_open(work)


def _home_category_grid_selection_key(
    candidate: CandidateAction,
) -> tuple[int, float, float, str]:
    """分栏目优先高价值，其次按屏幕位置从上到下、从左到右点选。"""

    raw = candidate.metadata.get("normalized_bounds")
    top = 0.0
    left = 0.0
    if isinstance(raw, Mapping):
        try:
            top = float(raw.get("top") or 0.0)
            left = float(raw.get("left") or 0.0)
        except (TypeError, ValueError):
            top, left = 0.0, 0.0
    return (-int(candidate.exploration_value), top, left, candidate.candidate_id)


def _bottom_navigation_tab_selection_key(
    candidate: CandidateAction,
) -> tuple[int, int, float, str]:
    """底栏按 playbook 顺序优先，其次从左到右，保证未点栏目不被跳过。"""

    playbook_rank = 99
    raw_order = candidate.metadata.get("playbook_tab_index")
    if isinstance(raw_order, int) and not isinstance(raw_order, bool) and raw_order >= 0:
        playbook_rank = raw_order
    else:
        label = str(candidate.semantic_target or "")
        for index, marker in enumerate(
            ("主页", "首页", "短剧", "短视频", "视频", "暗网", "AI", "社区", "我的")
        ):
            if marker in label:
                playbook_rank = index
                break
    raw = candidate.metadata.get("normalized_bounds")
    left = 0.0
    if isinstance(raw, Mapping):
        try:
            left = float(raw.get("left") or 0.0)
        except (TypeError, ValueError):
            left = 0.0
    return (playbook_rank, -int(candidate.exploration_value), left, candidate.candidate_id)


def _safe_coverage_parent_restore_branch(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> CandidateAction | None:
    """嵌套页先用当前帧真实返回入口恢复父级，禁止固定底栏越级点击。"""

    if not _uses_dynamic_app_frontier(work):
        return None
    observation = work.frontier.observation
    raw_facts = observation.metadata.get("surface_facts")
    facts = raw_facts if isinstance(raw_facts, Mapping) else {}
    page_type = str(observation.page_type or "").casefold()
    # guide／启动页上的系统 BACK 常退出 App 或落到 Chrome FirstRun，禁止当作父级恢复
    if page_type in {
        "guide_page",
        "startup_ad",
        "splash",
        "line_loading",
        "countdown",
    }:
        return None
    if (
        str(observation.page_state or "").casefold()
        not in {"stable", "normal", "loaded", "content_available"}
        or is_obstructed_surface(
            page_state=observation.page_state,
            page_type=observation.page_type,
            overlay_signature=observation.overlay_signature,
            surface_facts=facts,
        )
    ):
        return None
    safe = [
        candidate
        for candidate in eligible
        if candidate.metadata.get("coverage_parent_restore") is True
        and candidate.metadata.get("coverage_page_seek") is True
        and candidate.metadata.get("coverage_page_seek_contract") is None
        and candidate.action_type
        in {InteractionOperation.CLICK, InteractionOperation.BACK}
        and candidate.risk_level is InteractionRiskLevel.LOW
    ]
    if not safe:
        return None
    return max(safe, key=lambda item: (item.exploration_value, item.candidate_id))


def _safe_missing_post_publish_overlay_branch(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> CandidateAction | None:
    """发帖类型选择层直接进入缺失编辑页，避免模型反复选择 BACK。"""

    if not _uses_dynamic_app_frontier(work) or work.app_goal is None:
        return None
    observation = work.frontier.observation
    raw_surface_facts = observation.metadata.get("surface_facts")
    surface_facts = raw_surface_facts if isinstance(raw_surface_facts, Mapping) else {}
    popup_visible = (
        str(observation.page_state or "").casefold() in {"popup", "modal", "overlay", "dialog"}
        or observation.overlay_signature is not None
        or surface_facts.get("popup_visible") is True
    )
    if not popup_visible:
        return None
    if str(observation.page_type or "").casefold() not in {"post_list", "content_list"}:
        return None
    required = {item.casefold() for item in work.app_goal.required_page_types}
    covered = {
        str(item).casefold()
        for item in observation.metadata.get("covered_page_types", ())
        if isinstance(item, str)
    }
    if "post_publish" not in required or "post_publish" in covered:
        return None

    safe: list[CandidateAction] = []
    for candidate in eligible:
        if (
            candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
            or candidate.risk_level
            not in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
            or candidate.metadata.get("frontier_scope") not in {"local", "page"}
            or _text(candidate.metadata.get("expected_next_page_type")) != "post_publish"
        ):
            continue
        terms = " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ).casefold()
        if any(
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
        if not any(marker in terms for marker in ("图文帖子", "视频帖子", "发帖", "post")):
            continue
        safe.append(candidate)
    if not safe:
        return None

    def rank(candidate: CandidateAction) -> tuple[int, int, str]:
        terms = " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
            )
        ).casefold()
        text_post_rank = int(any(marker in terms for marker in ("图文", "文字", "text")))
        return (text_post_rank, candidate.exploration_value, candidate.candidate_id)

    return max(safe, key=rank)


def _bottom_tab_bounds_in_safe_band(
    candidate: CandidateAction,
    *,
    relax: bool = False,
) -> bool:
    """真实底栏触控条带：顶边足够低，避免点到浮层／内容卡。

    relax=True 时与 promote 口径对齐（top≥0.78），避免 Vision 略偏高却选不到底栏。
    """

    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return False
    try:
        top = float(raw.get("top") or 0.0)
        bottom = float(raw.get("bottom") or 0.0)
    except (TypeError, ValueError):
        return False
    min_top = 0.78 if relax else 0.88
    return top >= min_top and bottom > top


def _home_popup_budget_allows_coverage_seek(work: ExplorationWork) -> bool:
    """仅满帧让路后，才允许确定性覆盖／底栏寻页压过关层穿透 popup。

    覆盖缺口未完只用于 popup_chain 交还 Explorer／候选保留，不得在此把底栏
    提到关层之前——否则会空点底栏假 completed，关层候选永远执行不到。
    另：非挡路 overlay 误标时直接放行底栏（回首页常无弹窗）。
    """

    observation = work.frontier.observation
    if not _blocking_popup_requires_dismiss(work):
        # 无真实挡路弹窗（详情返回误标 overlay）：直接放行底栏／覆盖寻页
        return True
    page_state = str(observation.page_state or "").casefold()
    surface_facts = observation.metadata.get("surface_facts")
    popup_visible = (
        page_state in {"popup", "modal", "overlay", "dialog"}
        or observation.overlay_signature is not None
        or (isinstance(surface_facts, Mapping) and surface_facts.get("popup_visible") is True)
    )
    if not popup_visible:
        return False
    metadata = dict(observation.metadata)
    if "required_page_types" not in metadata and work.app_goal is not None:
        metadata["required_page_types"] = list(work.app_goal.required_page_types)
    return overlay_budget_allows_real_coverage_seek(
        page_type=observation.page_type,
        coverage_metadata=metadata,
    )


def _has_center_hard_popup_dismiss(work: ExplorationWork) -> bool:
    """中央挡路广告（下方／中部 X）与侧浮国庆贴区分：有中部关层钮即真硬弹窗。"""

    tokens = ("关闭弹层", "關閉彈層", "关闭广告", "關閉廣告", "关闭开屏", "關閉開屏")
    for candidate in work.frontier.candidates:
        blob = " ".join(
            str(item or "")
            for item in (
                candidate.semantic_target,
                candidate.metadata.get("purpose"),
                candidate.metadata.get("text"),
                candidate.metadata.get("expected_state_hint"),
            )
        )
        if not any(token in blob for token in tokens):
            continue
        raw = candidate.metadata.get("normalized_bounds")
        # 无坐标不得单凭文案判「中央硬弹窗」，否则课纲期裸「关闭广告」候选
        # 会劫持 splash 观察帧（forced-popup-clearance-observe）。
        if not isinstance(raw, Mapping):
            continue
        try:
            left = float(raw.get("left"))
            top = float(raw.get("top"))
            right = float(raw.get("right"))
            bottom = float(raw.get("bottom"))
        except (TypeError, ValueError):
            continue
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        if 0.18 <= cx <= 0.82 and 0.28 <= cy <= 0.92:
            return True
    return False


def _blocking_popup_requires_dismiss(work: ExplorationWork) -> bool:
    """仅真实挡路弹窗才强制关层；详情返回后的 home|overlay 误标不算。

    黑料：回首页时常无弹窗；换栏才出大广告。page_state=overlay／仅
    popup_visible／overlay_signature 不足以触发关层优先。
    海角网格广告：Vision 常标 home + ad_popup_*，底栏仍可见——必须先关下方 X。
    软浮层（国庆贴／分享角标）：Vision 误标 page_state=popup 但主页栏已露出时，
    不得占用关层支路卡死「最新」；analysis_blocks_home_top 已区分真软。
    中央硬广告（糖果短剧等）：即使 page_type=home，有中部关层 X 仍必须先关。
    """

    observation = work.frontier.observation
    page_state = str(observation.page_state or "").casefold()
    page_type = str(observation.page_type or "").casefold()
    analysis_proxy = _observation_visual_analysis_proxy(
        observation,
        extra_candidates=work.frontier.candidates,
    )
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
        popup_phase_blocks_bottom_tabs,
    )

    center_hard = _has_center_hard_popup_dismiss(work)
    # 主页软浮层误标：栏位已露出、无硬广告、无中央关层钮 → 不强制关层。
    soft_home_chrome = (
        page_type == "home"
        and not analysis_blocks_home_top_category_tabs(analysis_proxy)
        and not center_hard
    )
    if center_hard:
        return True
    if page_state in {"popup", "modal", "dialog", "system_dialog", "startup_ad"}:
        if not soft_home_chrome:
            return True
    facts = observation.metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {
        "ad_popup",
        "promo_popup",
        "promo_ad",
        "modal",
        "dialog",
        "system_dialog",
        "age_verification",
        "age_confirm",
        "age_gate",
    }:
        return True
    topmost = facts.get("topmost_ad_popup")
    if isinstance(topmost, Mapping) and topmost:
        return True
    blob = " ".join(
        (
            page_state,
            str(facts.get("page_subtype") or ""),
            str(facts.get("obstruction_kind") or ""),
            str(facts.get("overlay_kind") or ""),
            str(observation.page_type or ""),
        )
    ).casefold()
    if "ad_popup" in blob or "bottom_ad_banner" in blob or "free_watch" in blob:
        if not soft_home_chrome:
            return True
    # 流程契约未满足：禁止当「已清弹窗」切底栏；但主页软浮层不得借此强制关层死循环
    # （QC 91c7b53a：popup_seen 后 popup_satisfied 长期 false，一直点「关闭弹层下方X」
    # 连课纲「最新」都未签发就 before_capture_failed 收口）。
    if popup_phase_blocks_bottom_tabs(observation.metadata):
        if soft_home_chrome:
            return False
        return True
    # 无 X 终身卡／抢购促销：即使 Vision 标 stable，也视为挡路（只许观察等消失）。
    if _work_is_no_close_wait_promo(work):
        return True
    return False


def _promo_paywall_requires_system_back(work: ExplorationWork) -> bool:
    """尊享卡／抢购半屏不是课纲页：禁止点蒙层（会打到卡片或抢购），必须系统返回。"""

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}
    page_type = str(observation.page_type or "").casefold()
    blob = " ".join(
        (
            page_type,
            str(observation.page_state or ""),
            str(facts.get("dialog_kind") or ""),
            str(facts.get("dialog_title") or ""),
            str(facts.get("popup_title") or ""),
            str(metadata.get("visual_summary") or ""),
            " ".join(str(item) for item in (metadata.get("key_elements") or ()) if item),
        )
    )
    if page_type in {"payment", "paywall"}:
        return True
    return any(
        marker in blob
        for marker in (
            "尊享卡",
            "国庆尊享",
            "立即去抢购",
            "立即抢购",
            "永久短剧卡",
            "免费视频剩余",
        )
    )


def _work_is_no_close_wait_promo(work: ExplorationWork) -> bool:
    """当前帧是否为无关闭 X、应等待自消的促销／终身卡层。"""

    from services.api.app.application.app_visual_close_calibration import (
        is_no_close_auto_dismiss_promo,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}
    proxy = _observation_visual_analysis_proxy(
        observation,
        extra_candidates=work.frontier.candidates,
    )
    if _has_verified_circular_popup_x(work):
        return False
    # 摘要已写明底部圆 X／关闭 X 可见时，禁止因「立即抢购」改等自消。
    summary_blob = " ".join(
        (
            str(metadata.get("visual_summary") or ""),
            str(facts.get("dialog_title") or ""),
            str(facts.get("popup_title") or ""),
            " ".join(str(item) for item in (metadata.get("key_elements") or ()) if item),
        )
    )
    if any(
        marker in summary_blob
        for marker in (
            "底部圆",
            "下方圆",
            "圆形关闭",
            "关闭x可见",
            "关闭Ｘ可见",
            "底部关闭x",
            "下方关闭",
            "圆x可关",
            "圆Ｘ可关",
        )
    ):
        return False
    if is_no_close_auto_dismiss_promo(proxy):  # type: ignore[arg-type]
        return True
    # 仅当「无本层关闭点」时，促销文案才强制等自消／点蒙层。
    # 禁止：画面有面板下方真 X，却因「立即抢购」改观察卡住。
    has_layer_dismiss = False
    for candidate in work.frontier.candidates or ():
        role = str(candidate.metadata.get("control_role") or "").casefold()
        terms = " ".join(
            (
                str(candidate.semantic_target or ""),
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.candidate_id or ""),
            )
        ).casefold()
        if role not in {"dismiss", "skip"} and not any(
            marker in terms
            for marker in ("关闭", "關閉", "close", "dismiss", "圆x", "圆ｘ", "下方x")
        ):
            continue
        raw = candidate.metadata.get("normalized_bounds")
        cy = None
        if isinstance(raw, Mapping):
            try:
                cy = (float(raw["top"]) + float(raw["bottom"])) / 2.0
            except (KeyError, TypeError, ValueError):
                cy = None
        # 贴底底飘不算本层
        if cy is not None and cy >= 0.86:
            continue
        if "底飘" in terms or (
            "bottom_banner" in terms and (cy is None or cy >= 0.86)
        ):
            continue
        has_layer_dismiss = True
        break
    if has_layer_dismiss:
        return False
    blob = " ".join(
        (
            str(observation.page_type or ""),
            str(facts.get("dialog_kind") or ""),
            str(facts.get("dialog_title") or ""),
            str(facts.get("popup_title") or ""),
            str(metadata.get("visual_summary") or ""),
            " ".join(str(item) for item in (metadata.get("key_elements") or ()) if item),
        )
    )
    if any(
        marker in blob
        for marker in ("至尊终身卡", "终身卡", "尊享卡", "立即抢购", "立即去抢购")
    ):
        return True
    # 禁止把短剧解锁／普通 payment 页一律当终身卡蒙层空白点。
    return False


def _is_hjll_airtest_work(work: ExplorationWork) -> bool:
    """仅显式课纲或旧配置按完整包名启用固定流程，动态探索沿当前帧选路。"""

    if _uses_dynamic_app_frontier(work):
        return False
    metadata = work.frontier.observation.metadata
    package_name = (
        str(metadata.get("package_name") or "").strip()
        if isinstance(metadata, Mapping)
        else ""
    )
    from services.api.app.application.app_ui_tree_features import (
        resolve_product_orchestrator_id,
    )

    if resolve_product_orchestrator_id(package_name=package_name) == "hjll":
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(
        resolve_product_orchestrator_id(package_name=str(item)) == "hjll"
        for item in allowed
    )


def _hjll_expected_package(work: ExplorationWork) -> str | None:
    """从当前帧或任务白名单取得 HJLL 目标包，绝不猜测第三方前台。"""

    metadata = work.frontier.observation.metadata
    package_name = (
        str(metadata.get("package_name") or "").strip()
        if isinstance(metadata, Mapping)
        else ""
    )
    allowed_packages = {"com.hj.ttthree"}
    if package_name in allowed_packages:
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return next(
        (str(item).strip() for item in allowed if str(item).strip() in allowed_packages),
        None,
    )


def _has_verified_circular_popup_x(work: ExplorationWork) -> bool:
    """当前截图校准的中央圆形 X 优先于促销文案，右下底飘 X 不匹配。"""

    metadata = work.frontier.observation.metadata
    facts = metadata.get("surface_facts") if isinstance(metadata, Mapping) else None
    calibrated = facts.get("pixel_cross_close_calibration") if isinstance(facts, Mapping) else None
    raw_bounds = calibrated.get("normalized_bounds") if isinstance(calibrated, Mapping) else None
    if not isinstance(raw_bounds, Mapping):
        return False
    try:
        left, top = float(raw_bounds["left"]), float(raw_bounds["top"])
        right, bottom = float(raw_bounds["right"]), float(raw_bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return False
    if right <= left or bottom <= top:
        return False
    if str(calibrated.get("close_kind") or "").strip() not in {"bottom_circular_x", "circular_x"}:
        return False
    center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
    # HJLL Airtest 实测安全点是屏幕正中的下方圆 X（约 0.50, 0.80）；
    # 不同广告图高度会把它上移至约 y=0.71。右下角浮动广告的假 X 不可接受。
    return 0.42 <= center_x <= 0.58 and 0.55 <= center_y <= 0.90


def _verified_circular_popup_x_needs_rebind(work: ExplorationWork) -> bool:
    """真 X 连续无效时先重采重绑，禁止转向蒙层或 BACK。"""

    if not _has_verified_circular_popup_x(work):
        return False
    metadata = work.frontier.observation.metadata
    if not isinstance(metadata, Mapping):
        return False
    same_attempts = metadata.get("surface_phase_same_popup_attempt_count")
    noop_streak = metadata.get("dismiss_noop_streak")
    return (
        isinstance(same_attempts, int)
        and not isinstance(same_attempts, bool)
        and same_attempts >= 2
    ) or (
        isinstance(noop_streak, int)
        and not isinstance(noop_streak, bool)
        and noop_streak >= 2
    )


def _close_target_large_enough(candidate: CandidateAction) -> bool:
    """拒绝内容区针尖假 X；那种点会点进影片并弹出尊享卡，不是关层。"""

    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return True
    try:
        width = float(raw.get("right") or 0.0) - float(raw.get("left") or 0.0)
        height = float(raw.get("bottom") or 0.0) - float(raw.get("top") or 0.0)
    except (TypeError, ValueError):
        return False
    # 真关闭控件在 1080 宽上通常 ≥ 约 16px；0.006 宽的像素噪声会点穿进播放器。
    return width >= 0.012 and height >= 0.008


def _popup_close_avoids_bottom_navigation_band(candidate: CandidateAction) -> bool:
    """关层点击不得落入底栏条带，避免误点「我的」跳进账号凭证。

    宫格／运营大图「正下方圆 X」常落在 y≈0.82–0.90（底栏上沿之上），
    不得按底栏禁区整段误杀；仅拒绝贴边底栏与右下「我的」。
    """

    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        return True
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return True
    try:
        top = float(raw.get("top") or 0.0)
        bottom = float(raw.get("bottom") or 0.0)
        left = float(raw.get("left") or 0.0)
        right = float(raw.get("right") or 0.0)
    except (TypeError, ValueError):
        return False
    cy = (top + bottom) / 2.0
    cx = (left + right) / 2.0
    role = str(candidate.metadata.get("control_role") or "").casefold()
    bottom_x = (
        candidate.metadata.get("popup_close_bottom_x_fallback") is True
        or candidate.metadata.get("forced_pixel_popup_close") is True
        or "下方" in str(candidate.semantic_target or "")
        or "圆x" in str(candidate.semantic_target or "").casefold()
        or "圆ｘ" in str(candidate.semantic_target or "")
    )
    # 中央下方圆 X：允许贴近底栏上沿，但不得偏到右下「我的」。
    if bottom_x or (role in {"dismiss", "skip"} and 0.32 <= cx <= 0.68 and cy >= 0.52):
        if cx >= 0.78 and cy >= 0.80:
            return False
        return cy < 0.93 and bottom <= 0.97
    # 其它关层：底栏约 y≥0.88；右下「我的」约 x≥0.82。
    if cy >= 0.86 or top >= 0.84:
        return False
    if cy >= 0.80 and cx >= 0.78:
        return False
    return True


def _dismiss_candidate_is_center_modal(candidate: CandidateAction) -> bool:
    """关层命中应像中部模态，拒绝底栏旁误标；明确 dismiss 角色放宽几何。"""

    role = str(candidate.metadata.get("control_role") or "").casefold()
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return role in {"dismiss", "skip"}
    try:
        left = float(raw.get("left") or 0.0)
        top = float(raw.get("top") or 0.0)
        right = float(raw.get("right") or 0.0)
        bottom = float(raw.get("bottom") or 0.0)
    except (TypeError, ValueError):
        return False
    if right <= left or bottom <= top:
        return False
    # 落在底栏条带的「关闭」几乎必是误标
    if top >= 0.88:
        return False
    # 视觉已标 dismiss／skip：任意非底栏区均可（含模态右上角 X）
    if role in {"dismiss", "skip"}:
        return True
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    area = (right - left) * (bottom - top)
    # 文本推断的关闭：要求大致落在中部模态区
    centerish = 0.18 <= cx <= 0.92 and 0.12 <= cy <= 0.90
    return centerish and area >= 0.0004


def _dismiss_candidate_is_corner_safe_close(candidate: CandidateAction) -> bool:
    """底栏未完时允许的关层：顶角小 X，或底部广告条关闭；禁止中部假 X 点穿。"""

    role = str(candidate.metadata.get("control_role") or "").casefold()
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("semantic_name") or ""),
        )
    ).casefold()
    skip_like = any(marker in terms for marker in ("跳过", "跳過", "skip"))
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        # 无坐标时仅放行明确 skip，避免中部假关闭。
        return skip_like
    try:
        left = float(raw.get("left") or 0.0)
        top = float(raw.get("top") or 0.0)
        right = float(raw.get("right") or 0.0)
        bottom = float(raw.get("bottom") or 0.0)
    except (TypeError, ValueError):
        return False
    if right <= left or bottom <= top:
        return False
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    area = (right - left) * (bottom - top)
    # 中部内容区（含水印／卡片中心）：一律拒绝。大图广告下方圆 X 可达 y≈0.55。
    if 0.28 < cy < 0.52:
        return False
    if area > 0.045:
        return False
    # 顶角／顶带跳过。
    if cy <= 0.28 and top <= 0.30 and (cx <= 0.30 or cx >= 0.70 or skip_like):
        return True
    # 底部广告条／圆钮 X（录屏海角下方 X；大图模态下 X 可略靠上）。
    if (
        0.52 <= cy <= 0.95
        and top >= 0.48
        and bottom <= 0.98
        and area <= 0.045
        and 0.22 <= cx <= 0.78
    ):
        return True
    if candidate.metadata.get("popup_close_bottom_x_fallback") is True and cy >= 0.52:
        return True
    return role in {"dismiss", "skip"} and (cy <= 0.28 or cy >= 0.52)


def _ordered_playbook_bottom_tabs(work: ExplorationWork) -> tuple[str, ...]:
    """课纲底栏顺序；优先产品 UI Tree 特征 JSON，否则 playbook／缺省。"""

    from services.api.app.application.app_capture_playbook import resolve_app_capture_playbook
    from services.api.app.application.app_ui_tree_features import (
        hjll_ordered_bottom_tab_labels,
        hlapp_ordered_bottom_tab_labels,
        ordered_bottom_tab_labels,
        resolve_ui_tree_playbook_id,
    )

    metadata = work.frontier.observation.metadata
    package_name = None
    apk_filename = None
    playbook_id = None
    if isinstance(metadata, Mapping):
        raw_pkg = metadata.get("package_name")
        if isinstance(raw_pkg, str) and raw_pkg.strip():
            package_name = raw_pkg.strip()
        progress = metadata.get("app_curriculum_progress")
        if isinstance(progress, Mapping):
            raw_apk = progress.get("apk_filename") or progress.get("original_filename")
            if isinstance(raw_apk, str) and raw_apk.strip():
                apk_filename = raw_apk.strip()
            raw_playbook = progress.get("playbook_id")
            if isinstance(raw_playbook, str) and raw_playbook.strip():
                playbook_id = raw_playbook.strip()
    resolved = resolve_ui_tree_playbook_id(
        playbook_id=playbook_id,
        package_name=package_name,
    )
    if resolved == "hjll":
        return hjll_ordered_bottom_tab_labels()
    if resolved == "hlapp":
        return hlapp_ordered_bottom_tab_labels()
    if resolved:
        return ordered_bottom_tab_labels(
            playbook_id=resolved,
            package_name=package_name,
        )
    playbook = resolve_app_capture_playbook(
        package_name=package_name,
        apk_filename=apk_filename,
    )
    if playbook is not None and playbook.tabs:
        return tuple(str(item).strip() for item in playbook.tabs if str(item).strip())
    return hjll_ordered_bottom_tab_labels()


def _short_drama_top_left_back_candidate(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> CandidateAction | None:
    """仅返回当前帧左上角条带内的真实返回候选；禁止无候选时合成坐标点击。

    F02：不得再造 ``forced-short-drama-top-left-back:*`` 并伪装 VISION。
    无视觉返回时由 ``_authorized_short_drama_exit_back`` 改用明确语义 BACK。
    """

    _ = work
    scored: list[tuple[int, CandidateAction]] = []
    for candidate in eligible:
        if candidate.risk_level not in {
            InteractionRiskLevel.LOW,
            InteractionRiskLevel.MEDIUM,
        }:
            continue
        if candidate.action_type is InteractionOperation.BACK:
            continue
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        # 禁止消费历史合成坐标候选。
        if str(candidate.candidate_id or "").startswith("forced-short-drama-top-left-back:"):
            continue
        terms = " ".join(
            (
                str(candidate.semantic_target or ""),
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ).casefold()
        looks_back = (
            any(
                marker in terms
                for marker in ("返回", "back", "上一页", "上一级", "<", "左上角")
            )
            or candidate.metadata.get("coverage_parent_restore") is True
            or candidate.metadata.get("short_drama_top_left_back") is True
        )
        if not looks_back:
            continue
        # 仅接受左上角条带内的视觉返回；中部「返回」文案／广告格禁止当返回键。
        raw = candidate.metadata.get("normalized_bounds")
        if not isinstance(raw, Mapping):
            continue
        try:
            left = float(raw.get("left"))
            top = float(raw.get("top"))
            right = float(raw.get("right"))
            bottom = float(raw.get("bottom"))
        except (TypeError, ValueError):
            continue
        if not (left <= 0.20 and top <= 0.16 and right <= 0.28 and bottom <= 0.18):
            continue
        score = 70
        scored.append((score, candidate))
    if not scored:
        return None
    chosen = max(scored, key=lambda item: (item[0], item[1].candidate_id))[1]
    return CandidateAction(
        candidate_id=chosen.candidate_id,
        semantic_target=chosen.semantic_target,
        action_type=chosen.action_type,
        risk_level=chosen.risk_level,
        exploration_value=max(chosen.exploration_value, 100),
        navigation_depth=chosen.navigation_depth,
        expected_surface_kind=chosen.expected_surface_kind,
        prior_failure_count=chosen.prior_failure_count,
        metadata={
            **dict(chosen.metadata),
            "forced_detail_exit_back": True,
            "post_detail_exit_prefer_tabs": True,
            "short_drama_top_left_back": True,
            "discovery_source_policy": "frame_verified_only",
        },
        protocol_version=chosen.protocol_version,
    )


def _authorized_short_drama_exit_back(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> CandidateAction:
    """短剧／会员退出：先真实左上角视觉返回，否则明确授权的语义 BACK。"""

    visual = _short_drama_top_left_back_candidate(work=work, eligible=eligible)
    if visual is not None:
        return visual
    for candidate in eligible:
        if candidate.action_type is not InteractionOperation.BACK:
            continue
        if candidate.risk_level not in {
            InteractionRiskLevel.LOW,
            InteractionRiskLevel.MEDIUM,
        }:
            continue
        return _with_post_detail_exit_prefer_tabs(candidate)
    return _with_post_detail_exit_prefer_tabs(_synthetic_system_back_candidate(work=work))


def _short_drama_playback_wait_intent(
    *,
    work: ExplorationWork,
    delay_ms: int,
) -> TimedObservationIntent:
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    phase_run_id = f"short-drama-playback:{work.task_id}"
    decision_id = f"wait-{delay_ms}-{capture_id}"
    return TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id=phase_run_id,
        decision_id=decision_id,
        observation_id=build_timed_observation_id(
            phase_run_id=phase_run_id,
            decision_id=decision_id,
            source_capture_id=capture_id,
        ),
        source_capture_id=capture_id,
        surface_kind=work.surface_kind,
        delay_ms=max(0, delay_ms),
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot",),
            inherit_parent_semantics=True,
        ),
        deadline=work.deadline,
        protocol_version=TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION,
    )


def _observation_frame_text_blob(
    *,
    work: ExplorationWork,
    metadata_now: Mapping[str, object],
) -> str:
    """拼当前帧 OCR／摘要／关键元素，供暗网落地／短剧结束态识别。"""

    observation = work.frontier.observation
    parts: list[str] = [
        str(observation.page_type or ""),
        str(observation.page_state or ""),
        str(observation.overlay_signature or ""),
        str(metadata_now.get("visual_summary") or ""),
        str(metadata_now.get("page_summary") or ""),
        str(metadata_now.get("ocr_text") or ""),
    ]
    parts.extend(str(item) for item in observation.key_elements)
    raw_keys = metadata_now.get("key_elements")
    if isinstance(raw_keys, (list, tuple)):
        parts.extend(str(item) for item in raw_keys)
    selected = metadata_now.get("selected_bottom_tab_labels")
    if isinstance(selected, (list, tuple)):
        parts.extend(str(item) for item in selected)
    return " ".join(parts)


def _preview_ended_paywall_surface(
    *,
    work: ExplorationWork,
    metadata_now: Mapping[str, object],
) -> bool:
    """本片试看结束／开通预售卡：左上角有「<」，不是挡路弹窗。"""

    blob = _observation_frame_text_blob(work=work, metadata_now=metadata_now)
    markers = (
        "试看结束",
        "試看結束",
        "本片试看结束",
        "本片試看結束",
        "开通预售卡",
        "開通預售卡",
        "开通预售免费看",
        "開通預售免費看",
        "全站视频免费看",
        "全站視頻免費看",
    )
    return any(marker in blob for marker in markers)


def _anwang_membership_landing_branch(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    tabs_hard_incomplete: bool,
    page_type_now: str,
    metadata_now: Mapping[str, object],
    visible_bottom: Sequence[CandidateAction],
) -> ActionIntent | TimedObservationIntent | None:
    """暗网会员解锁落地：留证后切课纲下一栏，禁止点「立即解锁」／系统 BACK。"""

    del page_type_now  # 页型常被标 payment；以文案／选中栏为准。
    if not tabs_hard_incomplete:
        return None
    frame_blob = _observation_frame_text_blob(work=work, metadata_now=metadata_now)
    selected_raw = metadata_now.get("selected_bottom_tab_labels")
    selected_labels: list[str] = []
    if isinstance(selected_raw, (list, tuple)):
        selected_labels = [str(item) for item in selected_raw if item]
    facts = metadata_now.get("surface_facts")
    if isinstance(facts, Mapping):
        pixel = facts.get("pixel_bottom_nav_calibration")
        if isinstance(pixel, Mapping):
            tabs = pixel.get("tabs")
            if isinstance(tabs, (list, tuple)):
                for tab in tabs:
                    if not isinstance(tab, Mapping):
                        continue
                    if tab.get("selected") is True:
                        selected_labels.append(str(tab.get("label") or ""))
    on_anwang_tab = any(is_anwang_bottom_tab_label(item) for item in selected_labels)
    if not (
        anwang_membership_landing_detected(frame_blob)
        or (on_anwang_tab and ("立即解锁" in frame_blob or "至尊金卡" in frame_blob))
        or (
            "立即解锁" in frame_blob
            and any(
                is_anwang_bottom_tab_label(candidate.semantic_target)
                for candidate in visible_bottom
            )
        )
    ):
        return None
    tab_pool = list(visible_bottom) or [
        candidate
        for candidate in eligible
        if candidate.metadata.get("bottom_navigation_tab_seek") is True
        and candidate.metadata.get("coverage_page_seek_contract") is None
        and _bottom_tab_bounds_in_safe_band(candidate, relax=True)
    ]
    next_tab = _pick_next_playbook_bottom_tab(
        work=work,
        visible_bottom=tab_pool,
    )
    if next_tab is not None:
        next_tab = CandidateAction(
            candidate_id=next_tab.candidate_id,
            semantic_target=next_tab.semantic_target,
            action_type=next_tab.action_type,
            risk_level=next_tab.risk_level,
            exploration_value=max(next_tab.exploration_value, 100),
            navigation_depth=next_tab.navigation_depth,
            expected_surface_kind=next_tab.expected_surface_kind,
            prior_failure_count=next_tab.prior_failure_count,
            metadata={
                **dict(next_tab.metadata),
                "post_detail_exit_prefer_tabs": True,
                "anwang_membership_landing_exit": True,
            },
            protocol_version=next_tab.protocol_version,
        )
        return _build_action_intent(
            work=work,
            candidate=next_tab,
            purpose=(
                "暗网会员／立即解锁落地页已留证；禁止点解锁 CTA，"
                "按课纲切下一栏（AI／社区／我的）。"
            ),
        )
    wait = _observe_wait_for_next_playbook_tab(work=work, eligible=eligible)
    if wait is not None:
        return wait
    return None


def _short_drama_exit_context_active(
    *,
    work: ExplorationWork,
    metadata_now: Mapping[str, object],
    text_blob: str = "",
) -> bool:
    """是否处于短剧退出上下文（有 pending／播完信号／沉浸铬条），避免误劫持普通详情页。"""

    if short_drama_exit_pending(metadata_now) or short_drama_end_seen(metadata_now):
        return True
    if short_drama_playback_started_at(metadata_now) is not None:
        return True
    blob = text_blob or _observation_frame_text_blob(
        work=work, metadata_now=metadata_now
    )
    if short_drama_immersive_player_chrome(blob):
        return True
    if short_drama_playback_end_detected(blob):
        return True
    return False


def _short_drama_playback_exit_branch(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    tabs_hard_incomplete: bool,
    page_type_now: str,
    metadata_now: Mapping[str, object],
    now: datetime | None = None,
) -> ActionIntent | TimedObservationIntent | None:
    """短剧退出：沉浸态见左上角「<」即返回；Tab 内嵌无箭头则切下一栏。"""

    del now  # 退出闸门不再读墙钟；保留参数兼容调用方。
    from services.api.app.application.app_coverage_page_seek import (
        home_top_category_sample_in_progress,
        home_top_category_tabs_incomplete,
    )

    in_tab_preview = short_drama_bottom_nav_visible_on_frame(
        coverage_metadata=metadata_now,
        candidates=eligible,
        key_elements=work.frontier.observation.key_elements,
    )
    preview_blob = _observation_frame_text_blob(
        work=work, metadata_now=metadata_now
    ).casefold()
    embedded_player = in_tab_preview and any(
        marker in preview_blob for marker in SHORT_DRAMA_IN_TAB_TOP_MARKERS
    )
    # 上方分类课纲／第一片取证未完：回主页续国产…。
    # 例外：已在短剧 Tab 内嵌播放器（顶栏直播／AI短剧 + 底栏可见）须切暗网。
    # 例外：已进入短剧退出上下文（pending／播完／沉浸铬条）须先退出播放器。
    if home_top_category_sample_in_progress(metadata_now) and not embedded_player:
        return None
    pkg = metadata_now.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    short_drama_ctx = _short_drama_exit_context_active(
        work=work,
        metadata_now=metadata_now,
        text_blob=preview_blob,
    )
    if (
        not embedded_player
        and not short_drama_ctx
        and home_top_category_tabs_incomplete(
            coverage_metadata=metadata_now,
            package_name=package_name,
        )
    ):
        return None
    # 暗网会员落地（同标 payment）：不得走短剧左上角退出，交给暗网切栏支。
    if anwang_membership_landing_detected(
        _observation_frame_text_blob(work=work, metadata_now=metadata_now)
    ):
        return None
    # 仅真实弹层态才让路关层；流程契约 popup_satisfied=false 不得阻断播放器退出
    # （197bac3c：短剧点完后 Deferred 空转，无法左上角返回／切下一栏）。
    page_state_now = str(work.frontier.observation.page_state or "").casefold()
    if page_state_now in {
        "popup",
        "modal",
        "dialog",
        "system_dialog",
        "startup_ad",
    }:
        return None
    facts = metadata_now.get("surface_facts")
    if isinstance(facts, Mapping):
        dialog_kind = str(facts.get("dialog_kind") or "").casefold()
        if dialog_kind in {
            "ad_popup",
            "promo_popup",
            "promo_ad",
            "modal",
            "dialog",
            "system_dialog",
        }:
            return None
        topmost = facts.get("topmost_ad_popup")
        if isinstance(topmost, Mapping) and topmost:
            return None

    text_parts: list[str] = [
        str(work.frontier.observation.page_type or ""),
        str(work.frontier.observation.page_state or ""),
        str(work.frontier.observation.overlay_signature or ""),
        str(metadata_now.get("visual_summary") or ""),
        str(metadata_now.get("page_summary") or ""),
        str(metadata_now.get("ocr_text") or ""),
    ]
    text_parts.extend(str(item) for item in work.frontier.observation.key_elements)
    raw_keys = metadata_now.get("key_elements")
    if isinstance(raw_keys, (list, tuple)):
        text_parts.extend(str(item) for item in raw_keys)
    # payment 页型本身即解锁／套餐层；OCR 再认「更多精彩解锁即享／金币解锁」等。
    # Tab 内嵌三形态：①未完待续 ②进度贴尾+中间播放钮 ③解锁层；
    # 进度贴尾或轮询多次仍无解锁层 → 视为可切下一栏，禁止永远卡在「等解锁」。
    # 粘性 end_seen：解锁→返回→又见底栏后，即使进度又是 02:xx 也须立刻切栏，禁止空等再进播。
    text_blob = " ".join(text_parts)
    # 与暗网落地支统一拼帧，补 selected_bottom_tab_labels。
    text_blob = f"{text_blob} {_observation_frame_text_blob(work=work, metadata_now=metadata_now)}"
    in_tab_form = short_drama_bottom_nav_visible_on_frame(
        coverage_metadata=metadata_now,
        candidates=eligible,
        key_elements=work.frontier.observation.key_elements,
    )
    # 播放器铬条已见且底栏标签不足 3：视为沉浸，忽略上一帧残留 tab_seek。
    if (
        short_drama_immersive_player_chrome(text_blob)
        and short_drama_bottom_nav_label_hits(text_blob) < 3
    ):
        in_tab_form = False
    arrow_visible = short_drama_top_left_back_arrow_visible(
        coverage_metadata=metadata_now,
        candidates=eligible,
        key_elements=work.frontier.observation.key_elements,
    )
    # 沉浸播放器：仅短剧上下文下，左上角「<」出现或播放器页型即返回，
    # 不等待播完、不依赖 pending。无短剧信号时禁止把普通详情／系统 BACK 候选当短剧退出。
    if (
        not in_tab_form
        and short_drama_ctx
        and (arrow_visible or page_type_now in SHORT_DRAMA_PLAYER_PAGE_TYPES)
    ):
        back = _authorized_short_drama_exit_back(work=work, eligible=eligible)
        purpose = (
            "左上角返回箭头已出现，立即返回，禁止空等播放。"
            if back.action_type is not InteractionOperation.BACK
            else "短剧沉浸页无左上角视觉返回；使用授权语义 BACK，禁止合成坐标。"
        )
        return _build_action_intent(
            work=work,
            candidate=back,
            purpose=purpose,
        )
    if not tabs_hard_incomplete:
        return None
    ended_sticky = short_drama_end_seen(metadata_now)
    force_embedded_advance = in_tab_form and (
        embedded_player
        or any(marker in text_blob.casefold() for marker in SHORT_DRAMA_IN_TAB_TOP_MARKERS)
    )
    # Tab 内嵌（顶栏直播／AI短剧 + 底栏可见）：播放中即可切下一栏，不等待播完。
    if not force_embedded_advance:
        if not short_drama_exit_pending(metadata_now) and not ended_sticky:
            return None
        # 已见解锁／播完后回到非播放器页型（短剧 Tab 根）：仍允许切栏。
        if page_type_now not in SHORT_DRAMA_PLAYER_PAGE_TYPES:
            if not (ended_sticky and in_tab_form):
                return None
    observe_count = short_drama_playback_observe_count(metadata_now)
    end_seen = short_drama_in_tab_finished_for_advance(
        text_blob,
        observe_count=observe_count,
        page_type=page_type_now,
        coverage_metadata=metadata_now,
    ) if in_tab_form else (
        ended_sticky
        or page_type_now == "payment"
        or short_drama_playback_end_detected(text_blob)
    )
    if ended_sticky:
        end_seen = True
    if force_embedded_advance:
        end_seen = True

    if not end_seen:
        # 未见解锁层／未贴尾／未见未完待续：短间隔重采，禁止抢点下一栏。
        return _short_drama_playback_wait_intent(
            work=work,
            delay_ms=SHORT_DRAMA_UNLOCK_POLL_MS,
        )

    # 形态分流：底栏可见（Tab 内嵌）→ 强制切下一栏；仅沉浸态点左上角。
    # 播完／贴尾／查看完整短剧／中间播放／粘性已见解锁时禁止因误认返回箭头空点 Logo。
    use_top_left = short_drama_should_tap_top_left_back(
        coverage_metadata=metadata_now,
        candidates=eligible,
        key_elements=work.frontier.observation.key_elements,
    )
    if in_tab_form and end_seen:
        use_top_left = False
    if not use_top_left:
        tab_pool = [
            candidate
            for candidate in eligible
            if candidate.metadata.get("bottom_navigation_tab_seek") is True
            and candidate.metadata.get("coverage_page_seek_contract") is None
            and _bottom_tab_bounds_in_safe_band(candidate, relax=True)
            and not short_drama_reentry_candidate_blocked(
                candidate,
                coverage_metadata=metadata_now,
            )
        ]
        next_tab = _pick_next_playbook_bottom_tab(
            work=work,
            visible_bottom=tab_pool,
        )
        if next_tab is not None:
            if ended_sticky and not short_drama_playback_progress_near_end(text_blob):
                purpose = (
                    "短剧已见解锁／播完并回到底栏可见态；"
                    "禁止再点查看完整／播放，直接切课纲下一栏（暗网）。"
                )
            elif "未完待续" in text_blob or "未完待續" in text_blob:
                purpose = (
                    "短剧 Tab 内嵌形态①：片尾「未完待续」；"
                    "底栏可见，直接切课纲下一栏（暗网），禁止点解锁／重播。"
                )
            elif short_drama_playback_progress_near_end(
                text_blob
            ) or short_drama_center_replay_control_detected(text_blob):
                purpose = (
                    "短剧 Tab 内嵌形态②：进度贴尾／中央播放钮（本集已播完）；"
                    "底栏可见，直接切课纲下一栏（暗网），禁止空点左上角或永久等解锁。"
                )
            elif short_drama_playback_end_detected(text_blob) or page_type_now == "payment":
                purpose = (
                    "短剧 Tab 内嵌形态③：解锁／付费层／「查看完整短剧」已见；"
                    "无左上角返回箭头、底栏可见，直接切课纲下一栏，禁止点金币解锁。"
                )
            else:
                purpose = (
                    "短剧为 Tab 内嵌形态；轮询解锁层多次仍未出现，底栏已见课纲下一栏，"
                    "切栏继续（禁止永久空等）。"
                )
            next_tab = CandidateAction(
                candidate_id=next_tab.candidate_id,
                semantic_target=next_tab.semantic_target,
                action_type=next_tab.action_type,
                risk_level=next_tab.risk_level,
                exploration_value=max(next_tab.exploration_value, 100),
                navigation_depth=next_tab.navigation_depth,
                expected_surface_kind=next_tab.expected_surface_kind,
                prior_failure_count=next_tab.prior_failure_count,
                metadata={
                    **dict(next_tab.metadata),
                    "purpose": purpose,
                    "post_detail_exit_prefer_tabs": True,
                    "short_drama_in_tab_form_exit": True,
                    "short_drama_playback_finished_exit": True,
                },
                protocol_version=next_tab.protocol_version,
            )
            return _build_action_intent(
                work=work,
                candidate=next_tab,
                purpose=purpose,
            )
        # 无下一栏候选时不合成左上角，交给其它支路，避免点到 Logo。
        return None

    # 左上角多次落点仍停播放器：若同帧可点课纲下一栏则切栏，避免空转。
    # 内嵌底栏已见时阈值降为 1，避免空点撞 repeated_page_limit。
    top_left_attempts = short_drama_top_left_back_attempt_count(metadata_now)
    top_left_before_tab = (
        SHORT_DRAMA_IN_TAB_TOP_LEFT_BEFORE_TAB_ADVANCE
        if in_tab_form
        else SHORT_DRAMA_TOP_LEFT_BACK_BEFORE_TAB_ADVANCE
    )
    if top_left_attempts >= top_left_before_tab:
        tab_pool = [
            candidate
            for candidate in eligible
            if candidate.metadata.get("bottom_navigation_tab_seek") is True
            and candidate.metadata.get("coverage_page_seek_contract") is None
            and _bottom_tab_bounds_in_safe_band(candidate, relax=True)
        ]
        next_tab = _pick_next_playbook_bottom_tab(
            work=work,
            visible_bottom=tab_pool,
        )
        if next_tab is not None:
            return _build_action_intent(
                work=work,
                candidate=next_tab,
                purpose=(
                    "短剧左上角返回多次落点仍停播放器；底栏已见课纲下一栏，"
                    "切栏继续（主页只截不点）。"
                ),
            )
        # 仍无底栏：payment／播放器吞左上角时改系统 BACK，禁止继续空点左上角。
        if page_type_now == "payment" or top_left_attempts >= (
            top_left_before_tab + 2
        ):
            system_back = next(
                (
                    candidate
                    for candidate in eligible
                    if candidate.action_type is InteractionOperation.BACK
                    and candidate.risk_level
                    in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
                ),
                None,
            )
            if system_back is None:
                system_back = _synthetic_system_back_candidate(work=work)
            system_back = _with_post_detail_exit_prefer_tabs(system_back)
            return _build_action_intent(
                work=work,
                candidate=system_back,
                purpose=(
                    "短剧解锁层左上角多次无效；改系统 BACK 离开后再巡底栏。"
                ),
            )

    back = _authorized_short_drama_exit_back(work=work, eligible=eligible)
    purpose = (
        "短剧沉浸播放器左上角返回箭头已出现；立即点击返回回根后再按课纲切下一栏。"
        if back.action_type is not InteractionOperation.BACK
        else "短剧沉浸播放器无左上角视觉返回；使用授权语义 BACK 回根，禁止合成坐标点击。"
    )
    return _build_action_intent(
        work=work,
        candidate=back,
        purpose=purpose,
    )


def _pick_next_playbook_bottom_tab(
    *,
    work: ExplorationWork,
    visible_bottom: Sequence[CandidateAction],
) -> CandidateAction | None:
    """按课纲取下一未访栏；UI Tree 未命中时与 promote 对齐做像素／语义兜底。

    禁止插值猜点其它栏；仅放行课纲下一栏的真实条带或同帧像素校准槽。
    """

    if _uses_dynamic_app_frontier(work):
        return None
    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        _filter_tabs_to_next_playbook_only,
        _next_unvisited_playbook_bottom_tab_label,
        _normalize_bottom_tab_label,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )

    metadata = work.frontier.observation.metadata
    visited_raw: Sequence[object] = ()
    if isinstance(metadata, Mapping):
        raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
        if isinstance(raw, (list, tuple)):
            visited_raw = raw
    visited = {
        _normalize_bottom_tab_label(item)
        for item in visited_raw
        if isinstance(item, str) and _normalize_bottom_tab_label(item)
    }
    page_type = str(work.frontier.observation.page_type or "")
    next_label = _next_unvisited_playbook_bottom_tab_label(
        visited_labels=visited,
        ordered_labels=_ordered_playbook_bottom_tabs(work),
        page_type=page_type,
    )
    from services.api.app.application.app_coverage_page_seek import (
        _prefer_anwang_after_short_drama,
    )

    next_label = _prefer_anwang_after_short_drama(
        next_label=next_label,
        visited_labels=visited,
        ordered_labels=_ordered_playbook_bottom_tabs(work),
        coverage_metadata=metadata if isinstance(metadata, Mapping) else None,
    )
    if not next_label:
        return None
    # HJLL 底栏沿用录制脚本 y≈0.90 的固定安全点。仅在当前帧已确认底栏可见
    # 时签发，防止详情页盲点；一旦签发便不再被通用 UI Tree／OCR 候选改写。
    if visible_bottom and _is_hjll_airtest_work(work):
        from services.api.app.application.app_ui_tree_features import (
            bottom_navigation_tab_tap_bounds,
        )

        fixed_bounds = bottom_navigation_tab_tap_bounds(
            next_label,
            package_name=_hjll_expected_package(work),
        )
        if fixed_bounds is not None:
            capture_id = str(
                work.source_capture_id
                or work.frontier.observation.capture_id
                or "unknown"
            )
            return CandidateAction(
                candidate_id=f"hjll-orchestrated-bottom-tab:{capture_id}:{next_label}",
                semantic_target=f"HJLL 底栏「{next_label}」",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=work.surface_kind,
                metadata={
                    "purpose": f"按 HJLL 录制脚本固定落点点击底栏「{next_label}」",
                    "frontier_scope": "app",
                    "bottom_navigation_tab_seek": True,
                    "coverage_page_seek": True,
                    "bottom_navigation_tab_fixed_airtest": True,
                    "hjll_product_orchestrated_point": True,
                    "require_screenshot_evidence": True,
                    "expected_state_hint": f"tab:{next_label}",
                    "normalized_bounds": dict(fixed_bounds),
                },
            )
    if visible_bottom:
        matched = _filter_tabs_to_next_playbook_only(
            list(visible_bottom),
            next_label=next_label,
        )
        if matched:
            return min(matched, key=_bottom_navigation_tab_selection_key)
        # 与 coverage promote 像素兜底对齐：语义／purpose 已是下一栏则放行。
        alias_hits = [
            tab
            for tab in visible_bottom
            if bottom_tab_label_already_visited(tab.semantic_target, {next_label})
            or next_label in str(tab.metadata.get("purpose") or "")
            or tab.metadata.get("bottom_nav_pixel_visual") is True
            and bottom_tab_label_already_visited(
                str(tab.metadata.get("expected_state_hint") or "").removeprefix("tab:"),
                {next_label},
            )
        ]
        if alias_hits:
            return min(alias_hits, key=_bottom_navigation_tab_selection_key)
    # seek 池空或过滤未命中：同帧像素校准已有下一栏则合成可点候选。
    pixel = _synthetic_pixel_playbook_bottom_tab(work=work, next_label=next_label)
    if pixel is not None:
        return pixel
    # HJLL 的 Airtest 脚本把五个底栏都固定在 y≈0.90，避开 Android 手势带。
    # 仅当本帧已经看见底栏但其文字/像素未能重绑时才使用产品实测框；
    # 看不见底栏时继续观察，绝不插值盲点。
    if not visible_bottom:
        return None
    from services.api.app.application.app_ui_tree_features import (
        bottom_navigation_tab_tap_bounds,
    )

    package_name = None
    if isinstance(metadata, Mapping):
        raw_package = metadata.get("package_name")
        if isinstance(raw_package, str) and raw_package.strip():
            package_name = raw_package.strip()
    bounds = bottom_navigation_tab_tap_bounds(
        next_label,
        package_name=package_name,
    )
    if bounds is None:
        return None
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    return CandidateAction(
        # 沿用 Runtime 已支持的合成底栏候选前缀；实际落点仍由 Airtest 框限定。
        candidate_id=f"forced-pixel-bottom-nav:{capture_id}:{next_label}",
        semantic_target=f"底栏「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": f"按 HJLL Airtest 安全落点点击底栏「{next_label}」并截图留证",
            "frontier_scope": "app",
            "bottom_navigation_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "bottom_nav_airtest_fallback": True,
            "expected_state_hint": f"tab:{next_label}",
            "normalized_bounds": bounds,
        },
    )


def _fallback_visible_bottom_tab_while_yielding(
    *,
    work: ExplorationWork,
    visible_bottom: Sequence[CandidateAction],
) -> CandidateAction | None:
    """清障让路后课纲下一栏未命中时的底栏兜底。

    优先课纲下一未访栏；含「我的」的课纲禁止再从 unvisited 全集 min() 跳栏。
    无「我的」课纲的产品保留原未访栏放行，避免详情页可见底栏时空转。
    """

    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        _normalize_bottom_tab_label,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )

    chosen = _pick_next_playbook_bottom_tab(
        work=work,
        visible_bottom=visible_bottom,
    )
    if chosen is not None:
        return chosen
    ordered = _ordered_playbook_bottom_tabs(work)
    if any(_normalize_bottom_tab_label(item) == "我的" for item in ordered):
        return None
    if not visible_bottom:
        return None
    metadata = work.frontier.observation.metadata
    visited: set[str] = set()
    if isinstance(metadata, Mapping):
        raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
        if isinstance(raw, (list, tuple)):
            visited = {
                _normalize_bottom_tab_label(item)
                for item in raw
                if isinstance(item, str) and _normalize_bottom_tab_label(item)
            }
    unvisited = [
        tab
        for tab in visible_bottom
        if not bottom_tab_label_already_visited(tab.semantic_target, visited)
    ]
    pool = unvisited or list(visible_bottom)
    return min(pool, key=_bottom_navigation_tab_selection_key)


def _synthetic_pixel_playbook_bottom_tab(
    *,
    work: ExplorationWork,
    next_label: str,
) -> CandidateAction | None:
    """本帧像素底栏已检出课纲下一栏时，合成 forced-pixel-bottom-nav 点击。"""

    from services.api.app.application.app_exploration_flow_contract import (
        popup_phase_blocks_bottom_tabs,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )
    from services.api.app.application.app_visual_bottom_nav_calibration import (
        extract_pixel_bottom_nav_tabs,
    )

    metadata = work.frontier.observation.metadata
    if not isinstance(metadata, Mapping):
        return None
    if popup_phase_blocks_bottom_tabs(metadata):
        return None
    observation = work.frontier.observation
    page_state = str(getattr(observation, "page_state", "") or "").strip().casefold()
    page_type = str(getattr(observation, "page_type", "") or "").strip().casefold()
    # 与 coverage seek 对齐：开屏／弹层当前帧禁止合成底栏（契约 popup_seen 可滞后）
    if page_type in {"startup_ad", "splash", "countdown"} or page_state in {
        "startup_ad",
        "splash",
        "countdown",
        "popup",
        "modal",
        "overlay",
        "dialog",
    }:
        return None
    facts = metadata.get("surface_facts")
    tabs = extract_pixel_bottom_nav_tabs(facts if isinstance(facts, Mapping) else None)
    if not tabs and isinstance(facts, Mapping):
        # 部分帧把校准挂在 surface_facts 外层副本。
        tabs = extract_pixel_bottom_nav_tabs({"surface_facts": facts})
    hit = next(
        (
            tab
            for tab in tabs
            if bottom_tab_label_already_visited(tab.label, {next_label})
        ),
        None,
    )
    if hit is None:
        return None
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    bounds = {
        "left": float(hit.bounds.left),
        "top": float(hit.bounds.top),
        "right": float(hit.bounds.right),
        "bottom": float(hit.bounds.bottom),
    }
    purpose = (
        f"点击底栏「{next_label}」并截图留证；当前截图视觉像素底栏校准"
    )
    return CandidateAction(
        candidate_id=f"forced-pixel-bottom-nav:{capture_id}:{next_label}",
        semantic_target=f"底栏「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "app",
            "bottom_navigation_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "bottom_nav_pixel_visual": True,
            "forced_pixel_bottom_nav": True,
            "expected_state_hint": f"tab:{next_label}",
            "normalized_bounds": bounds,
        },
    )


def _is_profile_credential_skip_candidate(candidate: CandidateAction) -> bool:
    """账号凭证「我已保存」不得当作普通关层 X 点击（须走凭证第二步支路）。"""

    from services.api.app.application.app_ui_tree_features import (
        candidate_matches_profile_credential_skip,
    )

    if candidate.metadata.get("profile_credential_save") is True:
        return False
    if candidate.metadata.get("profile_credential_dismiss") is True:
        return True
    return candidate_matches_profile_credential_skip(candidate)


def _profile_credential_save_phase_done(work: ExplorationWork) -> bool:
    """是否已完成「保存凭证」或同帧已见保存成功 toast。"""

    from services.api.app.application.app_semantic_progress import (
        PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY,
        PROFILE_CREDENTIAL_SAVE_DONE_KEY,
    )
    from services.api.app.application.app_ui_tree_features import (
        profile_credential_save_phase_ready,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    raw_attempts = metadata.get(PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY)
    attempts = (
        raw_attempts
        if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool)
        else 0
    )
    facts = metadata.get("surface_facts")
    blob = " ".join(
        str(part or "")
        for part in (
            metadata.get("visual_summary"),
            metadata.get("ocr_text"),
            metadata.get("page_summary"),
            observation.page_type,
            observation.page_state,
            facts.get("dialog_kind") if isinstance(facts, Mapping) else None,
            " ".join(str(item) for item in (observation.key_elements or ())),
        )
    )
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    return profile_credential_save_phase_ready(
        save_done_flag=metadata.get(PROFILE_CREDENTIAL_SAVE_DONE_KEY) is True,
        save_attempt_count=attempts,
        observation_text=blob,
        package_name=package_name,
    )


def _work_shows_profile_credential(work: ExplorationWork) -> bool:
    """当前帧是否为账号凭证弹层（OCR／摘要／候选文案／已访我的+弹层）。"""

    from services.api.app.application.app_semantic_progress import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        bottom_tab_label_already_visited,
    )
    from services.api.app.application.app_ui_tree_features import (
        observation_matches_profile_credential,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    facts = metadata.get("surface_facts")
    visited_raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    visited: set[str] = set()
    if isinstance(visited_raw, (list, tuple, set, frozenset)):
        visited = {str(item) for item in visited_raw if str(item or "").strip()}
    profile_tab_visited = bottom_tab_label_already_visited("我的", visited)
    # 禁止把候选池文案拼进匹配（Vision 幻觉「保存凭证／我已保存」会污染主页判定）。
    blob = " ".join(
        str(part or "")
        for part in (
            metadata.get("visual_summary"),
            metadata.get("ocr_text"),
            metadata.get("page_summary"),
            observation.page_type,
            observation.page_state,
            facts.get("dialog_kind") if isinstance(facts, Mapping) else None,
            " ".join(str(item) for item in (observation.key_elements or ())),
        )
    )
    return observation_matches_profile_credential(
        page_type=observation.page_type,
        page_state=observation.page_state,
        observation_text=blob,
        package_name=package_name,
        profile_tab_visited=profile_tab_visited,
    )


def _raw_profile_credential_markers_on_screen(work: ExplorationWork) -> bool:
    """是否看到账号凭证强文案（含误进「我的」、尚未记入已访）。"""

    from services.api.app.application.app_ui_tree_features import (
        observation_shows_raw_profile_credential_markers,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = metadata.get("surface_facts")
    blob = " ".join(
        str(part or "")
        for part in (
            metadata.get("visual_summary"),
            metadata.get("ocr_text"),
            metadata.get("page_summary"),
            observation.page_type,
            observation.page_state,
            facts.get("dialog_kind") if isinstance(facts, Mapping) else None,
            " ".join(str(item) for item in (observation.key_elements or ())),
        )
    )
    return observation_shows_raw_profile_credential_markers(blob)


def _profile_credential_curriculum_ready(work: ExplorationWork) -> bool:
    """课纲允许处理账号凭证：须已访「我的」，或上方完且下一栏就是「我的」。"""

    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        _next_unvisited_playbook_bottom_tab_label,
        _normalize_bottom_tab_label,
        home_top_category_tabs_incomplete,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    visited_raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    visited: set[str] = set()
    if isinstance(visited_raw, (list, tuple, set, frozenset)):
        visited = {
            _normalize_bottom_tab_label(item)
            for item in visited_raw
            if isinstance(item, str) and _normalize_bottom_tab_label(item)
        }
    if bottom_tab_label_already_visited("我的", visited):
        return True
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    if home_top_category_tabs_incomplete(
        coverage_metadata=metadata,
        package_name=package_name,
    ):
        return False
    next_label = _next_unvisited_playbook_bottom_tab_label(
        visited_labels=visited,
        ordered_labels=_ordered_playbook_bottom_tabs(work),
        page_type=str(observation.page_type or ""),
    )
    return _normalize_bottom_tab_label(next_label or "") == "我的"


def _escape_accidental_profile_credential_intent(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> ActionIntent | None:
    """误进「我的」+凭证弹层且课纲未到：点回主页，禁止保存凭证收口。"""

    from services.api.app.application.app_coverage_page_seek import (
        _normalize_bottom_tab_label,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )

    for candidate in eligible:
        if candidate.metadata.get("bottom_navigation_tab_seek") is not True:
            continue
        label = _normalize_bottom_tab_label(candidate.semantic_target)
        if bottom_tab_label_already_visited(label, {"主页", "首页"}):
            return _build_action_intent(
                work=work,
                candidate=candidate,
                purpose=(
                    "误入个人中心账号凭证且上方／底栏课纲未到「我的」；"
                    "点击底栏「主页」离开，禁止保存凭证提前收口。"
                ),
            )
    # 池内无主页：系统返回离开个人中心。
    back = next(
        (
            candidate
            for candidate in eligible
            if candidate.action_type is InteractionOperation.BACK
        ),
        None,
    )
    if back is not None:
        return _build_action_intent(
            work=work,
            candidate=back,
            purpose=(
                "误入个人中心账号凭证且课纲未到「我的」；系统返回离开，"
                "回到主页后再跑上方分类。"
            ),
        )
    return None


def _synthetic_profile_credential_save_candidate(
    *,
    work: ExplorationWork,
) -> CandidateAction | None:
    """池内无保存钮时按 UI Tree 坐标合成「保存凭证」。"""

    from services.api.app.application.app_synthetic_touch_contracts import (
        profile_credential_save_contract,
    )
    from services.api.app.application.app_ui_tree_features import (
        profile_credential_save_bounds,
    )
    from services.api.app.domain.exploration import NormalizedBounds

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    preferred = profile_credential_save_bounds(package_name=package_name)
    if preferred is None:
        return None
    zone_name, raw_bounds = preferred
    bounds = NormalizedBounds.from_dict(raw_bounds)
    purpose = (
        f"点账号凭证弹层蓝色「保存凭证」（ui_tree:{zone_name}）；"
        "保存成功后再点「我已保存」"
    )
    contract = profile_credential_save_contract(bounds=bounds, purpose=purpose)
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    )
    return CandidateAction(
        candidate_id=f"forced-profile-credential-save:{capture_id}",
        semantic_target="保存凭证",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "source_capture_id": capture_id,
            "control_role": "profile_credential_confirm",
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "save_profile_credential",
            "normalized_bounds": bounds.to_dict(),
            "profile_credential_save": True,
            "profile_credential_save_contract": contract,
        },
    )


def _synthetic_profile_credential_dismiss_candidate(
    *,
    work: ExplorationWork,
) -> CandidateAction | None:
    """池内无「我已保存」时按 UI Tree 坐标合成关闭。"""

    from services.api.app.application.app_synthetic_touch_contracts import (
        profile_credential_dismiss_contract,
    )
    from services.api.app.application.app_ui_tree_features import (
        profile_credential_skip_bounds,
    )
    from services.api.app.domain.exploration import NormalizedBounds

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    preferred = profile_credential_skip_bounds(package_name=package_name)
    if preferred is None:
        return None
    zone_name, raw_bounds = preferred
    bounds = NormalizedBounds.from_dict(raw_bounds)
    purpose = f"账号凭证已保存；点灰色「我已保存」（ui_tree:{zone_name}）关闭弹层"
    contract = profile_credential_dismiss_contract(bounds=bounds, purpose=purpose)
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    )
    return CandidateAction(
        candidate_id=f"forced-profile-credential-dismiss:{capture_id}",
        semantic_target="我已保存",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "source_capture_id": capture_id,
            "control_role": "profile_credential_dismiss",
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "dismiss_profile_credential",
            "normalized_bounds": bounds.to_dict(),
            "profile_credential_dismiss": True,
            "profile_credential_dismiss_contract": contract,
        },
    )


def _hjll_popup_attempt_limit_reached(work: ExplorationWork) -> bool:
    """同层尝试与连续无效动作任一达到既有八轮上限即停止关层。"""
    metadata = work.frontier.observation.metadata
    return any(
        isinstance(count, int) and not isinstance(count, bool) and count >= 8
        for count in (
            metadata.get("surface_phase_same_popup_attempt_count"),
            metadata.get("dismiss_noop_streak"),
        )
    )


def _synthetic_pixel_popup_close_candidate(
    *,
    work: ExplorationWork,
) -> CandidateAction | None:
    """同帧已有像素圆 X／角 X 校准时，合成 forced-pixel-popup-close，禁止空观察。"""

    from services.api.app.application.app_synthetic_touch_contracts import (
        popup_close_corner_contract,
    )
    from services.api.app.domain.exploration import NormalizedBounds

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        return None
    calibrated = facts.get("pixel_cross_close_calibration")
    if not isinstance(calibrated, Mapping):
        return None
    raw_bounds = calibrated.get("normalized_bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        left = float(raw_bounds["left"])
        top = float(raw_bounds["top"])
        right = float(raw_bounds["right"])
        bottom = float(raw_bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    # 校准层已按「相对弹层面板」验收；此处信任 close_zone／close_kind，
    # 禁止再用固定屏幕 Y 二次误杀其它位置的真 X。
    close_kind = str(calibrated.get("close_kind") or "").strip()
    close_zone = str(calibrated.get("close_zone") or "").strip()
    panel_zones = {
        "panel_top_right",
        "panel_top_left",
        "panel_below",
        "panel_bottom_edge",
    }
    screen_zones = {
        "screen_top_corner",
        "screen_top_right_label",
        "screen_bottom_banner",
    }
    trusted_kinds = {
        "bottom_circular_x",
        "circular_x",
        "corner_x",
        "panel_corner_x",
        "top_right_close_label",
        "startup_skip_or_close",
    }
    zone_ok = close_zone in panel_zones or close_zone in screen_zones
    kind_ok = close_kind in trusted_kinds
    # 兼容旧帧：无 close_zone 时才弱回落绝对分区。
    if not zone_ok and not kind_ok:
        top_corner_ok = cy <= 0.28 and top <= 0.30 and (cx <= 0.28 or cx >= 0.72)
        bottom_banner_ok = (
            0.52 <= cy <= 0.90
            and top >= 0.45
            and bottom <= 0.96
            and 0.22 <= cx <= 0.96
        )
        if not (top_corner_ok or bottom_banner_ok):
            return None
    is_hjll = _is_hjll_airtest_work(work)
    if is_hjll and _hjll_popup_attempt_limit_reached(work):
        return None
    if is_hjll and close_kind in {"bottom_circular_x", "circular_x"} and not (
        0.42 <= cx <= 0.58 and 0.66 <= cy <= 0.90
    ):
        # HJLL 中部／右下广告常伪装 X；只接受中央下方圆 X。
        return None
    bottom_x = (
        close_zone in {"panel_below", "panel_bottom_edge", "screen_bottom_banner"}
        or close_kind == "bottom_circular_x"
        or (0.52 <= cy <= 0.90 and top >= 0.45)
    )
    capture_id = str(
        work.source_capture_id or observation.capture_id or "unknown"
    )
    bounds = NormalizedBounds(left, top, right, bottom)
    if bottom_x:
        semantic_target = "关闭弹层下方X"
        purpose = (
            "像素检出弹层下方圆 X；立即点击关闭并截图，禁止只观察不点"
        )
    else:
        semantic_target = "关闭弹层右上X"
        purpose = "像素检出弹层关闭控件；立即点击关闭并截图，禁止只观察不点"
    contract = popup_close_corner_contract(
        bounds=bounds,
        purpose=purpose,
        confidence=0.95,
    )
    return CandidateAction(
        candidate_id=f"forced-pixel-popup-close:{capture_id}",
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "control_role": "dismiss",
            "expected_state_hint": "close_popup_x",
            "normalized_bounds": bounds.to_dict(),
            "popup_close_corner_fallback": True,
            "popup_close_bottom_x_fallback": bottom_x,
            "popup_close_pixel_visual": True,
            "forced_pixel_popup_close": True,
            "popup_close_corner_contract": dict(contract),
        },
    )


def _synthetic_hjll_airtest_popup_close_candidate(
    *,
    work: ExplorationWork,
) -> CandidateAction | None:
    """仅产品固定流程允许未重绑像素 X 时使用脚本中央下方点。"""

    if not _is_hjll_airtest_work(work):
        return None
    from services.api.app.application.app_synthetic_touch_contracts import (
        popup_close_corner_contract,
    )
    from services.api.app.domain.exploration import NormalizedBounds

    metadata = work.frontier.observation.metadata
    if not isinstance(metadata, Mapping):
        return None
    package_name = str(metadata.get("package_name") or "").strip()
    if package_name != "com.hj.ttthree":
        return None
    attempts = metadata.get("surface_phase_same_popup_attempt_count")
    if _hjll_popup_attempt_limit_reached(work):
        # 与 Airtest 八轮「截图→点 X」一致；预算耗尽后由无进展保护收口。
        return None
    facts = metadata.get("surface_facts")
    if not isinstance(facts, Mapping) or facts.get("popup_visible") is not True:
        return None
    # HJLL 的圆 X 虽然固定在水平中央，垂直位置会随广告素材高度变化：
    # 实机已验证为约 y=0.71 或 y=0.80。旧版只点 0.80，遇到 y=0.71 时会
    # 偏低约 270px（1440x3040），等同没有点到关闭键。每轮仍优先使用本帧
    # 像素重绑；仅重绑缺失时才在这两个经验证的安全点之间有限交替。Airtest
    # 原脚本的首点为 y=0.80，必须保持这一主路径；y=0.71 是素材上移时的次选。
    attempt_index = (
        int(attempts)
        if isinstance(attempts, int) and not isinstance(attempts, bool)
        else 0
    )
    center_y = 0.80 if attempt_index % 2 == 0 else 0.71
    bounds = NormalizedBounds(0.45, center_y - 0.025, 0.55, center_y + 0.025)
    purpose = (
        "HJLL 弹窗未重绑到像素 X；按 Airtest 已验证的中央下方圆 X 安全点关闭并截图"
    )
    contract = popup_close_corner_contract(
        bounds=bounds,
        purpose=purpose,
        confidence=0.90,
    )
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    return CandidateAction(
        candidate_id=f"forced-pixel-popup-close:{capture_id}",
        semantic_target="关闭弹层下方圆X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "control_role": "dismiss",
            "expected_state_hint": "close_popup_x",
            "normalized_bounds": bounds.to_dict(),
            "popup_close_corner_fallback": True,
            "popup_close_bottom_x_fallback": True,
            "popup_close_airtest_fallback": True,
            "forced_pixel_popup_close": True,
            "popup_close_corner_contract": dict(contract),
        },
    )


def _popup_close_candidate_for_obstruction(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> CandidateAction | None:
    """仅真实挡路弹窗才关层；误标 overlay 时把回合留给底栏。"""

    if not _blocking_popup_requires_dismiss(work):
        return None
    # 账号凭证弹层禁止点下方 X／角 X；必须走保存凭证→我已保存。
    if _work_shows_profile_credential(work):
        return None
    # 满帧让路后交还覆盖／底栏，不再强关。
    if _home_popup_budget_allows_coverage_seek(work):
        return None
    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    same_attempts = metadata.get("surface_phase_same_popup_attempt_count")
    noop_streak = metadata.get("dismiss_noop_streak")
    is_hjll = _is_hjll_airtest_work(work)
    if is_hjll and _hjll_popup_attempt_limit_reached(work):
        # Airtest 固定八轮后停止出手，交给无进展预算收口，避免无限烧探索配额。
        return None
    wasted_close = (
        isinstance(same_attempts, int)
        and not isinstance(same_attempts, bool)
        and same_attempts >= 2
    ) or (
        isinstance(noop_streak, int)
        and not isinstance(noop_streak, bool)
        and noop_streak >= 2
    )
    # 连续无效关层：底栏硬覆盖未完时立即放弃强关，交还已签发底栏。
    if (
        wasted_close
        and not is_hjll
        and coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=metadata,
    )
    ):
        return None
    # 弹窗 Watcher cascade：A 文案 → B 几何安全 X → C 蒙层 → D 底栏「<」连点两下。
    # 不含 popup_back_recovery（系统 BACK 回包路径，走上方 early branch）。
    tabs_hard_incomplete = playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=metadata,
    )

    def _terms(candidate: CandidateAction) -> str:
        return " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("semantic_name") or ""),
            )
        ).casefold()

    def _is_purchase_cta(terms: str) -> bool:
        return any(
            bad in terms
            for bad in ("抢购", "搶購", "购买", "購買", "开通会员", "立即开通")
        )

    def _score(candidate: CandidateAction) -> tuple[int, int, int, int]:
        role = str(candidate.metadata.get("control_role") or "").casefold()
        terms = _terms(candidate)
        role_rank = int(role in {"dismiss", "skip"})
        term_rank = int(
            any(
                marker in terms
                for marker in (
                    "关闭",
                    "關閉",
                    "跳过",
                    "跳過",
                    "略过",
                    "略過",
                    "close",
                    "dismiss",
                    "skip",
                    "知道了",
                    "我知道了",
                )
            )
        )
        if _is_purchase_cta(terms):
            return (-1, 0, 0, 0)
        raw_conf = candidate.metadata.get("confidence")
        conf_rank = int(isinstance(raw_conf, (int, float)) and float(raw_conf) * 100)
        pixel_rank = int(
            candidate.metadata.get("forced_pixel_popup_close") is True
            or candidate.metadata.get("popup_close_pixel_visual") is True
            or str(candidate.candidate_id or "").startswith("forced-pixel-popup-close:")
        )
        return (pixel_rank, role_rank, term_rank, conf_rank)

    def _is_text_close(candidate: CandidateAction) -> bool:
        try:
            from services.api.app.domain.app_product.popup_close import (
                is_popup_text_close_target,
            )
        except Exception:  # noqa: BLE001
            return False
        return is_popup_text_close_target(_terms(candidate))

    def _is_scrim_close(candidate: CandidateAction) -> bool:
        if candidate.metadata.get("forced_popup_scrim_dismiss") is True:
            return True
        terms = _terms(candidate)
        return any(m in terms for m in ("蒙层", "scrim", "空白处", "遮罩空白"))

    def _geometry_ok(candidate: CandidateAction) -> bool:
        # 像素／下方圆 X 合成点已在甜区校验，勿再被「仅顶角」几何误杀。
        if (
            candidate.metadata.get("forced_pixel_popup_close") is True
            or candidate.metadata.get("popup_close_bottom_x_fallback") is True
            or candidate.metadata.get("popup_close_pixel_visual") is True
        ):
            return True
        if tabs_hard_incomplete:
            return _dismiss_candidate_is_corner_safe_close(candidate)
        # 非硬覆盖：中部模态或底／顶角安全区都可（宫格下方圆 X）。
        return _dismiss_candidate_is_center_modal(
            candidate
        ) or _dismiss_candidate_is_corner_safe_close(candidate)

    def _tap_closers() -> list[CandidateAction]:
        return [
            candidate
            for candidate in eligible
            if candidate.action_type
            in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
            and _score(candidate)[0] >= 0
            and _geometry_ok(candidate)
            and _close_target_large_enough(candidate)
            and not _is_profile_credential_skip_candidate(candidate)
            and _popup_close_avoids_bottom_navigation_band(candidate)
        ]

    tap_pool = _tap_closers()

    # A：OCR／语义文案关钮
    text_closers = [c for c in tap_pool if _is_text_close(c)]
    if text_closers:
        return max(text_closers, key=_score)

    # B：几何安全圆 X（含同帧像素／Airtest 合成点）
    x_closers = [
        c
        for c in tap_pool
        if not _is_scrim_close(c)
        and (
            _score(c)[1:3] != (0, 0)
            or c.metadata.get("forced_pixel_popup_close") is True
            or c.metadata.get("popup_close_corner_fallback") is True
            or c.metadata.get("popup_close_bottom_x_fallback") is True
        )
    ]
    pixel_close = _synthetic_pixel_popup_close_candidate(work=work)
    if pixel_close is not None:
        x_closers.append(pixel_close)
    else:
        airtest_close = _synthetic_hjll_airtest_popup_close_candidate(work=work)
        if airtest_close is not None:
            x_closers.append(airtest_close)
    if x_closers:
        return max(x_closers, key=_score)

    # C：蒙层空白
    scrim_closers = [c for c in tap_pool if _is_scrim_close(c)]
    if scrim_closers:
        return max(scrim_closers, key=_score)

    # D：底栏「<」连点两下（执行层 cloud_back_tap_twice）
    cloud_back = next(
        (
            candidate
            for candidate in eligible
            if candidate.action_type is InteractionOperation.BACK
            and candidate.risk_level
            in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
            and (
                candidate.metadata.get("cloud_back_popup_close") is True
                or "底部导航栏" in candidate.semantic_target
                or "cloud_back"
                in str(candidate.metadata.get("purpose") or "").casefold()
            )
        ),
        None,
    )
    return cloud_back


def _safe_missing_current_frame_coverage_branch(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> CandidateAction | None:
    """直接执行同帧低风险缺页入口；首页先巡检中部分栏目，再考虑底栏全局分支。"""

    if not _uses_dynamic_app_frontier(work) or work.app_goal is None:
        return None
    # 底栏硬覆盖未完：禁止分栏目／内容卡／发现等覆盖寻页，只允许切栏／安全关层。
    # 例外：主页上方分类课纲未完且池内已有上方 seek 时继续，交由下方优先返回。
    if playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=work.frontier.observation.metadata,
    ):
        observation = work.frontier.observation
        metadata = (
            observation.metadata if isinstance(observation.metadata, Mapping) else {}
        )
        from services.api.app.application.app_coverage_page_seek import (
            home_top_category_tabs_incomplete,
        )
        from services.api.app.application.app_exploration_flow_contract import (
            analysis_blocks_home_top_category_tabs,
            popup_phase_blocks_home_top_category_tabs,
        )

        pkg = metadata.get("package_name")
        package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
        analysis_proxy = _observation_visual_analysis_proxy(observation)
        allow_home_top = (
            str(observation.page_type or "").casefold() == "home"
            and home_top_category_tabs_incomplete(
                coverage_metadata=metadata,
                package_name=package_name,
            )
            and not popup_phase_blocks_home_top_category_tabs(
                metadata, analysis=analysis_proxy
            )
            and not analysis_blocks_home_top_category_tabs(analysis_proxy)
            # 软浮层：blocking 已放行；仅真挡路才禁止上方栏覆盖寻页。
            and not (
                _blocking_popup_requires_dismiss(work)
                and analysis_blocks_home_top_category_tabs(analysis_proxy)
            )
        )
        if not allow_home_top:
            return None
    observation = work.frontier.observation
    facts = observation.metadata.get("surface_facts")
    page_state = str(observation.page_state or "").casefold()
    # 仅满帧让路才允许在 popup 上空点非底栏寻页；若已有可见底栏候选则放行。
    yield_through_overlay = _home_popup_budget_allows_coverage_seek(work)
    if not yield_through_overlay and page_state in {
        "popup",
        "modal",
        "overlay",
        "dialog",
    }:
        yield_through_overlay = any(
            candidate.metadata.get("bottom_navigation_tab_seek") is True
            and _bottom_tab_bounds_in_safe_band(candidate)
            for candidate in eligible
        )
    stable_ok = page_state in {
        "stable",
        "normal",
        "loaded",
        "content_available",
    }
    obstructed = is_obstructed_surface(
        page_state=observation.page_state,
        page_type=observation.page_type,
        overlay_signature=observation.overlay_signature,
        surface_facts=facts if isinstance(facts, Mapping) else {},
    )
    if not stable_ok and not (
        yield_through_overlay and page_state in {"popup", "modal", "overlay", "dialog"}
    ):
        return None
    if obstructed and not yield_through_overlay:
        return None
    required = tuple(item.casefold() for item in work.app_goal.required_page_types)
    covered = {
        str(item).casefold()
        for item in observation.metadata.get("covered_page_types", ())
        if isinstance(item, str)
    }
    missing = set(required) - covered
    page_type = str(observation.page_type or "").casefold()
    prefer_home_category = page_type in {"home", "video_list", "content_list"}

    def _is_safe_coverage_click(candidate: CandidateAction) -> bool:
        terms = " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ).casefold()
        synthetic_blocked = candidate.metadata.get("coverage_page_seek_contract") is not None
        allow_medium = (
            candidate.metadata.get("home_category_grid_seek") is True
            or candidate.metadata.get("home_top_category_tab_seek") is True
            or candidate.metadata.get("bottom_navigation_tab_seek") is True
        )
        risk_ok = candidate.risk_level is InteractionRiskLevel.LOW or (
            allow_medium and candidate.risk_level is InteractionRiskLevel.MEDIUM
        )
        return not (
            synthetic_blocked
            or candidate.metadata.get("control_role") in {"dismiss", "skip"}
            or candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
            or not risk_ok
            or candidate.metadata.get("frontier_scope") not in {"local", "page", "app"}
            or any(
                marker in terms
                for marker in (
                    "提交发布",
                    "确认发布",
                    "立即发布",
                    "立即支付",
                    "确认支付",
                    "会员中心",
                    "开通会员",
                    "立即开通",
                    "充值",
                    "submit",
                    "confirm publish",
                    "confirm payment",
                )
            )
        )

    category_safe = [
        candidate
        for candidate in eligible
        if candidate.metadata.get("home_category_grid_seek") is True
        and _is_safe_coverage_click(candidate)
    ]
    top_category_safe = [
        candidate
        for candidate in eligible
        if candidate.metadata.get("home_top_category_tab_seek") is True
        and _is_safe_coverage_click(candidate)
    ]
    bottom_tab_safe = [
        candidate
        for candidate in eligible
        if candidate.metadata.get("bottom_navigation_tab_seek") is True
        and candidate.metadata.get("coverage_page_seek_contract") is None
        and _is_safe_coverage_click(candidate)
        and _bottom_tab_bounds_in_safe_band(
            candidate,
            relax=True,
        )
    ]
    # 主页上方分类未完：优先于底栏切走（海角最新／国产／动漫／漫画；推荐不点）。
    # 池空时也不得继续返回短剧；交由 deterministic 合成槽位。
    if page_type == "home":
        from services.api.app.application.app_coverage_page_seek import (
            _candidate_matches_home_top_category_label,
            _next_unvisited_home_top_category_tab_label,
            _visited_home_top_category_tabs,
            home_top_category_tabs_incomplete,
        )
        from services.api.app.application.app_ui_tree_features import (
            ordered_home_top_category_tab_labels,
        )

        pkg = None
        meta = observation.metadata if isinstance(observation.metadata, Mapping) else {}
        raw_pkg = meta.get("package_name")
        if isinstance(raw_pkg, str) and raw_pkg.strip():
            pkg = raw_pkg.strip()
        if home_top_category_tabs_incomplete(
            coverage_metadata=meta,
            package_name=pkg,
        ):
            next_label = _next_unvisited_home_top_category_tab_label(
                visited_labels=_visited_home_top_category_tabs(meta),
                ordered_labels=ordered_home_top_category_tab_labels(
                    package_name=pkg
                ),
            )
            matched = [
                item
                for item in top_category_safe
                if next_label
                and _candidate_matches_home_top_category_label(
                    item,
                    next_label=next_label,
                    package_name=pkg,
                    visual=None,
                )
            ]
            if matched:
                return min(matched, key=_home_category_grid_selection_key)
            # 池内无下一栏：交还 deterministic 合成槽位，禁止再点已访「最新」。
            return None
    # 底栏硬验收未完时，底栏必须优先于首页分栏目／内容卡，避免点进详情后底栏消失。
    tabs_hard_incomplete = playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=observation.metadata,
    )
    if tabs_hard_incomplete and bottom_tab_safe:
        return _pick_next_playbook_bottom_tab(
            work=work,
            visible_bottom=bottom_tab_safe,
        ) or min(bottom_tab_safe, key=_bottom_navigation_tab_selection_key)
    # 稳定首页／列表：中部分栏目优先于一般缺页寻页（仅底栏已巡完）。
    if prefer_home_category and category_safe and not tabs_hard_incomplete:
        return min(
            category_safe,
            key=_home_category_grid_selection_key,
        )
    # 根业务 Tab（含 video_list）上未巡检底栏优先于同页内容卡。
    if page_type in {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
    } and bottom_tab_safe:
        return _pick_next_playbook_bottom_tab(
            work=work,
            visible_bottom=bottom_tab_safe,
        ) or min(bottom_tab_safe, key=_bottom_navigation_tab_selection_key)

    if not missing:
        # 弹层让路后若缺口已空，仍可用当前帧真实底栏切走空转关层。
        if yield_through_overlay:
            bottom = [
                candidate
                for candidate in eligible
                if (
                    candidate.metadata.get("frontier_scope") == "app"
                    or candidate.metadata.get("bottom_navigation_tab_seek") is True
                )
                and candidate.metadata.get("coverage_page_seek_contract") is None
                and _is_safe_coverage_click(candidate)
            ]
            if bottom:
                return _pick_next_playbook_bottom_tab(
                    work=work,
                    visible_bottom=bottom,
                ) or min(bottom, key=_bottom_navigation_tab_selection_key)
        return None

    safe: list[CandidateAction] = []
    for candidate in eligible:
        expected = _text(candidate.metadata.get("expected_next_page_type"))
        if not _is_safe_coverage_click(candidate) or expected not in missing:
            continue
        # 底栏硬覆盖未完：禁止选详情／付费入口，打断 home↔详情循环。
        if tabs_hard_incomplete and (
            expected in {"content_detail", "video_detail", "payment"}
            or candidate_targets_detail_page(candidate)
        ):
            continue
        safe.append(candidate)
    if not safe:
        return None

    required_rank = {page_type: len(required) - index for index, page_type in enumerate(required)}
    # 根业务页上底栏缺口寻页优先于同页 local/page 内容卡（杏吧常落 video_list）。
    prefer_global_navigation = page_type in {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
    }
    return max(
        safe,
        key=lambda candidate: (
            int(candidate.metadata.get("home_category_grid_seek") is True),
            int(candidate.metadata.get("bottom_navigation_tab_seek") is True),
            int(candidate.metadata.get("coverage_page_seek") is True),
            int(
                prefer_global_navigation
                and (
                    candidate.metadata.get("frontier_scope") == "app"
                    or candidate.metadata.get("bottom_navigation_tab_seek") is True
                )
            ),
            required_rank.get(
                _text(candidate.metadata.get("expected_next_page_type")),
                0,
            ),
            candidate.exploration_value,
            candidate.candidate_id,
        ),
    )


def _dynamic_app_candidate_rank(
    work: ExplorationWork,
    candidate: CandidateAction,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    """动态探索只按当前候选事实、缺口和信息增益排序。"""

    page_state = str(work.frontier.observation.page_state or "").casefold()
    surface_facts = work.frontier.observation.metadata.get("surface_facts")
    if not isinstance(surface_facts, Mapping):
        surface_facts = {}
    age_confirm_rank = int(
        observation_needs_age_confirm(
            page_state=page_state,
            page_type=work.frontier.observation.page_type,
            surface_facts=surface_facts,
        )
        and _is_dynamic_age_confirm_clear(work=work, candidate=candidate)
    )
    overlay_rank = int(
        page_state in {"popup", "modal", "overlay", "dialog", "system_dialog"}
        and candidate.action_type is InteractionOperation.CLICK
        and any(
            marker
            in " ".join(
                (
                    candidate.semantic_target,
                    str(candidate.metadata.get("purpose") or ""),
                    str(candidate.metadata.get("expected_state_hint") or ""),
                )
            ).casefold()
            for marker in (
                "close",
                "dismiss",
                "skip",
                "关闭",
                "跳过",
                "下次",
                "稍后",
                "知道了",
                "确认",
            )
        )
    )
    expected_page_type = _text(candidate.metadata.get("expected_next_page_type"))
    covered = {
        str(item).casefold()
        for item in work.frontier.observation.metadata.get("covered_page_types", ())
        if isinstance(item, str)
    }
    required = {
        item.casefold() for item in (work.app_goal.required_page_types if work.app_goal else ())
    }
    uncovered_rank = uncovered_required_page_rank(
        expected_page_type,
        required=required,
        covered=covered,
    )
    coverage_seek_rank = int(candidate.metadata.get("coverage_page_seek") is True)
    home_category_rank = int(candidate.metadata.get("home_category_grid_seek") is True)
    bottom_tab_rank = int(candidate.metadata.get("bottom_navigation_tab_seek") is True)
    # 底栏硬覆盖未完：详情／付费入口降到最低，禁止 home↔详情空转。
    tabs_hard_incomplete = playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=work.frontier.observation.metadata,
    )
    if tabs_hard_incomplete and (
        expected_page_type in {"content_detail", "video_detail", "payment"}
        or candidate_targets_detail_page(candidate)
    ):
        coverage_seek_rank = 0
        home_category_rank = 0
        uncovered_rank = 0
    # 底栏未完：切栏压过 OBSERVE／一般可逆动作。
    if tabs_hard_incomplete and bottom_tab_rank:
        bottom_tab_rank = 3
    # 首页／业务 Tab 弹层让路后寻页必须压过关层，否则会永久关弹窗直至 business stall
    home_popup_seek_rank = int(
        (coverage_seek_rank >= 1 or home_category_rank >= 1 or bottom_tab_rank >= 1)
        and _home_popup_budget_allows_coverage_seek(work)
    )
    if home_popup_seek_rank:
        overlay_rank = 0
    reversible_rank = int(
        candidate.action_type
        in {
            InteractionOperation.BACK,
            InteractionOperation.SCROLL,
            InteractionOperation.OBSERVE,
            InteractionOperation.WARM_RESTORE,
        }
        or candidate.risk_level is InteractionRiskLevel.LOW
    )
    # 底栏未完禁止 OBSERVE 空转抢高分——仅当下一栏已可见。
    if tabs_hard_incomplete and candidate.action_type is InteractionOperation.OBSERVE:
        tab_pool = tuple(
            item
            for item in work.frontier.candidates
            if item.metadata.get("bottom_navigation_tab_seek") is True
            and _bottom_tab_bounds_in_safe_band(item, relax=True)
        )
        if (
            tab_pool
            and _pick_next_playbook_bottom_tab(work=work, visible_bottom=tab_pool)
            is not None
        ):
            reversible_rank = 0
    return (
        age_confirm_rank,
        overlay_rank,
        uncovered_rank,
        reversible_rank,
        int(candidate.risk_level is InteractionRiskLevel.LOW),
        int(expected_page_type is not None and expected_page_type not in covered),
        -candidate.prior_failure_count,
        candidate.exploration_value,
        -candidate.navigation_depth,
    )


def _is_dynamic_age_confirm_clear(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """动态 Frontier 允许执行当前截图上的年龄确认进入候选。"""

    surface_facts = work.frontier.observation.metadata.get("surface_facts")
    if not isinstance(surface_facts, Mapping):
        surface_facts = {}
    observation_text = work.frontier.observation.metadata.get("visual_summary")
    if not observation_needs_age_confirm(
        page_state=work.frontier.observation.page_state,
        page_type=work.frontier.observation.page_type,
        surface_facts=surface_facts,
        observation_text=observation_text,
    ):
        return False
    return is_age_confirm_candidate(
        action_type=candidate.action_type,
        risk_level=candidate.risk_level,
        semantic_target=candidate.semantic_target,
        purpose=candidate.metadata.get("purpose"),
        expected_state_hint=candidate.metadata.get("expected_state_hint"),
        metadata=dict(candidate.metadata),
    )


def _is_dynamic_profile_credential_save(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """动态 Frontier 允许执行个人中心账号凭证「保存凭证」（第一步）。"""

    from services.api.app.application.app_ui_tree_features import (
        candidate_matches_profile_credential_save,
        observation_matches_profile_credential,
    )

    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if _profile_credential_save_phase_done(work):
        return False
    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    facts = metadata.get("surface_facts")
    from services.api.app.application.app_semantic_progress import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        bottom_tab_label_already_visited,
    )

    visited_raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    visited: set[str] = set()
    if isinstance(visited_raw, (list, tuple, set, frozenset)):
        visited = {str(item) for item in visited_raw if str(item or "").strip()}
    profile_tab_visited = bottom_tab_label_already_visited("我的", visited)
    blob = " ".join(
        str(part or "")
        for part in (
            observation.metadata.get("visual_summary") if isinstance(observation.metadata, Mapping) else None,
            observation.metadata.get("ocr_text") if isinstance(observation.metadata, Mapping) else None,
            observation.page_type,
            observation.page_state,
            facts.get("dialog_kind") if isinstance(facts, Mapping) else None,
            " ".join(str(item) for item in (observation.key_elements or ())),
        )
    )
    if not observation_matches_profile_credential(
        page_type=observation.page_type,
        page_state=observation.page_state,
        observation_text=blob,
        package_name=package_name,
        profile_tab_visited=profile_tab_visited,
    ):
        return False
    if candidate.metadata.get("profile_credential_save") is True:
        return True
    return candidate_matches_profile_credential_save(
        candidate,
        package_name=package_name,
    )


def _is_dynamic_profile_credential_dismiss(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """动态 Frontier：保存成功后允许点「我已保存」关闭凭证弹层。"""

    from services.api.app.application.app_ui_tree_features import (
        candidate_matches_profile_credential_skip,
        observation_matches_profile_credential,
    )

    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if not _profile_credential_save_phase_done(work):
        return False
    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    facts = metadata.get("surface_facts")
    from services.api.app.application.app_semantic_progress import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        bottom_tab_label_already_visited,
    )

    visited_raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    visited: set[str] = set()
    if isinstance(visited_raw, (list, tuple, set, frozenset)):
        visited = {str(item) for item in visited_raw if str(item or "").strip()}
    profile_tab_visited = bottom_tab_label_already_visited("我的", visited)
    blob = " ".join(
        str(part or "")
        for part in (
            metadata.get("visual_summary"),
            metadata.get("ocr_text"),
            observation.page_type,
            observation.page_state,
            facts.get("dialog_kind") if isinstance(facts, Mapping) else None,
            " ".join(str(item) for item in (observation.key_elements or ())),
        )
    )
    if not observation_matches_profile_credential(
        page_type=observation.page_type,
        page_state=observation.page_state,
        observation_text=blob,
        package_name=package_name,
        profile_tab_visited=profile_tab_visited,
    ):
        # 同帧仅有「我已保存」合成候选时仍放行。
        if candidate.metadata.get("profile_credential_dismiss") is not True:
            return False
    if candidate.metadata.get("profile_credential_dismiss") is True:
        return True
    return candidate_matches_profile_credential_skip(
        candidate,
        package_name=package_name,
    )


def _is_current_curriculum_low_risk_click(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """只放行经当前 Playbook 阶段语义复核的低风险点击候选。"""

    if (
        candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
    ):
        return False
    from services.api.app.application.app_script_curriculum import (
        AppCurriculumProgress,
        curriculum_action_matches_current_phase,
        extract_playbook_id_from_objective,
        resolve_playbook_by_id,
    )

    playbook = resolve_playbook_by_id(extract_playbook_id_from_objective(work.objective))
    raw_progress = work.frontier.observation.metadata.get("app_curriculum_progress")
    if playbook is None or not isinstance(raw_progress, Mapping):
        return False
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
        return curriculum_action_matches_current_phase(
            playbook=playbook,
            progress=progress,
            action_type=candidate.action_type.value,
            semantic_terms=(
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            ),
        )
    except (TypeError, ValueError):
        return False


def _is_trusted_hlw_popup_close(candidate: CandidateAction) -> bool:
    """只识别受信 Accessibility 合同签发的精确弹层关闭候选。"""

    purpose = str(candidate.metadata.get("purpose") or "")
    return (
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.metadata.get("expected_state_hint") == "close_popup_x"
        and (
            (
                candidate.semantic_target == "关闭神器甄选"
                and purpose.startswith("仅关闭目标包当前神器甄选遮挡")
            )
            or (
                candidate.semantic_target == "关闭弹层"
                and purpose.startswith("仅关闭目标包当前遮挡")
            )
        )
    )


def _is_trusted_popup_close_recovery(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """业务课纲被可信遮挡阻塞时允许只关闭弹层，不推进当前阶段。"""

    if not _is_trusted_hlw_popup_close(candidate):
        return False
    observation = work.frontier.observation
    facts = observation.metadata.get("surface_facts")
    popup_visible = (
        str(observation.page_state or "").casefold() in {"popup", "modal", "overlay", "dialog"}
        or bool(observation.overlay_signature)
        or (isinstance(facts, Mapping) and facts.get("popup_visible") is True)
    )
    if not popup_visible:
        return False
    from services.api.app.application.app_script_curriculum import AppCurriculumProgress

    raw_progress = observation.metadata.get("app_curriculum_progress")
    if not isinstance(raw_progress, Mapping):
        return False
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
    except (TypeError, ValueError):
        return False
    phase_id = progress.current_phase_id or ""
    if phase_id == "shenqi":
        return progress.current_phase_action_count >= 2
    return phase_id.startswith("tab:") or phase_id in {
        "content_detail",
        "content_detail_back",
        "profile",
    }


def _is_trusted_content_detail_list_restore(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """HLW 详情阶段停在无普通卡片列表时，只允许切回吃瓜列表。"""

    observation = work.frontier.observation
    facts = observation.metadata.get("surface_facts")
    controls = facts.get("trusted_accessibility_controls") if isinstance(facts, Mapping) else None
    if not isinstance(controls, Mapping):
        return False
    package_name = observation.metadata.get("package_name")
    content_card_count = controls.get("content_card_count")
    bottom_navigation_count = controls.get("bottom_navigation_count")
    if (
        controls.get("contract_id") != "hlwapp-accessibility.v1"
        or not isinstance(package_name, str)
        or not package_name
        or controls.get("package_name") != package_name
        or controls.get("page_type") != "content_list"
        or isinstance(content_card_count, bool)
        or content_card_count != 0
        or isinstance(bottom_navigation_count, bool)
        or not isinstance(bottom_navigation_count, int)
        or bottom_navigation_count < 1
    ):
        return False
    from services.api.app.application.app_script_curriculum import AppCurriculumProgress

    raw_progress = observation.metadata.get("app_curriculum_progress")
    if not isinstance(raw_progress, Mapping):
        return False
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
    except (TypeError, ValueError):
        return False
    return (
        progress.playbook_id == "hlwapp"
        and progress.current_phase_id == "content_detail"
        and candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.semantic_target == "底栏「吃瓜」"
        and candidate.metadata.get("expected_state_hint") == "tab:吃瓜"
        and str(candidate.metadata.get("purpose") or "").startswith("打开目标包底栏 tab:吃瓜")
    )


def _decide_app_curriculum_action(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
    reason: str,
    llm_candidate: CandidateAction | None = None,
    llm_purpose: str | None = None,
) -> ActionIntent:
    """按 script_curriculum.current_phase_hint.prefer_hints 确定性选候选。"""
    if not eligible:
        raise ExplorerAgentOutputError("App 课纲推进时没有可执行候选")
    depth = max((int(item.navigation_depth or 0) for item in eligible), default=0)
    curriculum = _script_curriculum_for_work(work=work) or {}
    hint = curriculum.get("current_phase_hint") if isinstance(curriculum, dict) else None
    prefer_hints: list[str] = []
    phase_id = ""
    if isinstance(hint, dict):
        phase_id = str(hint.get("phase_id") or "")
        raw_hints = hint.get("prefer_hints") or []
        if isinstance(raw_hints, (list, tuple)):
            prefer_hints = [str(item) for item in raw_hints if str(item).strip()]
    ranked = sorted(
        eligible,
        key=lambda item: _app_playbook_candidate_rank(
            item,
            navigation_depth=depth,
            prefer_hints=prefer_hints,
        ),
        reverse=True,
    )
    phase_progress = curriculum.get("phase_progress") if isinstance(curriculum, dict) else None
    phase_action_count = (
        phase_progress.get("current_phase_action_count")
        if isinstance(phase_progress, Mapping)
        else None
    )
    curriculum_candidate = _current_platform_curriculum_candidate(
        eligible=eligible,
        phase_id=phase_id,
        phase_action_count=phase_action_count,
    )
    trusted_phase_close = next(
        (
            item
            for item in eligible
            if phase_id == "shenqi"
            and phase_action_count == 2
            and _is_trusted_hlw_popup_close(item)
        ),
        None,
    )
    trusted_detail_restore = next(
        (
            item
            for item in eligible
            if _is_trusted_content_detail_list_restore(work=work, candidate=item)
        ),
        None,
    )
    chosen = (
        curriculum_candidate
        or trusted_phase_close
        or trusted_detail_restore
        or llm_candidate
        or ranked[0]
    )
    restore_only = (
        _is_trusted_popup_close_recovery(work=work, candidate=chosen)
        or _is_trusted_content_detail_list_restore(work=work, candidate=chosen)
    ) and not (_is_current_curriculum_low_risk_click(work=work, candidate=chosen))
    # 平台为当前阶段生成的零触控／安全滚动候选必须先执行；模型仅在没有该标记时选择。
    if restore_only:
        purpose = str(chosen.metadata.get("purpose") or "仅关闭当前可信遮挡弹层")
    elif (
        curriculum_candidate is None
        and trusted_phase_close is None
        and trusted_detail_restore is None
        and llm_candidate is not None
        and llm_purpose
    ):
        purpose = llm_purpose
    else:
        purpose = (
            f"{reason}；推进 phase_id={phase_id or 'unknown'}："
            f"「{chosen.semantic_target}」（depth={depth}）。"
        )
    return _build_action_intent(
        work=work,
        candidate=chosen,
        purpose=purpose,
        curriculum_phase_id=phase_id or None,
        restore_only=restore_only,
    )


def _current_platform_curriculum_candidate(
    *,
    eligible: Sequence[CandidateAction],
    phase_id: str,
    phase_action_count: object,
) -> CandidateAction | None:
    """返回平台为当前课纲动作生成的候选，禁止模型用旁路动作覆盖它。"""

    if (
        not phase_id
        or isinstance(phase_action_count, bool)
        or not isinstance(phase_action_count, int)
    ):
        return None
    marker_keys = (
        ("curriculum_observation", InteractionOperation.OBSERVE),
        ("curriculum_action", InteractionOperation.SCROLL),
    )
    for candidate in eligible:
        for marker_key, expected_operation in marker_keys:
            marker = candidate.metadata.get(marker_key)
            if (
                candidate.action_type is expected_operation
                and isinstance(marker, Mapping)
                and marker.get("phase_id") == phase_id
                and marker.get("phase_action_count") == phase_action_count
            ):
                return candidate
    return None


def _app_playbook_candidate_rank(
    candidate: CandidateAction,
    *,
    navigation_depth: int = 0,
    prefer_hints: Sequence[str] | None = None,
) -> tuple[int, int]:
    """结合课纲 prefer_hints：深度 0 等稳／外链／跳过；深度≥1 关层／下滑／换栏，禁止反复开屏 OBSERVE。"""
    hint = str(candidate.metadata.get("expected_state_hint") or "").casefold()
    text = " ".join(
        part
        for part in (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            hint,
            candidate.action_type.value,
        )
        if part
    ).casefold()
    depth = max(0, int(navigation_depth or 0))
    score = 10
    if "splash_burst" in text or "开屏零触控" in text:
        # 开屏连拍只允许浅深度；之后必须按该包课纲下一动作推进
        score = 120 if depth == 0 else 5
    elif "等待" in text or "wait_line" in text or "wait_stable" in text:
        score = 95 if depth == 0 else 25
    elif "ad_outbound" in text or "外链广告" in text:
        # 仅 91cg／untitled.py 有此外链步骤；浅深度优先于乱点跳过
        score = 118 if depth <= 2 else 45
    elif "跳过" in text or "tap_skip" in text or ("skip" in text and "ad_outbound" not in text):
        # 仅当该包课纲含 tap_skip 时，由 prefer_hints 抬到最高；基线不抢 91cg 外链
        score = 100 if depth <= 1 else 35
    elif "confirm" in text or "橘色确认" in text:
        score = 92 if depth <= 2 else 55
    elif "关闭" in text or "弹层" in text or "close_popup" in text or "popup_x" in text:
        score = 98 if depth <= 2 else 55
    elif candidate.metadata.get("coverage_page_seek") is True or "覆盖寻页" in text:
        # 规则集缺页寻航：高于普通下滑／观察，低于年龄／关弹层 prefer。
        score = 125
    elif "年龄" in text or "age_gate" in text:
        score = 85 if depth == 0 else 40
    elif "滑动" in text or "scroll" in text or "swipe" in text:
        score = 65 if depth == 0 else 110
    elif "底栏" in text or "tab:" in text:
        score = 45 if depth <= 1 else 100
    elif candidate.action_type is InteractionOperation.WARM_RESTORE:
        score = 130
    elif candidate.action_type is InteractionOperation.BACK or "返回" in text:
        # 仅当课纲明确 prefer back 时抬升；禁止因 prefer 列表误伤把 BACK 抬成最高
        score = 40
    elif candidate.action_type is InteractionOperation.OBSERVE:
        score = 35 if depth == 0 else 8

    # 当前课纲阶段 prefer_hints 强抬升（唯一允许偏离基线分的依据＝脚本课纲）
    for index, prefer in enumerate(prefer_hints or ()):
        token = str(prefer).strip().casefold()
        if not token:
            continue
        if token in {"back", "observe"}:
            # 短词必须与动作类型严格对齐，避免「系统返回键」抢走 ad_outbound
            if token == "back" and candidate.action_type is not InteractionOperation.BACK:
                continue
            if token == "observe" and candidate.action_type is not InteractionOperation.OBSERVE:
                continue
        tab_name = token.split(":", 1)[1] if token.startswith("tab:") else ""
        matched = token in text or token in hint or bool(tab_name and tab_name in text)
        if matched:
            score = max(score, 220 - index * 12)
            break
    return score, candidate.exploration_value


def _decide_probe(
    work: ProbeWork,
    *,
    now: datetime,
) -> ActionIntent | ExplorationTerminal:
    """按 Evidence Request 精确构造 Probe，不让模型猜测动作或恢复来源。"""
    request = work.evidence_request
    if now >= min(work.deadline, request.expires_at):
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
            reason="ProbeWork 或 Evidence Request 已超过截止时间。",
        )
    if work.budget.remaining_actions == 0 or work.budget.remaining_duration_ms == 0:
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
            reason="ProbeWork 预算已耗尽。",
        )
    if request.side_effect_policy is EvidenceSideEffectPolicy.APPROVAL_REQUIRED:
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BLOCKED,
            reason="Evidence Request 需要外部审批，Explorer Agent 无权自行批准。",
        )
    requirement = request.required_interaction
    if (
        request.side_effect_policy is EvidenceSideEffectPolicy.PROHIBITED
        and requirement is not None
    ):
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BLOCKED,
            reason="Evidence Request 禁止执行所要求的交互。",
        )
    action_type = requirement.operation if requirement is not None else InteractionOperation.OBSERVE
    semantic_target = requirement.semantic_target if requirement is not None else None
    purpose = requirement.purpose if requirement is not None else request.evidence_goal
    risk = (
        InteractionRiskLevel.MEDIUM
        if request.side_effect_policy is EvidenceSideEffectPolicy.ISOLATED_TEST_DATA
        else InteractionRiskLevel.LOW
    )
    identity = [
        work.work_id,
        request.request_id,
        work.restoration_attempt_id,
        work.equivalence_assessment_id,
        work.source_capture_id,
        action_type.value,
    ]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    parameters = dict(requirement.parameters) if requirement is not None else {}
    parameters.update(
        {
            "evidence_request_id": request.request_id,
            "restoration_attempt_id": work.restoration_attempt_id,
            "equivalence_assessment_id": work.equivalence_assessment_id,
        }
    )
    base_profile = CaptureProfilePolicy.for_surface(work.surface_kind, action_type)
    if work.surface_kind is not SurfaceKind.WEB:
        try:
            probe_profile = CaptureProfilePolicy.for_app_probe(
                operation=action_type,
                required_artifact_types=tuple(request.required_artifact_types),
            )
        except ValueError:
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BLOCKED,
                reason="App Probe 请求了不受支持的证据类型。",
            )
        after_artifacts = probe_profile.after.artifact_types
        before_artifacts = probe_profile.before.artifact_types
        profile_id = probe_profile.profile_id
    else:
        after_artifacts = tuple(
            dict.fromkeys(
                [
                    *base_profile.before.artifact_types,
                    *request.required_artifact_types,
                    "action_trace",
                ]
            )
        )
        before_artifacts = base_profile.before.artifact_types
        profile_id = f"probe-{action_type.value}-v1"
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"probe-intent:{digest}",
        idempotency_key=f"probe-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=action_type,
        semantic_target=semantic_target,
        parameters=parameters,
        purpose=purpose,
        risk_level=risk,
        capture_profile=CaptureProfile(
            profile_id=profile_id,
            before=CaptureDirective(
                role="before_action"
                if work.surface_kind is not SurfaceKind.WEB
                else "probe_before",
                artifact_types=before_artifacts,
                inherit_parent_semantics=True,
            ),
            after=CaptureDirective(
                role="after_action"
                if work.surface_kind is not SurfaceKind.WEB
                else "probe_result_replay",
                artifact_types=after_artifacts,
            ),
        ),
        deadline=min(work.deadline, request.expires_at),
    )


def _preflight_terminal(
    work: ExplorationWork,
    *,
    now: datetime,
) -> ExplorationTerminal | None:
    if _h5_task_frontier_cleanly_exhausted(work):
        if work.frontier.observation.metadata.get("objective_satisfied") is not True:
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="task_frontier_objective_not_satisfied",
            )
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.COMPLETED,
            reason="H5 Task Frontier 已安全耗尽。",
        )
    if now >= work.deadline:
        reason = "ExplorationWork deadline 已到。"
    elif work.budget.remaining_actions == 0:
        reason = "ExplorationWork remaining_actions 已耗尽。"
    elif work.budget.remaining_duration_ms == 0:
        reason = "ExplorationWork remaining_duration_ms 已耗尽。"
    else:
        return None
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        reason=reason,
    )


def _h5_task_frontier_cleanly_exhausted(work: ExplorationWork) -> bool:
    """只判定页面探索终态，不读取或改写质检规则与 Judge 结论。"""
    metadata = work.frontier.observation.metadata
    payload = metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return False
    try:
        snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    except (TypeError, ValueError):
        return False
    return bool(
        not any(entry.status.value == "selected" for entry in snapshot.entries)
        and snapshot.pending_count == 0
        and h5_frontier_completion_blockers_are_explainable(snapshot)
        and metadata.get("requires_auth") is not True
        and work.frontier.observation.overlay_signature is None
    )


def _eligible_candidates(
    work: ExplorationWork,
) -> tuple[list[CandidateAction], dict[str, str]]:
    if work.surface_kind is not SurfaceKind.WEB:
        return eligible_app_candidates(work)
    eligible: list[CandidateAction] = []
    rejected: dict[str, str] = {}
    blocked_ids = set(work.frontier.blocked_candidate_ids)
    visited_candidate_ids = set(work.frontier.visited_candidate_ids)
    visited_action_fingerprints = set(work.frontier.visited_action_fingerprints)
    for candidate in work.frontier.candidates:
        reason = _candidate_rejection_reason(
            work,
            candidate,
            blocked_ids=blocked_ids,
            visited_candidate_ids=visited_candidate_ids,
            visited_action_fingerprints=visited_action_fingerprints,
        )
        if reason is None:
            eligible.append(candidate)
        else:
            rejected[candidate.candidate_id] = reason
    return eligible, rejected


def _candidate_rejection_reason(
    work: ExplorationWork,
    candidate: CandidateAction,
    *,
    blocked_ids: set[str],
    visited_candidate_ids: set[str],
    visited_action_fingerprints: set[str],
) -> str | None:
    if work.surface_kind is not SurfaceKind.WEB:
        return app_candidate_rejection_reason(
            work,
            candidate,
            blocked_ids=blocked_ids,
            visited_candidate_ids=visited_candidate_ids,
            visited_action_fingerprints=visited_action_fingerprints,
            allowed_operations=_APP_EXPLORATION_OPERATIONS,
        )
    if candidate.candidate_id in blocked_ids:
        return "候选已被 Frontier 确定性阻断"
    if candidate.candidate_id in visited_candidate_ids:
        return "候选 ID 已访问"
    if (
        build_action_fingerprint(
            state_version=work.frontier.observation.state_version,
            candidate=candidate,
        )
        in visited_action_fingerprints
    ):
        return "动作指纹已访问"
    if candidate.action_type not in work.boundaries.allowed_operations:
        return "动作类型不在 allowed_operations"
    if candidate.navigation_depth > min(
        work.budget.max_depth,
        work.boundaries.max_navigation_depth,
    ):
        return "导航深度超出预算或边界"
    if (
        candidate.expected_surface_kind is not None
        and candidate.expected_surface_kind is not work.surface_kind
    ):
        return "候选 Surface 与当前工作不一致"
    location_allowed = _location_allowed(
        work.frontier.observation.location,
        surface_kind=work.surface_kind,
        allowed_hosts=work.boundaries.allowed_hosts,
        allowed_route_prefixes=work.boundaries.allowed_route_prefixes,
    )
    if not location_allowed:
        return "当前 Surface 已超出 host 或 route 边界"
    if candidate.action_type is InteractionOperation.NAVIGATE:
        destination_url = _text(candidate.metadata.get("destination_url"))
        decision = evaluate_h5_navigation_candidate(
            destination_url=destination_url,
            entry_url=work.frontier.observation.location,
            allowed_route_prefixes=work.boundaries.allowed_route_prefixes,
            navigation_depth=candidate.navigation_depth,
            max_depth=min(work.budget.max_depth, work.boundaries.max_navigation_depth),
        )
        if not decision.eligible:
            if decision.reason_code == "h5_navigation_missing_url":
                return f"{decision.reason_code}: navigate 候选缺少平台锁定的 destination_url"
            if decision.reason_code == "h5_navigation_depth_exceeded":
                return f"{decision.reason_code}: 导航深度超出预算或边界"
            return f"{decision.reason_code}: navigate 目标超出 host 或 route 边界"
    if not _side_effect_allowed(work.boundaries.side_effect_policy, candidate):
        return "动作副作用或风险级别超出 side_effect_policy"
    uses_obstruction = _candidate_uses_obstruction_budget(candidate)
    # 仅在已启用清障池时按 obstruction 桶闸门；旧单桶合同清障走业务桶
    charges_obstruction = uses_obstruction and work.budget.resolved_max_obstruction_actions > 0
    if charges_obstruction and work.budget.resolved_remaining_obstruction_actions == 0:
        return "清障动作预算已耗尽"
    if (
        not charges_obstruction
        and candidate.action_type
        not in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
        and work.budget.resolved_remaining_business_actions == 0
    ):
        return "业务动作预算已耗尽"
    return None


def _candidate_uses_obstruction_budget(candidate: CandidateAction) -> bool:
    """与 prepare_after_action 清障分账口径对齐（候选侧仅看 metadata／语义）。"""

    if candidate.metadata.get("package_boundary_recovery") is True:
        # 包边界恢复是广告清障可能触发的必要回程。如果继续占清障池，最后一层
        # 广告恰好耗尽预算时会把 App 永久留在浏览器；改由业务动作池有界承接。
        return False
    if candidate.metadata.get("app_ad_landing_consistency") is True:
        # 首广告落地取证发生在稳定业务页之前，属于广告清障链路的一部分。
        # 若将其计入业务池，业务预算为 0 的纯清障阶段会先把候选拒绝，
        # Surface Phase 随后关闭广告，导致成对证据永久丢失。
        return True
    if candidate.metadata.get("surface_phase_key") is not None:
        return True
    if candidate.metadata.get("phase_key") is not None:
        return True
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    markers = (
        "close_popup",
        "close_ad_popup",
        "close_promo_popup",
        "close_promo_ad",
        "关闭弹窗",
        "关闭弹层",
        "关闭广告",
        "关闭推广",
        "关闭宣传",
        "跳过广告",
        "跳过开屏",
    )
    return any(marker.casefold() in terms for marker in markers)


def _side_effect_allowed(
    policy: SideEffectPolicy,
    candidate: CandidateAction,
) -> bool:
    approval_granted = candidate.metadata.get("approval_granted") is True
    if candidate.risk_level is InteractionRiskLevel.HIGH and not approval_granted:
        return False
    if policy is SideEffectPolicy.PROHIBITED:
        return candidate.action_type in _PROHIBITED_POLICY_OPERATIONS
    if policy is SideEffectPolicy.READ_ONLY:
        return candidate.action_type in _READ_ONLY_OPERATIONS
    if policy is SideEffectPolicy.APPROVAL_REQUIRED:
        return approval_granted
    return True


def _location_allowed(
    location: str,
    *,
    surface_kind: SurfaceKind,
    allowed_hosts: tuple[str, ...],
    allowed_route_prefixes: tuple[str, ...],
) -> bool:
    if surface_kind is not SurfaceKind.WEB:
        parsed = urlsplit(location)
        package_name = (parsed.hostname or "").casefold()
        return bool(package_name) and package_name in {item.casefold() for item in allowed_hosts}
    parsed = urlsplit(location)
    host = (parsed.hostname or "").casefold()
    try:
        _ = parsed.port
    except ValueError:
        return False
    if not host or host not in {item.casefold() for item in allowed_hosts}:
        return False
    return not allowed_route_prefixes or any(
        parsed.path.startswith(prefix) for prefix in allowed_route_prefixes
    )


def _app_coverage_for_agent(coverage: Mapping[str, object]) -> dict[str, Any]:
    """保留完整规则需求，只在模型输入中去掉与其相同的缺口副本。"""

    payload = thaw_json_mapping(coverage)
    rules = payload.get("app_rule_inventory_coverage")
    if isinstance(rules, dict):
        pending = rules.get("pending_rules")
        if isinstance(pending, list) and rules.get("evidence_gaps") == pending:
            rules.pop("evidence_gaps", None)
    return payload


def _agent_input(
    *,
    work: ExplorationWork,
    candidates: list[CandidateAction],
    app_coverage_summary: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """只向模型暴露候选的认知字段，剥离运行时绑定和批准标记。"""
    payload: dict[str, Any] = {
        "protocol_version": work.protocol_version,
        "work_id": work.work_id,
        "task_id": work.task_id,
        "branch_id": work.branch_id,
        "objective": work.objective,
        "surface_kind": work.surface_kind.value,
        "source_capture_id": work.source_capture_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "observation": {
            "capture_id": work.frontier.observation.capture_id,
            "surface_kind": work.frontier.observation.surface_kind.value,
            "location": _public_location(work.frontier.observation.location),
            "state_version": work.frontier.observation.state_version,
            "observed_at": work.frontier.observation.observed_at.isoformat(),
            "page_type": work.frontier.observation.page_type,
            "page_state": work.frontier.observation.page_state,
            "overlay_signature": work.frontier.observation.overlay_signature,
            "key_elements": list(work.frontier.observation.key_elements),
        },
        "visited_state_ids": list(work.frontier.visited_state_ids),
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "semantic_target": item.semantic_target,
                "action_type": item.action_type.value,
                "risk_level": item.risk_level.value,
                "exploration_value": item.exploration_value,
                "navigation_depth": item.navigation_depth,
                "prior_failure_count": item.prior_failure_count,
                "purpose": item.metadata.get("purpose"),
                "expected_state_hint": item.metadata.get("expected_state_hint"),
                "expected_next_page_type": item.metadata.get("expected_next_page_type"),
                "frontier_scope": item.metadata.get("frontier_scope", "local"),
            }
            for item in candidates
        ],
        "budget": work.budget.to_dict(),
        "deadline": work.deadline.isoformat(),
    }
    if work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        if work.app_goal is not None:
            payload["app_goal"] = work.app_goal.to_dict()
        if app_coverage_summary:
            payload["app_coverage"] = _app_coverage_for_agent(app_coverage_summary)
        from services.api.app.application.app_product_capability_for_explorer import (
            capability_facts_for_agent_payload,
        )

        # F04 闭环：V2 Explorer 必读只读能力事实（无坐标授权）。
        payload["app_product_capability_facts"] = capability_facts_for_agent_payload(
            work
        )
        curriculum = _script_curriculum_for_work(work=work)
        if curriculum is not None:
            payload["script_curriculum"] = curriculum
    return payload


def _script_curriculum_for_work(
    *,
    work: ExplorationWork,
) -> dict[str, Any] | None:
    """把 untitled 节奏课纲注入 user payload，供 LLM 按阶段选候选。"""
    from services.api.app.application.app_script_curriculum import (
        AppCurriculumProgress,
        build_app_curriculum_progress,
        build_script_curriculum_payload,
        extract_playbook_id_from_objective,
        resolve_playbook_by_id,
    )

    playbook = resolve_playbook_by_id(extract_playbook_id_from_objective(work.objective))
    if playbook is None:
        return None
    raw_progress = work.frontier.observation.metadata.get("app_curriculum_progress")
    progress = (
        AppCurriculumProgress.from_dict(raw_progress)
        if isinstance(raw_progress, Mapping)
        else build_app_curriculum_progress(playbook)
    )
    return build_script_curriculum_payload(playbook, progress=progress)


def _validated_model_terminal(
    *,
    work: ExplorationWork,
    decision: ExplorerDecisionModel,
    eligible: list[CandidateAction],
) -> ExplorationTerminal:
    status = decision.terminal_status
    if status is None:
        raise ExplorerAgentOutputError("Explorer 终态缺少 terminal_status")
    objective_satisfied = work.frontier.observation.metadata.get("objective_satisfied") is True
    if status is ExplorationTerminalStatus.COMPLETED and not objective_satisfied:
        raise ExplorerAgentOutputError("Explorer 无平台完成信号时不能声明 completed")
    if status is not ExplorationTerminalStatus.COMPLETED and eligible:
        raise ExplorerPrematureTerminalError("仍有可执行候选时 Explorer 不能提前声明非完成终态")
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=status,
        reason=decision.reason or "Explorer 返回结构化终态。",
    )


def _model_terminal_intent(
    *,
    work: ExplorationWork,
    decision: ExplorerDecisionModel,
) -> ExplorationTerminal:
    status = decision.terminal_status
    if status is None:
        raise ExplorerAgentOutputError("Explorer 终态缺少 terminal_status")
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=status,
        reason=decision.reason or "Explorer 返回结构化终态建议。",
    )


def _build_action_intent(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
    purpose: str,
    curriculum_phase_id: str | None = None,
    restore_only: bool = False,
) -> ActionIntent:
    stable_parts = [
        work.work_id,
        work.frontier_snapshot_id,
        work.source_capture_id,
        candidate.candidate_id,
    ]
    digest = hashlib.sha256(
        json.dumps(stable_parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    effective_purpose = purpose
    if work.surface_kind is SurfaceKind.WEB:
        parameters: dict[str, Any] = {}
        destination_url = _text(candidate.metadata.get("destination_url"))
        safe_parameters = candidate.metadata.get("action_parameters")
        if isinstance(safe_parameters, Mapping):
            parameters.update(dict(safe_parameters))
        parameters.update(
            {
                "candidate_id": candidate.candidate_id,
                "frontier_snapshot_id": work.frontier_snapshot_id,
                "navigation_depth": candidate.navigation_depth,
            }
        )
        expected_page_type = _text(candidate.metadata.get("expected_next_page_type"))
        if (
            H5_TASK_FRONTIER_METADATA_KEY in work.frontier.observation.metadata
            and expected_page_type is not None
        ):
            parameters["expected_next_page_type"] = expected_page_type
        if destination_url is not None:
            parameters["destination_url"] = destination_url
    else:
        parameters = {"candidate_id": candidate.candidate_id}
        safe_parameters = candidate.metadata.get("action_parameters")
        if isinstance(safe_parameters, Mapping):
            parameters.update(dict(safe_parameters))
        # 写入目标包：cloud_back 连点后若退桌面可热拉回。
        pkg = work.frontier.observation.metadata.get("package_name")
        if isinstance(pkg, str) and pkg.strip():
            parameters.setdefault("package_name", pkg.strip())
        elif work.app_goal is not None:
            allowed = getattr(work.app_goal, "allowed_hosts", None) or ()
            if allowed:
                host = str(allowed[0]).strip()
                if host:
                    parameters.setdefault("package_name", host)
        if curriculum_phase_id is not None:
            parameters["app_curriculum_phase_id"] = curriculum_phase_id
        if restore_only:
            parameters["app_frontier_restore_only"] = True
        curriculum_observation = candidate.metadata.get("curriculum_observation")
        if candidate.action_type is InteractionOperation.OBSERVE and isinstance(
            curriculum_observation, Mapping
        ):
            parameters["app_curriculum_observation"] = dict(curriculum_observation)
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        dynamic_startup_observation = candidate.metadata.get("dynamic_startup_observation")
        if candidate.action_type is InteractionOperation.OBSERVE and isinstance(
            dynamic_startup_observation, Mapping
        ):
            parameters["app_dynamic_startup_observation"] = dict(dynamic_startup_observation)
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        unstable_reobserve = candidate.metadata.get("dynamic_unstable_page_reobserve")
        if (
            candidate.action_type is InteractionOperation.OBSERVE
            and isinstance(unstable_reobserve, Mapping)
        ):
            parameters["app_dynamic_unstable_page_reobserve"] = dict(unstable_reobserve)
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        unstable_back = candidate.metadata.get("dynamic_unstable_page_back")
        if (
            candidate.action_type is InteractionOperation.BACK
            and isinstance(unstable_back, Mapping)
        ):
            parameters["app_dynamic_unstable_page_back"] = dict(unstable_back)
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        curriculum_action = candidate.metadata.get("curriculum_action")
        if candidate.action_type is InteractionOperation.SCROLL and isinstance(
            curriculum_action, Mapping
        ):
            parameters["app_curriculum_action"] = dict(curriculum_action)
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        dynamic_page_sampling = candidate.metadata.get("dynamic_page_sampling_contract")
        if candidate.action_type is InteractionOperation.SCROLL and isinstance(
            dynamic_page_sampling, Mapping
        ):
            parameters["app_dynamic_page_sampling"] = dict(dynamic_page_sampling)
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        attach_synthetic_touch_contracts(parameters, candidate.metadata)
        # HJLL 已由产品专属编排器接管：这里不让通用 Runtime 再依据语义、
        # OCR 或历史 sibling 改写已签发的安全点。Driver 会按 Airtest 节奏
        # 等待页面稳定，并在外跳后把前台拉回目标包；通用层只负责证据和账本。
        if _is_hjll_airtest_work(work):
            package_name = _hjll_expected_package(work)
            if package_name:
                parameters["hjll_orchestrated"] = True
                parameters["hjll_expected_package"] = package_name
                parameters["hjll_settle_after_action_ms"] = (
                    1_700
                    if candidate.metadata.get("forced_pixel_popup_close") is True
                    or candidate.metadata.get("popup_close_airtest_fallback") is True
                    else 1_500
                )
        # 分栏目：必须写入意图参数，强制走点击+前后截图留证，不得只观察。
        if candidate.metadata.get("home_category_grid_seek") is True:
            parameters["home_category_grid_seek"] = True
            parameters["require_screenshot_evidence"] = True
            parameters["home_category_grid_evidence"] = "click_and_screenshot"
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
            elif "截图留证" not in effective_purpose:
                label = str(candidate.semantic_target or "").strip() or "分栏目入口"
                effective_purpose = f"点击分栏目入口「{label}」并截图留证"
        if candidate.metadata.get("home_top_category_tab_seek") is True:
            parameters["home_top_category_tab_seek"] = True
            parameters["require_screenshot_evidence"] = True
            flow = work.frontier.observation.metadata.get("app_flow_contract")
            if isinstance(flow, Mapping):
                parameters["app_flow_contract"] = dict(flow)
            parameters["coverage_metadata"] = {
                key: work.frontier.observation.metadata[key]
                for key in (
                    "app_flow_contract",
                    "visited_home_top_category_tabs",
                    "home_top_category_sample_label",
                    "home_top_category_sample_phase",
                    "package_name",
                    "surface_facts",
                    "page_type",
                    "business_page_type",
                    "visual_summary",
                    "dynamic_home_popup_frame_count",
                    "dynamic_business_tab_overlay_frame_count",
                    "clearance_without_business_progress_count",
                    "dismiss_noop_streak",
                    "surface_phase_same_popup_attempt_count",
                )
                if key in work.frontier.observation.metadata
            }
            if "page_type" not in parameters["coverage_metadata"]:
                parameters["coverage_metadata"]["page_type"] = (
                    work.frontier.observation.page_type
                )
            if "normalized_bounds" in candidate.metadata:
                parameters["normalized_bounds"] = candidate.metadata["normalized_bounds"]
            if "expected_state_hint" in candidate.metadata:
                parameters["expected_state_hint"] = candidate.metadata[
                    "expected_state_hint"
                ]
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
            elif "上方分类" not in effective_purpose:
                label = str(candidate.semantic_target or "").strip() or "上方分类"
                effective_purpose = f"点击主页上方分类「{label}」并截图留证"
        if candidate.metadata.get("home_content_entry_seek") is True:
            parameters["home_content_entry_seek"] = True
            parameters["require_screenshot_evidence"] = True
            if "normalized_bounds" in candidate.metadata:
                parameters["normalized_bounds"] = candidate.metadata["normalized_bounds"]
            if "expected_state_hint" in candidate.metadata:
                parameters["expected_state_hint"] = candidate.metadata[
                    "expected_state_hint"
                ]
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        if candidate.metadata.get("home_top_category_sample_seek") is True:
            parameters["home_top_category_sample_seek"] = True
            parameters["require_screenshot_evidence"] = True
            if candidate.metadata.get("home_top_category_sample_phase") is not None:
                parameters["home_top_category_sample_phase"] = candidate.metadata[
                    "home_top_category_sample_phase"
                ]
            if candidate.metadata.get("home_top_category_sample_label") is not None:
                parameters["home_top_category_sample_label"] = candidate.metadata[
                    "home_top_category_sample_label"
                ]
            match_texts = candidate.metadata.get("home_top_category_sample_match_texts")
            if isinstance(match_texts, (list, tuple)):
                parameters["home_top_category_sample_match_texts"] = [
                    str(item).strip() for item in match_texts if str(item or "").strip()
                ]
            parameters["coverage_metadata"] = {
                key: work.frontier.observation.metadata[key]
                for key in (
                    "app_flow_contract",
                    "visited_home_top_category_tabs",
                    "home_top_category_sample_label",
                    "home_top_category_sample_phase",
                    "package_name",
                    "surface_facts",
                    "page_type",
                    "business_page_type",
                    "visual_summary",
                    "dynamic_home_popup_frame_count",
                    "dynamic_business_tab_overlay_frame_count",
                    "clearance_without_business_progress_count",
                    "dismiss_noop_streak",
                    "surface_phase_same_popup_attempt_count",
                )
                if key in work.frontier.observation.metadata
            }
            if "normalized_bounds" in candidate.metadata:
                parameters["normalized_bounds"] = candidate.metadata["normalized_bounds"]
            if "expected_state_hint" in candidate.metadata:
                parameters["expected_state_hint"] = candidate.metadata[
                    "expected_state_hint"
                ]
            if (
                candidate.metadata.get("home_top_sample_pixel_visual") is True
                or candidate.metadata.get("forced_pixel_home_top_sample") is True
                or str(candidate.candidate_id or "").startswith(
                    "forced-pixel-home-top-sample-"
                )
            ):
                parameters["home_top_sample_pixel_visual"] = True
                parameters["forced_pixel_home_top_sample"] = True
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            parameters["bottom_navigation_tab_seek"] = True
            parameters["require_screenshot_evidence"] = True
            # Runtime 硬闸：弹窗未满足不得执行底栏（禁止假 completed）。
            flow = work.frontier.observation.metadata.get("app_flow_contract")
            if isinstance(flow, Mapping):
                parameters["app_flow_contract"] = dict(flow)
            parameters["coverage_metadata"] = {
                key: work.frontier.observation.metadata[key]
                for key in ("app_flow_contract", "visited_bottom_navigation_labels")
                if key in work.frontier.observation.metadata
            }
            raw_bounds = candidate.metadata.get("normalized_bounds")
            if isinstance(raw_bounds, Mapping):
                parameters["normalized_bounds"] = dict(raw_bounds)
            if candidate.metadata.get("forced_pixel_bottom_nav") is True or str(
                candidate.candidate_id or ""
            ).startswith("forced-pixel-bottom-nav:"):
                parameters["forced_pixel_bottom_nav"] = True
            hint = candidate.metadata.get("expected_state_hint")
            if isinstance(hint, str) and hint.strip():
                parameters["expected_state_hint"] = hint.strip()
            canonical_purpose = _text(candidate.metadata.get("purpose"))
            if canonical_purpose is not None:
                effective_purpose = canonical_purpose
            elif "截图留证" not in effective_purpose:
                label = str(candidate.semantic_target or "").strip() or "底栏入口"
                effective_purpose = f"点击底栏「{label}」并截图留证"
        if candidate.metadata.get("short_drama_top_left_back") is True:
            parameters["short_drama_top_left_back"] = True
            parameters["post_detail_exit_prefer_tabs"] = True
            parameters["forced_detail_exit_back"] = True
            raw_bounds = candidate.metadata.get("normalized_bounds")
            if isinstance(raw_bounds, Mapping):
                parameters["normalized_bounds"] = dict(raw_bounds)
        if candidate.metadata.get("short_drama_in_tab_form_exit") is True:
            parameters["short_drama_in_tab_form_exit"] = True
            parameters["post_detail_exit_prefer_tabs"] = True
            if candidate.metadata.get("short_drama_playback_finished_exit") is True:
                parameters["short_drama_playback_finished_exit"] = True
        if candidate.metadata.get("anwang_membership_landing_exit") is True:
            parameters["anwang_membership_landing_exit"] = True
            parameters["post_detail_exit_prefer_tabs"] = True
        if candidate.metadata.get("profile_credential_dismiss") is True:
            parameters["profile_credential_dismiss"] = True
            raw_bounds = candidate.metadata.get("normalized_bounds")
            if isinstance(raw_bounds, Mapping):
                parameters["normalized_bounds"] = dict(raw_bounds)
        if candidate.metadata.get("profile_credential_save") is True:
            parameters["profile_credential_save"] = True
            raw_bounds = candidate.metadata.get("normalized_bounds")
            if isinstance(raw_bounds, Mapping):
                parameters["normalized_bounds"] = dict(raw_bounds)
        if candidate.action_type is InteractionOperation.SCROLL:
            hint = str(candidate.metadata.get("expected_state_hint") or "")
            parameters["direction"] = "down"
            if "swipe_left_edge" in hint or "左缘" in (candidate.semantic_target or ""):
                parameters["direction"] = "down"
    capture_profile = CaptureProfilePolicy.for_surface(
        work.surface_kind,
        candidate.action_type,
    )
    # 开屏／动态启动首帧 OBSERVE：一次真实连拍写 temporal_trace，供 spec_37 等吃加载态
    if (
        work.surface_kind is not SurfaceKind.WEB
        and candidate.action_type is InteractionOperation.OBSERVE
    ):
        want_startup_timed = False
        want_weak_network_approx = False
        curriculum_observation = candidate.metadata.get("curriculum_observation")
        if isinstance(curriculum_observation, Mapping):
            phase_id = str(curriculum_observation.get("phase_id") or "").casefold()
            phase_action_count = curriculum_observation.get("phase_action_count")
            first_frame = (
                isinstance(phase_action_count, int)
                and not isinstance(phase_action_count, bool)
                and phase_action_count == 0
            )
            if first_frame and any(
                token in phase_id
                for token in (
                    "startup",
                    "splash",
                    "countdown",
                    "age_gate",
                    "开屏",
                    "启动",
                )
            ):
                want_startup_timed = True
            if first_frame and any(
                token in phase_id
                for token in (
                    "line_loading",
                    "loading",
                    "weak_network",
                    "弱网",
                    "加载",
                )
            ):
                want_weak_network_approx = True
        dynamic_startup_observation = candidate.metadata.get("dynamic_startup_observation")
        if isinstance(dynamic_startup_observation, Mapping):
            raw_count = dynamic_startup_observation.get("observation_count")
            if (
                isinstance(raw_count, int)
                and not isinstance(raw_count, bool)
                and raw_count == 0
            ):
                want_startup_timed = True
        if want_startup_timed or want_weak_network_approx:
            after_types = list(capture_profile.after.artifact_types)
            if "temporal_trace" not in after_types:
                after_types.append("temporal_trace")
            if "ui_snapshot" not in after_types:
                after_types.append("ui_snapshot")
            if "visible_text" not in after_types:
                after_types.append("visible_text")
            profile_id = (
                "app-observe-weak_network_approx-timed_capture-v1"
                if want_weak_network_approx
                else "app-observe-startup-timed_capture-v1"
            )
            capture_profile = CaptureProfile(
                profile_id=profile_id,
                before=capture_profile.before,
                after=CaptureDirective(
                    role="after_action",
                    artifact_types=tuple(after_types),
                    inherit_parent_semantics=capture_profile.after.inherit_parent_semantics,
                ),
            )
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters=parameters,
        purpose=effective_purpose,
        risk_level=_effective_action_risk(candidate),
        capture_profile=capture_profile,
        deadline=work.deadline,
    )


def _effective_action_risk(candidate: CandidateAction) -> InteractionRiskLevel:
    """抬升至服务端动作最低风险，避免桌面热恢复等自报 LOW 被 CommandPolicy 拒。"""

    minimum = SERVER_MINIMUM_RISK_BY_OPERATION.get(candidate.action_type)
    if minimum is None:
        return candidate.risk_level
    order = {
        InteractionRiskLevel.LOW: 0,
        InteractionRiskLevel.MEDIUM: 1,
        InteractionRiskLevel.HIGH: 2,
    }
    if order.get(candidate.risk_level, 0) < order.get(minimum, 0):
        return minimum
    return candidate.risk_level


def _bounded_reason(reasons: Mapping[str, str]) -> str:
    if not reasons:
        return "当前 Frontier 没有可执行候选。"
    compact = "；".join(f"{key}: {value}" for key, value in sorted(reasons.items()))
    return f"当前 Frontier 候选均被确定性边界拒绝：{compact}"[:2_000]


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _public_location(location: str) -> str:
    """移除 URL 凭证、查询和片段，避免把敏感导航数据送入模型。"""
    parsed = urlsplit(location)
    if not parsed.scheme or not parsed.hostname:
        return location
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
