"""迁移期 V1 Main Agent 工厂：维持现网运行，禁止作为 V2 目标架构扩展。"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agent_framework import (
    Agent,
    AgentRunInputs,
    AgentSession,
    ContextProvider,
    FunctionInvocationContext,
    FunctionMiddleware,
    InMemoryHistoryProvider,
    MiddlewareTermination,
    SupportsChatGetResponse,
)
from agent_framework.exceptions import ChatClientException
from openai import OpenAIError

from services.api.app.agents.runtime.action_trace import (
    ActionTraceMiddleware,
    ActionTraceSink,
    InMemoryActionTraceSink,
)
from services.api.app.agents.runtime.business_middlewares.factory import (
    create_default_business_middlewares,
)
from services.api.app.agents.runtime.cancellation import CancellationCheckMiddleware
from services.api.app.agents.runtime.loop_guard import LoopGuardConfig, LoopGuardMiddleware
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
    UsageSnapshot,
)
from services.api.app.agents.runtime.retry import RetryConfig, RetryMiddleware
from services.api.app.agents.runtime.run_context import get_agent_run_context
from services.api.app.agents.runtime.usage_meter import (
    bind_run_llm_usage_meter,
    normalize_llm_usage_tokens,
)
from services.api.app.agents.runtime.work_dispatch_boundary import (
    WorkDispatchBoundaryMiddleware,
)
from services.api.app.agents.runtime.work_dispatcher import (
    WorkDispatch,
    prepare_page_rule_dispatch,
    prepare_work_dispatch,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.agents.tools.registry import default_registry
from services.api.app.core.config import AgentRuntimeSettings, get_settings

logger = logging.getLogger(__name__)

LEGACY_V1_MAIN_AGENT_NAME = "MainAgent"
DEFAULT_MAIN_AGENT_NAME = LEGACY_V1_MAIN_AGENT_NAME
MAX_EMPTY_TASK_CONTINUATIONS = 2
MAX_RETRYABLE_PROVIDER_CONTINUATIONS = 2
PROVIDER_RETRY_BACKOFF_SECONDS = 0.5
PROVIDER_OVERLOAD_RETRY_BACKOFF_SECONDS = 2.0
RETRYABLE_PROVIDER_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_PROVIDER_HTTP_STATUS_PATTERN = re.compile(
    r"(?:error\s+code|status(?:_code)?|http(?:/\d(?:\.\d)?)?)\s*[:=]?\s*['\"\\]*([1-5]\d{2})\b",
    re.IGNORECASE,
)


@dataclass
class AgentRunScope:
    """一次受监督 Main Agent Run 共享的会话、记忆、留痕和累计输出。"""

    session: AgentSession
    working_memory: WorkingMemory
    trace_sink: ActionTraceSink
    middleware: list[FunctionMiddleware]
    dispatch_boundary: WorkDispatchBoundaryMiddleware | None = None
    final_text: str | None = None
    usage: UsageSnapshot | None = None
    allow_optimized_pipeline: bool = True


def build_main_agent(
    *,
    client: SupportsChatGetResponse,
    instructions: str,
    runtime_settings: AgentRuntimeSettings,
    tools: Sequence[Callable[..., Any]] | None = None,
    context_providers: Sequence[ContextProvider] | None = None,
    name: str = DEFAULT_MAIN_AGENT_NAME,
) -> Agent:
    """构建 V1 Main Agent。

    会在 client 上写入 ``function_invocation_configuration.max_iterations``，作为
    框架自带的大模型往返轮次防护。网关兼容客户端每次受监督派发只执行一批 Tool，
    由 ``WorkDispatchBoundaryMiddleware`` 在真实 Tool 返回后立即结束本轮。
    禁止使用框架 ``max_function_calls`` 充当派发边界；该预算可能随会话残留，
    导致下一轮指定 Tool 在执行前被静默禁用。硬性步数上限由
    ``run_main_agent`` 每次运行时新建的 ``LoopGuardMiddleware`` 在执行前预占并强制执行；
    页面总量未知时可按已发现工作扩展预算，但始终受系统绝对硬上限约束。

    Agent 对象可跨多次 Run 复用；每次 Run 专属的会话、工作记忆与护栏中间件由
    ``run_main_agent`` 单独创建并按次绑定。只读 Skill Tool 的免审批策略由
    ``AliothSkillsProvider`` 负责，脚本执行仍保留人工审批边界。
    """
    invocation_config = client.function_invocation_configuration
    invocation_config["max_iterations"] = runtime_settings.max_iterations
    invocation_config.pop("max_function_calls", None)

    providers: list[ContextProvider] = [InMemoryHistoryProvider()]
    providers.extend(context_providers or [])
    default_options = {"store": False} if getattr(client, "STORES_BY_DEFAULT", False) else None

    return Agent(
        client=client,
        instructions=instructions,
        name=name,
        tools=list(tools) if tools else None,
        default_options=default_options,
        context_providers=providers,
        require_per_service_call_history_persistence=True,
    )


async def run_main_agent(
    agent: Agent,
    messages: AgentRunInputs,
    *,
    runtime_settings: AgentRuntimeSettings,
    sink: ActionTraceSink | None = None,
    extra_middleware: Sequence[FunctionMiddleware] | None = None,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    retry_config: RetryConfig | None = None,
) -> RunResult:
    """执行迁移期 V1 Main Agent。"""
    with bind_run_llm_usage_meter() as usage_meter:
        result = await _run_main_agent_impl(
            agent,
            messages,
            runtime_settings=runtime_settings,
            sink=sink,
            extra_middleware=extra_middleware,
            is_cancelled=is_cancelled,
            retry_config=retry_config,
        )
        metered_usage = usage_meter.snapshot()
        if metered_usage is not None:
            result.usage = metered_usage
        return result


async def _run_main_agent_impl(
    agent: Agent,
    messages: AgentRunInputs,
    *,
    runtime_settings: AgentRuntimeSettings,
    sink: ActionTraceSink | None = None,
    extra_middleware: Sequence[FunctionMiddleware] | None = None,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    retry_config: RetryConfig | None = None,
) -> RunResult:
    """执行一次 Main Agent Run：绑定全新的工作记忆与护栏中间件，并统一处理墙钟超时。

    中间件顺序（外层到内层）：``[trace, guard, cancellation(可选), *extra_middleware,
    audit, ruleset_lock, task_status_gate, rule_judge_handoff, evidence, dedupe,
    page_inspection_memory, scoped_tool_failure, publish_condition, retry]``。只读 Skill
    工具的免审批策略在
    ``agents.skills.loader`` 中配置，
    运行时不再通过审批响应绕行加载 Skill。``extra_middleware``（如权限校验
    `PermissionEnforcementMiddleware`）
    放在 ``LoopGuardMiddleware``/``CancellationCheckMiddleware`` 之后（更内层）：必须
    保证硬性步数上限/卡死检测/取消检查在每一次调用尝试时都无条件优先执行，不会因为
    某个更内层中间件"软阻断"（不调用 call_next）而被绕过。业务中间件位于权限校验
    之后，保证先完成权限兜底，再执行任务域门禁与发布前检查。``RetryMiddleware`` 必须
    是最内层，使重试对外层所有中间件都透明（详见各模块文档字符串与仓库记忆）。

    ``is_cancelled``：可选的异步回调，返回 True 表示该次 Run 已被外部请求取消；
    为 None 时不启用取消检查（如调试端点场景，不需要跨进程取消能力）。
    """
    working_memory = WorkingMemory()
    entry_url = _entry_url_from_inputs(messages)
    if entry_url is not None:
        working_memory.seed_entry_exploration_action(
            action="goto",
            target=entry_url,
            navigation_depth=0,
        )
    trace_sink = sink if sink is not None else InMemoryActionTraceSink()
    trace_middleware = ActionTraceMiddleware(working_memory, trace_sink)
    guard_middleware = LoopGuardMiddleware(
        working_memory,
        LoopGuardConfig(
            max_steps=runtime_settings.max_steps,
            stuck_window=runtime_settings.stuck_window,
            adaptive_page_budget=runtime_settings.adaptive_page_budget,
            max_steps_hard_cap=runtime_settings.max_steps_hard_cap,
            page_budget_base_steps=runtime_settings.page_budget_base_steps,
            page_budget_steps_per_page=runtime_settings.page_budget_steps_per_page,
        ),
    )
    retry_middleware = RetryMiddleware(
        retry_config
        if retry_config is not None
        else RetryConfig(
            max_attempts=runtime_settings.retry_max_attempts,
            backoff_seconds=runtime_settings.retry_backoff_seconds,
        ),
    )
    middleware: list[FunctionMiddleware] = []
    dispatch_boundary = None
    if getattr(agent.client, "SUPERVISED_SINGLE_TOOL_DISPATCH", False):
        dispatch_boundary = WorkDispatchBoundaryMiddleware()
        middleware.append(dispatch_boundary)
    # 派发边界必须位于追踪和步数护栏之前：错误 Tool 连业务
    # 调用都不构成，不应污染审计轨迹或消耗页面探索步数。
    middleware.extend([trace_middleware, guard_middleware])

    if is_cancelled is not None:
        middleware.append(CancellationCheckMiddleware(working_memory, is_cancelled))
    for item in extra_middleware or []:
        bind_working_memory = getattr(item, "bind_working_memory", None)
        if bind_working_memory is not None:
            bind_working_memory(working_memory)
    middleware.extend(extra_middleware or [])
    middleware.extend(create_default_business_middlewares(working_memory=working_memory))
    middleware.append(retry_middleware)

    run_context = get_agent_run_context()
    scope = AgentRunScope(
        session=AgentSession(),
        working_memory=working_memory,
        trace_sink=trace_sink,
        middleware=middleware,
        dispatch_boundary=dispatch_boundary,
        allow_optimized_pipeline=(run_context is not None and bool(run_context.task_id)),
    )

    try:
        await asyncio.wait_for(
            _run_supervised_agent(
                agent=agent,
                messages=messages,
                scope=scope,
                runtime_settings=runtime_settings,
            ),
            timeout=runtime_settings.max_duration_seconds,
        )
    except TimeoutError:
        working_memory.termination_reason = "timeout"
        working_memory.terminalize_remaining_work(
            failure_stage="run_timeout",
            failure_code="RUN_TIMEOUT",
            failure_message="任务达到墙钟超时，剩余页面工作已转人工复核。",
        )
        logger.warning(
            "Main Agent run 超时（%.0fs），已强制中断。", runtime_settings.max_duration_seconds
        )
        return RunResult(
            termination_reason=TerminationReason.TIMEOUT,
            working_memory=working_memory,
            trace_sink=trace_sink,
            final_text=scope.final_text,
            usage=scope.usage,
            execution_outcome=_execution_outcome(working_memory),
        )
    except (OpenAIError, ChatClientException) as exc:
        failure_code, failure_message = _provider_failure_diagnostic(exc)
        working_memory.termination_reason = "llm_provider_error"
        working_memory.terminalize_remaining_work(
            failure_stage="llm_provider",
            failure_code=failure_code,
            failure_message=failure_message,
        )
        logger.exception("Main Agent run 出现大模型供应商 API 异常。")
        return RunResult(
            termination_reason=TerminationReason.ERROR,
            working_memory=working_memory,
            trace_sink=trace_sink,
            final_text=scope.final_text,
            error_message=failure_message,
            error_category=ErrorCategory.LLM_PROVIDER,
            usage=scope.usage,
            execution_outcome=_execution_outcome(working_memory),
        )
    except Exception as exc:  # noqa: BLE001 - 顶层编排需要兜底，转成结构化结果而不是让异常裸奔到调用方
        working_memory.termination_reason = "run_error"
        working_memory.terminalize_remaining_work(
            failure_stage="run_error",
            failure_code="UNEXPECTED_RUN_ERROR",
            failure_message=str(exc),
        )
        logger.exception("Main Agent run 出现未预期异常。")
        return RunResult(
            termination_reason=TerminationReason.ERROR,
            working_memory=working_memory,
            trace_sink=trace_sink,
            final_text=scope.final_text,
            error_message=str(exc),
            error_category=ErrorCategory.UNKNOWN,
            usage=scope.usage,
            execution_outcome=_execution_outcome(working_memory),
        )

    final_text = scope.final_text
    usage = scope.usage
    if working_memory.step_count == 0 and not (final_text or "").strip():
        return RunResult(
            termination_reason=TerminationReason.ERROR,
            working_memory=working_memory,
            trace_sink=trace_sink,
            final_text=final_text,
            error_message="Agent 未返回文本且未执行任何工具，不能判定为完成。",
            error_category=ErrorCategory.UNKNOWN,
            usage=usage,
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )

    termination_reason = _classify_termination(working_memory)
    return RunResult(
        termination_reason=termination_reason,
        working_memory=working_memory,
        trace_sink=trace_sink,
        final_text=final_text,
        error_message=(
            "连续基础设施故障达到阈值，任务已停止并保留页面终态。"
            if termination_reason == TerminationReason.INFRASTRUCTURE_UNAVAILABLE
            else None
        ),
        error_category=(
            ErrorCategory.INFRASTRUCTURE
            if termination_reason == TerminationReason.INFRASTRUCTURE_UNAVAILABLE
            else None
        ),
        usage=usage,
        execution_outcome=_execution_outcome(working_memory),
    )


async def _run_supervised_agent(
    *,
    agent: Agent,
    messages: AgentRunInputs,
    scope: AgentRunScope,
    runtime_settings: AgentRuntimeSettings,
) -> None:
    """管理页级后台任务，并确保超时或取消时不遗留判定协程。"""
    background_tasks: dict[str, asyncio.Task[None]] = {}
    try:
        await _run_supervised_agent_loop(
            agent=agent,
            messages=messages,
            scope=scope,
            runtime_settings=runtime_settings,
            background_tasks=background_tasks,
        )
    finally:
        for task in background_tasks.values():
            if not task.done():
                task.cancel()
        if background_tasks:
            await asyncio.gather(
                *background_tasks.values(),
                return_exceptions=True,
            )


async def _run_supervised_agent_loop(
    *,
    agent: Agent,
    messages: AgentRunInputs,
    scope: AgentRunScope,
    runtime_settings: AgentRuntimeSettings,
    background_tasks: dict[str, asyncio.Task[None]],
) -> None:
    """使用结构化记忆和页面级紧凑会话续跑，直到收敛或命中安全边界。"""
    dispatch = prepare_work_dispatch(scope.working_memory)
    current_messages = _with_initial_dispatch_instruction(messages, dispatch)
    first_turn = True
    force_agent_dispatch = False
    session_page_capture_id: str | None = None
    optimization = get_settings().quality_optimization
    background_rule_gate = asyncio.Semaphore(
        max(1, int(getattr(optimization, "semantic_concurrency", 2)))
    )
    while True:
        optimized_mode = optimization.pipeline_mode == "on" and scope.allow_optimized_pipeline
        for page_capture_id, task in list(background_tasks.items()):
            if not task.done():
                continue
            task.result()
            background_tasks.pop(page_capture_id, None)
        if optimized_mode and dispatch is None and background_tasks:
            await asyncio.wait(
                background_tasks.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            dispatch = prepare_work_dispatch(scope.working_memory)
            current_messages = _build_continuation_message(
                scope.working_memory,
                dispatch=dispatch,
            )
            continue
        if (
            optimized_mode
            and dispatch is None
            and not background_tasks
            and not scope.working_memory.has_runnable_work()
            and not _bound_task_missing_page_capture(scope.working_memory)
        ):
            # 优化流水线已经由结构化 Tool 完成全部页面工作时直接收口。过去这里还会
            # 为生成一段结束文本额外请求 Main Agent；供应商短暂不可用会把一张已经
            # 完成质检的任务错误标成失败，也会平白增加一次模型往返。
            return
        if (
            optimized_mode
            and dispatch is None
            and not background_tasks
            and scope.working_memory.has_runnable_work()
            and not _bound_task_missing_page_capture(scope.working_memory)
        ):
            scope.working_memory.termination_reason = "page_work_undispatchable"
            scope.working_memory.terminalize_remaining_work(
                failure_stage="agent_dispatch",
                failure_code="PAGE_WORK_UNDISPATCHABLE",
                failure_message="页面工作仍未完成，但结构化派发器已无法生成下一步动作，已转人工复核。",
            )
            return

        current_page_capture_id = _current_page_capture_id(scope.working_memory)
        reset_for_page_boundary = False
        if first_turn:
            first_turn = False
        elif (dispatch is not None and dispatch.kind == "page_exploration") or (
            current_page_capture_id is not None
            and session_page_capture_id is not None
            and current_page_capture_id != session_page_capture_id
        ):
            # 页面总量动态增长时，跨页面保留完整聊天历史会随 DOM、规则和 Tool
            # 结果呈平方级膨胀；但同一页面的候选规则、Skill 和判定上下文仍需保留。
            # 因此仅在页面边界重建会话，页内继续复用历史。
            scope.session = AgentSession()
            session_page_capture_id = None
            reset_for_page_boundary = True
        progress_before = scope.working_memory.page_progress_signature()
        if (
            optimized_mode
            and not force_agent_dispatch
            and dispatch is not None
            and dispatch.kind == "rule_judge"
            and dispatch.session_dependency == "snapshot"
            and dispatch.arguments is not None
        ):
            page_capture_id = str(dispatch.arguments.get("page_capture_id") or "").strip()
            if page_capture_id and scope.working_memory.defer_snapshot_page_inspection(
                page_capture_id=page_capture_id
            ):
                background_tasks[page_capture_id] = asyncio.create_task(
                    _run_background_snapshot_page(
                        page_capture_id=page_capture_id,
                        initial_dispatch=dispatch,
                        scope=scope,
                        rule_judge_gate=background_rule_gate,
                    ),
                    name=f"alioth-snapshot-judge-{page_capture_id}",
                )
                dispatch = prepare_work_dispatch(scope.working_memory)
                current_messages = _build_continuation_message(
                    scope.working_memory,
                    dispatch=dispatch,
                )
                continue
        if (
            optimized_mode
            and not force_agent_dispatch
            and dispatch is not None
            and dispatch.tool_name is not None
            and dispatch.arguments is not None
            and dispatch.kind
            in {
                "page_exploration",
                "page_detection",
                "page_redetection",
                "rule_matching",
                "rule_judge",
                "rule_write",
            }
        ):
            await _run_direct_dispatch(dispatch=dispatch, scope=scope)
            if scope.working_memory.termination_reason is not None:
                # 直接派发同样经过 LoopGuardMiddleware。命中硬上限时，中间件会
                # 锁存 termination_reason 并阻断 Tool；此处必须立即收口。若继续
                # 进入 Agent 歧义处理，供应商异常隔离分支会反复选择同一 Frontier，
                # 造成任务停在 99% 并产生无意义的模型重试与审计轨迹。
                _terminalize_guarded_work(scope.working_memory)
                return
            if scope.working_memory.page_progress_signature() != progress_before:
                scope.working_memory.reset_continuation_count()
                dispatch = prepare_work_dispatch(scope.working_memory)
                current_messages = _build_continuation_message(
                    scope.working_memory,
                    dispatch=dispatch,
                )
                continue
            # 结构化执行未推进时交给 Agent 做一次歧义处理，不在直接执行器内死循环。
            force_agent_dispatch = True

        if scope.dispatch_boundary is not None:
            expected_tool_name = (
                str(dispatch.tool_choice.get("required_function_name") or "").strip()
                if dispatch is not None
                else ""
            )
            scope.dispatch_boundary.begin_dispatch(expected_tool_name or None)
        try:
            response = await _run_agent_with_provider_retry(
                agent=agent,
                messages=current_messages,
                middleware=scope.middleware,
                session=scope.session,
                options=({"tool_choice": dispatch.tool_choice} if dispatch is not None else None),
            )
        except (OpenAIError, ChatClientException) as exc:
            failure_code, failure_message = _provider_failure_diagnostic(exc)
            if not (
                optimized_mode
                and dispatch is not None
                and _isolate_optimized_provider_failure(
                    working_memory=scope.working_memory,
                    dispatch=dispatch,
                    failure_code=failure_code,
                    failure_message=failure_message,
                )
            ):
                raise
            # 优化模式下参数明确的 Tool 已先经过直接执行；只有未产生进展时才会
            # 进入本次 Agent 歧义处理。供应商重试耗尽后隔离当前工作单元，重建会话
            # 并继续剩余结构化页面，禁止把一次模型线路故障扩散成全任务终止。
            logger.warning(
                "Main Agent %s 已隔离当前 %s 工作单元，继续剩余结构化页面。",
                failure_code,
                dispatch.kind,
            )
            force_agent_dispatch = False
            scope.session = AgentSession()
            session_page_capture_id = None
            scope.working_memory.reset_continuation_count()
            dispatch = prepare_work_dispatch(scope.working_memory)
            current_messages = _build_continuation_message(
                scope.working_memory,
                dispatch=dispatch,
            )
            continue
        force_agent_dispatch = False
        page_after_run = _current_page_capture_id(scope.working_memory)
        if page_after_run is not None and (
            session_page_capture_id is None
            or page_after_run == current_page_capture_id
            or reset_for_page_boundary
        ):
            session_page_capture_id = page_after_run
        response_text = response.text if response is not None else None
        if (response_text or "").strip():
            scope.final_text = response_text
        scope.usage = _merge_usage(scope.usage, _extract_usage(response))
        progress_changed = scope.working_memory.page_progress_signature() != progress_before
        if progress_changed:
            scope.working_memory.reset_continuation_count()
            if optimized_mode:
                scope.session = AgentSession()
                session_page_capture_id = None

        if scope.working_memory.termination_reason is not None:
            _terminalize_guarded_work(scope.working_memory)
            return
        if (
            scope.working_memory.consecutive_infrastructure_failures
            >= runtime_settings.max_consecutive_infrastructure_failures
        ):
            scope.working_memory.termination_reason = "infrastructure_unavailable"
            scope.working_memory.terminalize_remaining_work(
                failure_stage="infrastructure",
                failure_code="INFRASTRUCTURE_UNAVAILABLE",
                failure_message="连续基础设施故障达到阈值，剩余页面工作无法可靠执行。",
            )
            return
        bound_task_missing_page = _bound_task_missing_page_capture(scope.working_memory)
        if not scope.working_memory.has_runnable_work() and not bound_task_missing_page:
            return
        continuation_limit = (
            min(
                runtime_settings.max_continuation_attempts,
                MAX_EMPTY_TASK_CONTINUATIONS,
            )
            if bound_task_missing_page
            else runtime_settings.max_continuation_attempts
        )
        if scope.working_memory.continuation_count >= continuation_limit:
            blocked_transition_ids = scope.working_memory.blocked_mandatory_transition_action_ids()
            degraded_page_ids = scope.working_memory.degrade_mandatory_action_source_pages(
                exploration_action_ids=blocked_transition_ids,
                failure_message=(
                    "本页自动判定已达到执行预算，剩余规则转证据不足复核并继续页面过渡。"
                ),
            )
            stalled_page_ids = scope.working_memory.degrade_stalled_rule_pages(
                failure_message=(
                    "本页自动判定已达到执行预算，剩余规则转证据不足复核并继续其他页面。"
                )
            )
            degraded_page_ids.extend(stalled_page_ids)
            unstarted_page_ids = scope.working_memory.fail_unstarted_rule_matching_pages(
                failure_message=("页面已识别但候选查询未在预算内完成，已转证据不足复核并继续探索。")
            )
            degraded_page_ids.extend(unstarted_page_ids)
            if degraded_page_ids:
                logger.warning(
                    "页面规则连续无进展，已降级 %d 个页面并继续剩余 Frontier。",
                    len(degraded_page_ids),
                )
                scope.working_memory.reset_continuation_count()
                if not scope.working_memory.has_runnable_work():
                    return
                dispatch = prepare_work_dispatch(scope.working_memory)
                current_messages = _build_continuation_message(
                    scope.working_memory, dispatch=dispatch
                )
                continue
            stalled_action_id = scope.working_memory.fail_next_stalled_exploration_action(
                failure_code="EXPLORATION_ACTION_STALLED",
                failure_message=(
                    "单个页面探索动作连续多轮未产生采集结果，已隔离该动作并继续其余 Frontier。"
                ),
            )
            if stalled_action_id is not None:
                logger.warning(
                    "页面探索动作 %s 连续无进展，已隔离该动作并继续剩余 Frontier。",
                    stalled_action_id,
                )
                scope.working_memory.reset_continuation_count()
                if not scope.working_memory.has_runnable_work():
                    return
                dispatch = prepare_work_dispatch(scope.working_memory)
                current_messages = _build_continuation_message(
                    scope.working_memory, dispatch=dispatch
                )
                continue
            scope.working_memory.termination_reason = "continuation_limit_reached"
            scope.working_memory.terminalize_remaining_work(
                failure_stage="agent_continuation",
                failure_code="CONTINUATION_LIMIT_REACHED",
                failure_message="Agent 多次提前返回，剩余页面工作已转人工复核。",
            )
            return

        continuation = scope.working_memory.increment_continuation_count()
        logger.info(
            "Main Agent 页面工作未收敛，开始第 %d 次受监督外层续跑。",
            continuation,
        )
        dispatch = prepare_work_dispatch(scope.working_memory)
        current_messages = _build_continuation_message(scope.working_memory, dispatch=dispatch)


async def _run_background_snapshot_page(
    *,
    page_capture_id: str,
    initial_dispatch: WorkDispatch,
    scope: AgentRunScope,
    rule_judge_gate: asyncio.Semaphore | None = None,
) -> None:
    """排队完成静态页面规则；并发闸门只限制 Judge，不阻塞浏览器主线。"""
    dispatch: WorkDispatch | None = initial_dispatch
    try:
        while dispatch is not None:
            progress_before = _page_rule_progress_signature(
                scope.working_memory,
                page_capture_id,
            )
            if rule_judge_gate is not None and dispatch.kind == "rule_judge":
                async with rule_judge_gate:
                    await _run_direct_dispatch(dispatch=dispatch, scope=scope)
            else:
                await _run_direct_dispatch(dispatch=dispatch, scope=scope)
            if (
                _page_rule_progress_signature(
                    scope.working_memory,
                    page_capture_id,
                )
                == progress_before
            ):
                raise RuntimeError("静态页面后台判定未产生可审计进展")
            dispatch = prepare_page_rule_dispatch(
                scope.working_memory,
                page_capture_id=page_capture_id,
            )
        if _is_pending_page(scope.working_memory, page_capture_id):
            raise RuntimeError("静态页面仍有规则缺少可执行 Judge 路由")
    except Exception as exc:  # noqa: BLE001 - 后台失败必须页级隔离，不能中断浏览器主线
        scope.working_memory.degrade_page_inspection(
            page_capture_id=page_capture_id,
            failure_stage="background_rule_judge",
            failure_code="BACKGROUND_RULE_JUDGE_FAILED",
            failure_message=str(exc) or exc.__class__.__name__,
            retryable=False,
            missing_rule_status="manual_review",
        )


def _is_pending_page(working_memory: WorkingMemory, page_capture_id: str) -> bool:
    snapshot = working_memory.model_snapshot()
    return any(
        isinstance(page, dict) and str(page.get("page_capture_id") or "") == page_capture_id
        for page in snapshot.get("pending_pages", [])
    )


def _page_rule_progress_signature(
    working_memory: WorkingMemory,
    page_capture_id: str,
) -> tuple[str | None, int, int] | None:
    snapshot = working_memory.model_snapshot()
    pages = [
        snapshot.get("current_page"),
        *snapshot.get("pending_pages", []),
        *snapshot.get("inspected_pages", []),
        *snapshot.get("terminal_pages", []),
    ]
    for page in pages:
        if not isinstance(page, dict):
            continue
        if str(page.get("page_capture_id") or "") != page_capture_id:
            continue
        return (
            str(page.get("inspection_status") or "") or None,
            int(page.get("completed_rule_count") or 0),
            len(page.get("pending_rule_judgements") or []),
        )
    return None


async def _run_direct_dispatch(*, dispatch: WorkDispatch, scope: AgentRunScope) -> Any:
    """让确定参数的页面流水线绕过 Main Agent，仍完整经过原有中间件与审计。"""
    if dispatch.tool_name is None or dispatch.arguments is None:
        return None
    metadata = default_registry.get(dispatch.tool_name)
    if metadata is None:
        raise RuntimeError(f"直接派发 Tool 未注册：{dispatch.tool_name}")
    context = FunctionInvocationContext(
        function=metadata.tool,
        arguments=dict(dispatch.arguments),
        session=scope.session,
        kwargs={},
    )
    middlewares = [item for item in scope.middleware if item is not scope.dispatch_boundary]

    async def invoke_at(index: int) -> None:
        if index >= len(middlewares):
            context.result = await metadata.tool.invoke(
                arguments=context.arguments,
                context=context,
                skip_parsing=True,
            )
            return
        await middlewares[index].process(context, lambda: invoke_at(index + 1))

    try:
        await invoke_at(0)
    except MiddlewareTermination as exc:
        if context.result is None:
            context.result = exc.result
    return context.result


async def _run_agent_with_provider_retry(
    *,
    agent: Agent,
    messages: AgentRunInputs,
    middleware: list[FunctionMiddleware],
    session: AgentSession,
    options: dict[str, Any] | None,
) -> Any:
    """仅对连接、超时和限流类供应商异常进行同会话有界重试。"""
    for attempt in range(MAX_RETRYABLE_PROVIDER_CONTINUATIONS + 1):
        try:
            return await agent.run(
                messages,
                middleware=middleware,
                session=session,
                options=options,
            )
        except (OpenAIError, ChatClientException) as exc:
            if attempt >= MAX_RETRYABLE_PROVIDER_CONTINUATIONS or not _is_retryable_provider_error(
                exc
            ):
                raise
            http_status = _provider_http_status(exc)
            backoff_seconds = (
                PROVIDER_OVERLOAD_RETRY_BACKOFF_SECONDS
                if http_status in {500, 502, 503, 504}
                else PROVIDER_RETRY_BACKOFF_SECONDS
            )
            delay = backoff_seconds * (2**attempt)
            reason = f"HTTP {http_status}" if http_status is not None else "连接瞬断"
            logger.warning(
                "大模型供应商%s，将在 %.1fs 后进行第 %s/%s 次同会话重试。",
                reason,
                delay,
                attempt + 1,
                MAX_RETRYABLE_PROVIDER_CONTINUATIONS,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("供应商重试循环未返回结果")


def _is_retryable_provider_error(error: BaseException) -> bool:
    """只识别异常链中的连接、超时和限流类型，不重试鉴权或内容策略错误。"""
    if _provider_http_status(error) in RETRYABLE_PROVIDER_HTTP_STATUSES:
        return True
    pending: list[Any] = [error]
    visited: set[int] = set()
    retryable_names = {
        "apiconnectionerror",
        "apitimeouterror",
        "connecterror",
        "connectionerror",
        "connecttimeout",
        "pooltimeout",
        "ratelimiterror",
        "readtimeout",
        "timeout",
        "timeouterror",
        "writetimeout",
    }
    while pending:
        item = pending.pop()
        if id(item) in visited:
            continue
        visited.add(id(item))
        if isinstance(item, BaseException):
            if type(item).__name__.casefold() in retryable_names:
                return True
            pending.extend(item.args)
            pending.extend(
                cause for cause in (item.__cause__, item.__context__) if cause is not None
            )
    return False


def _isolate_optimized_provider_failure(
    *,
    working_memory: WorkingMemory,
    dispatch: WorkDispatch,
    failure_code: str,
    failure_message: str,
) -> bool:
    """只终结依赖模型歧义处理的当前工作单元，保留其他页面与 Frontier。"""
    if dispatch.kind == "page_exploration" and dispatch.exploration_action_id is not None:
        isolated = working_memory.fail_exploration_action(
            exploration_action_id=dispatch.exploration_action_id,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=False,
            failure_reason="llm_provider_unavailable",
        )
        if isolated:
            working_memory.ensure_failed_obstruction_reobservation(
                exploration_action_id=dispatch.exploration_action_id
            )
        return isolated

    page_capture_id = (
        str(dispatch.arguments.get("page_capture_id") or "").strip()
        if dispatch.arguments is not None
        else ""
    )
    if not page_capture_id:
        return False
    isolated = working_memory.degrade_page_inspection(
        page_capture_id=page_capture_id,
        failure_stage="llm_provider",
        failure_code=failure_code,
        failure_message=failure_message,
        retryable=False,
        missing_rule_status="manual_review",
    )
    if isolated:
        return True
    return working_memory.fail_page_inspection(
        page_capture_id=page_capture_id,
        failure_stage="llm_provider",
        failure_code=failure_code,
        failure_message=failure_message,
        retryable=False,
        missing_rule_status="manual_review",
    )


def _provider_failure_diagnostic(error: BaseException) -> tuple[str, str]:
    """提取可公开展示的供应商诊断，不把响应正文和请求载荷写入规则结果。"""
    status_code = _provider_http_status(error)
    if status_code is not None:
        label = {
            401: "认证失败",
            403: "访问被拒绝",
            408: "请求超时",
            429: "请求限流",
            500: "服务内部错误",
            502: "网关错误",
            503: "服务暂时不可用",
            504: "网关超时",
        }.get(status_code, "请求失败")
        return (
            f"LLM_PROVIDER_HTTP_{status_code}",
            f"模型 API 返回 HTTP {status_code}（{label}），Main Agent 未能完成本轮处理。",
        )
    return (
        f"LLM_PROVIDER_{type(error).__name__.upper()}",
        f"模型 API 调用失败（{type(error).__name__}），Main Agent 未能完成本轮处理。",
    )


def _provider_http_status(error: BaseException) -> int | None:
    """递归读取包装异常中的 HTTP 状态，并兼容只把状态写入文本的网关。"""
    pending: list[Any] = [error]
    visited: set[int] = set()
    while pending:
        item = pending.pop(0)
        if id(item) in visited:
            continue
        visited.add(id(item))
        if not isinstance(item, BaseException):
            continue
        raw_status = getattr(item, "status_code", None)
        if raw_status is None:
            response = getattr(item, "response", None)
            raw_status = getattr(response, "status_code", None)
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = None
        if status_code is not None and 100 <= status_code <= 599:
            return status_code
        match = _PROVIDER_HTTP_STATUS_PATTERN.search(str(item))
        if match is not None:
            return int(match.group(1))
        pending.extend(item.args)
        pending.extend(
            nested for nested in (item.__cause__, item.__context__) if nested is not None
        )
    return None


def _entry_url_from_inputs(messages: AgentRunInputs) -> str | None:
    """从平台生成的任务消息中提取受信任入口 URL。"""
    if not isinstance(messages, str):
        return None
    for raw_line in messages.splitlines():
        line = raw_line.strip()
        if not line.startswith("目标URL："):
            continue
        candidate = line.removeprefix("目标URL：").strip()
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            return candidate
    return None


def _with_initial_dispatch_instruction(
    messages: AgentRunInputs,
    dispatch: WorkDispatch | None,
) -> AgentRunInputs:
    """首轮即告知模型精确入口派发，不等待一次自由规划往返。"""
    if dispatch is None or not isinstance(messages, str):
        return messages
    return f"{messages}\n\n本轮受监督派发：{dispatch.message}"


def _current_page_capture_id(working_memory: WorkingMemory) -> str | None:
    """返回当前活跃页面 ID，作为模型会话压缩边界。"""
    current_page = working_memory.model_snapshot().get("current_page")
    if not isinstance(current_page, dict):
        return None
    page_capture_id = current_page.get("page_capture_id")
    if page_capture_id is None:
        return None
    normalized = str(page_capture_id).strip()
    return normalized or None


def _build_continuation_message(
    working_memory: WorkingMemory, *, dispatch: WorkDispatch | None = None
) -> str:
    """构造只包含未完成页面和 Frontier 的紧凑续跑指令。"""
    snapshot = working_memory.model_snapshot()
    pending = {
        "current_page": snapshot.get("current_page"),
        "pending_pages": snapshot.get("pending_pages"),
        "exploration_frontier": {
            "pending_actions": snapshot.get("exploration_frontier", {}).get("pending_actions", []),
            "pending_action_count": snapshot.get("exploration_frontier", {}).get(
                "pending_action_count", 0
            ),
        },
        "terminal_page_count": snapshot.get("terminal_page_count", 0),
    }
    dispatch_instruction = f"\n本轮受监督派发：{dispatch.message}" if dispatch is not None else ""
    missing_capture_instruction = (
        "当前任务尚未形成任何页面采集，必须使用原始任务上下文中的目标 URL 调用 "
        'page_capture_tool(mode="goto")，再继续识别和规则执行；'
        if _bound_task_missing_page_capture(working_memory)
        else ""
    )
    return (
        "结构化页面工作记忆尚未收敛，上一条文本不能作为任务结束。"
        f"{missing_capture_instruction}"
        "请继续处理 current_page、pending_pages 或 exploration_frontier.pending_actions；"
        "如果 current_page.page_detection 为空，必须先调用 page_detection_tool，"
        "并准确传入 current_page.page_capture_id，禁止再次采集；"
        "mandatory=true 的动作不可跳过：observe 动作必须在同一会话调用 "
        'page_capture_tool(mode="observe", exploration_action_id=动作ID)，'
        'click 关闭动作必须调用 page_capture_tool(mode="single_trigger", '
        'trigger_action="click", selector=动作target, exploration_action_id=动作ID)；'
        'back 返回动作必须调用 page_capture_tool(mode="single_trigger", '
        'trigger_action="back", exploration_action_id=动作ID)，不得伪造 selector；'
        "普通 Frontier 动作也必须原样携带 exploration_action_id、target；若动作含有 "
        "navigation_depth，必须原样传入，不能以旧 URL 代替当前会话点击；"
        "每次动作后使用新 page_capture_id 重新识别，不得一次关闭多个旧弹窗动作。"
        "不要重复处理已进入终态的页面。只有所有已发现工作进入终态后才输出最终结论。\n"
        f"待处理快照：{json.dumps(pending, ensure_ascii=False, default=str)}"
        f"{dispatch_instruction}"
    )


def _terminalize_guarded_work(working_memory: WorkingMemory) -> None:
    """循环护栏终止后，把本 Run 中剩余页面工作转为可统计终态。"""
    reason = working_memory.termination_reason or "run_terminated"
    working_memory.terminalize_remaining_work(
        failure_stage="loop_guard",
        failure_code=reason.upper(),
        failure_message=f"任务因 {reason} 终止，剩余页面工作已转人工复核。",
    )


def _extract_usage(response: Any) -> UsageSnapshot | None:
    """从 ``AgentResponse`` 提取累计 token 用量；框架版本不提供时返回 None。"""
    usage_details = getattr(response, "usage_details", None)
    if usage_details is None:
        return None
    normalized = normalize_llm_usage_tokens(usage_details)
    return UsageSnapshot(
        input_tokens=normalized["input_tokens"],
        output_tokens=normalized["output_tokens"],
        cache_hit_tokens=normalized["cache_hit_tokens"],
        total_tokens=normalized["total_tokens"],
        ai_calls=usage_details.get("request_count"),
    )


def _merge_usage(
    current: UsageSnapshot | None,
    additional: UsageSnapshot | None,
) -> UsageSnapshot | None:
    """累计多次外层续跑分别返回的 token 与模型请求用量。"""
    if current is None:
        return additional
    if additional is None:
        return current
    return UsageSnapshot(
        input_tokens=_sum_optional(current.input_tokens, additional.input_tokens),
        output_tokens=_sum_optional(current.output_tokens, additional.output_tokens),
        cache_hit_tokens=_sum_optional(current.cache_hit_tokens, additional.cache_hit_tokens),
        billable_input_tokens=_sum_optional(
            current.billable_input_tokens,
            additional.billable_input_tokens,
        ),
        total_tokens=_sum_optional(current.total_tokens, additional.total_tokens),
        ai_calls=_sum_optional(current.ai_calls, additional.ai_calls),
        input_cost=_sum_optional_float(current.input_cost, additional.input_cost),
        output_cost=_sum_optional_float(current.output_cost, additional.output_cost),
        cache_hit_cost=_sum_optional_float(current.cache_hit_cost, additional.cache_hit_cost),
        total_cost=_sum_optional_float(current.total_cost, additional.total_cost),
        cost_currency=additional.cost_currency or current.cost_currency,
        pricing_source=additional.pricing_source or current.pricing_source,
        pricing_configured=(
            additional.pricing_configured
            if additional.pricing_configured is not None
            else current.pricing_configured
        ),
        applied_pricing_tiers=tuple(
            sorted(
                set(current.applied_pricing_tiers).union(additional.applied_pricing_tiers)
            )
        ),
        pricing_breakdown=tuple(
            sorted(
                (
                    *current.pricing_breakdown,
                    *additional.pricing_breakdown,
                ),
                key=lambda item: str(item.get("pricing_tier") or ""),
            )
        ),
        provider=additional.provider or current.provider,
        model=additional.model or current.model,
    )


def _sum_optional(left: int | None, right: int | None) -> int | None:
    """仅在至少一侧有值时求和，避免把未知用量误报为零。"""
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _sum_optional_float(left: float | None, right: float | None) -> float | None:
    """仅在至少一侧有值时求和，避免把未知成本误报为零。"""
    if left is None and right is None:
        return None
    return round((left or 0.0) + (right or 0.0), 8)


def _execution_outcome(working_memory: WorkingMemory) -> ExecutionOutcome:
    """将技术终止或页面执行缺口映射为稳定业务结果。"""
    if working_memory.termination_reason is not None or working_memory.requires_review():
        return ExecutionOutcome.NEEDS_REVIEW
    return ExecutionOutcome.SUCCESS


def _classify_termination(working_memory: WorkingMemory) -> TerminationReason:
    if working_memory.termination_reason == "stuck_detected":
        return TerminationReason.STUCK_DETECTED
    if working_memory.termination_reason == "max_steps_reached":
        return TerminationReason.MAX_STEPS_REACHED
    if working_memory.termination_reason == "canceled":
        return TerminationReason.CANCELED
    if working_memory.termination_reason == "infrastructure_unavailable":
        return TerminationReason.INFRASTRUCTURE_UNAVAILABLE
    if working_memory.termination_reason in {
        "continuation_limit_reached",
        "page_work_undispatchable",
    }:
        return TerminationReason.INCOMPLETE
    if _bound_task_missing_page_capture(working_memory):
        return TerminationReason.INCOMPLETE
    if working_memory.has_incomplete_page_inspection():
        return TerminationReason.INCOMPLETE
    if working_memory.has_pending_exploration():
        return TerminationReason.INCOMPLETE
    if working_memory.has_blocked_obstruction():
        return TerminationReason.INCOMPLETE
    return TerminationReason.COMPLETED


def _bound_task_missing_page_capture(working_memory: WorkingMemory) -> bool:
    """任务型 Run 必须至少形成一次页面采集，通用调试 Run 不受此约束。"""
    context = get_agent_run_context()
    return bool(
        context is not None and context.task_id and not working_memory.has_page_capture_history()
    )
