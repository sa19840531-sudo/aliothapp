"""质检任务业务接口。"""

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, model_validator

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.object_access import require_task_read_access
from services.api.app.application.app_exploration_target import (
    build_app_exploration_target_snapshot,
)
from services.api.app.application.execution_ruleset import TaskRulesetSelectionError
from services.api.app.application.platform_capability import attribute_rule_execution
from services.api.app.application.rule_judge import rule_execution_route_payload
from services.api.app.application.run_costs import (
    LLMCostEstimate,
    estimate_llm_run_cost,
)
from services.api.app.application.task_query import (
    AgentRunDetail,
    CandidateRule,
    RuleExecutionSummary,
    TaskDetail,
    TaskListItem,
    TaskQueryService,
)
from services.api.app.application.task_submission import (
    AppQcDisabledError,
    AppWorkflowVersionError,
    TaskRulesetNotFoundError,
    TaskSubmissionIdempotencyConflictError,
    TaskSubmissionIdempotencyKeyError,
    TaskSubmissionResult,
    TaskSubmissionService,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page.catalog import InvalidPageTypeCodeError
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.task import (
    AppTaskConfig,
    TaskTarget,
    TaskTargetPlatform,
    TaskTargetValidationError,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue, build_redis_client
from services.api.app.infrastructure.queue.state_store import build_agent_run_state_store

router = APIRouter()


class SubmitTaskRequest(BaseModel):
    """提交质检任务请求。"""

    target_platform: TaskTargetPlatform = TaskTargetPlatform.H5
    target_url: str | None = Field(default=None, max_length=4096, description="被质检目标 URL")
    # 不填 = 跑全部开启的规则集；ruleset_codes 选多个；ruleset_code 只锁单个规则集（老调用方）
    ruleset_code: str | None = Field(default=None, min_length=1, max_length=128)
    ruleset_version: int | None = Field(default=None, ge=1)
    ruleset_codes: list[str] | None = Field(default=None, max_length=20)
    channel: Literal["workbench", "telegram", "api"] = "workbench"
    priority: int = Field(default=100, ge=0)
    task_config: dict = Field(default_factory=dict)
    page_sampling_policy: dict = Field(default_factory=dict)
    loop_guard: dict = Field(default_factory=dict)

    def to_target(self) -> TaskTarget:
        if self.target_platform is not TaskTargetPlatform.APP:
            return TaskTarget(
                platform=self.target_platform,
                target_url=self.target_url,
            )
        app_payload = self.task_config.get("app")
        if not isinstance(app_payload, dict):
            raise TaskTargetValidationError("App 任务必须提供 platform_build_ref")
        platform_build_ref = app_payload.get("platform_build_ref")
        if not _is_valid_platform_build_ref(platform_build_ref):
            raise TaskTargetValidationError("platform_build_ref 必须使用 app-build:<uuid> 格式")
        device = app_payload.get("device")
        device_payload = device if isinstance(device, dict) else {}
        return TaskTarget(
            platform=self.target_platform,
            target_url=self.target_url,
            app=AppTaskConfig(
                platform_build_ref=platform_build_ref,
                package_name=app_payload.get("package_name"),
                device_backend=device_payload.get("backend", "cloud_phone"),
                device_profile_id=device_payload.get("profile_id", "default"),
            ),
        )

    @model_validator(mode="after")
    def validate_target_shape(self) -> "SubmitTaskRequest":
        try:
            self.to_target()
        except TaskTargetValidationError as exc:
            raise ValueError(str(exc)) from exc
        return self


def _is_valid_platform_build_ref(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("app-build:"):
        return False
    try:
        UUID(value.removeprefix("app-build:"))
    except (ValueError, AttributeError):
        return False
    return True


class SubmitTaskResponse(BaseModel):
    """提交质检任务响应。"""

    task_id: str
    run_id: str
    ruleset_id: str
    status: str


class TaskDetailResponse(BaseModel):
    """质检任务详情响应。"""

    task_id: str
    target_platform: TaskTargetPlatform
    target_url: str | None
    ruleset_id: str
    ruleset_code: str
    ruleset_version: int
    channel: str
    status: str
    priority: int
    run_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    evidence_index_id: str | None = None
    exploration_status: str | None = None
    evidence_gap: list[str] = Field(default_factory=list)
    page_timeline: list[str] = Field(default_factory=list)


class TaskListItemResponse(BaseModel):
    """质检任务列表项响应。"""

    task_id: str
    target_platform: TaskTargetPlatform
    target_url: str | None
    ruleset_code: str
    ruleset_version: int
    channel: str
    status: str
    priority: int
    run_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskListResponse(BaseModel):
    """质检任务列表响应。"""

    items: list[TaskListItemResponse]


class RuleExecutionSummaryResponse(BaseModel):
    """规则执行结果摘要响应。"""

    execution_id: str
    rule_id: str
    rule_code: str
    rule_name: str
    page_capture_id: str | None
    evidence_input_set_id: str | None
    observation_packet_id: str | None
    execution_status: str
    judge_strategy: str
    confidence: float | None
    severity: str | None
    result_summary: str | None
    problem_desc: str | None
    review_status: str | None
    started_at: datetime | None
    finished_at: datetime | None
    source: str
    outcome_class: str
    capability_issue_code: str | None
    diagnostic_codes: list[str]
    missing_evidence: list[str]
    evidence_artifact_ids: list[str]


class RuleExecutionListResponse(BaseModel):
    """规则执行结果列表响应。"""

    items: list[RuleExecutionSummaryResponse]


class CandidateRuleResponse(BaseModel):
    """候选规则响应项。"""

    rule_id: str
    rule_code: str
    name: str
    description: str | None
    category: str
    severity: str
    executor_type: str
    scope: str | None
    page_type: str | None
    applicable_page_types: list[str]
    url_pattern: str | None
    steps: list[Any]
    input_contract: dict[str, Any]
    check_point: str
    judge_config: dict
    manual_review_policy: dict[str, Any]
    sort_order: int
    match_reason: str
    execution_route: dict


class CandidateRuleListResponse(BaseModel):
    """候选规则列表响应。"""

    items: list[CandidateRuleResponse]


class AgentRunStepResponse(BaseModel):
    """Agent 运行单步轨迹响应。"""

    step_seq: int
    phase: str | None
    thought_summary: str | None
    selected_capability: str | None
    capability_kind: str | None
    status: str
    latency_ms: int | None
    error_code: str | None
    error_message: str | None
    capability_input: dict
    capability_output: dict


class LLMCostEstimateResponse(BaseModel):
    """Agent Run 的 LLM 成本估算响应。"""

    provider: str
    model: str
    currency: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    pricing_unit: str
    pricing_source: str
    pricing_configured: bool
    applied_pricing_tiers: list[str]
    pricing_tier: str | None
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    cache_hit_price_per_million_tokens: float
    calculation_formula: str


class AgentRunDetailResponse(BaseModel):
    """Agent 运行概要与轨迹响应。"""

    run_id: str
    status: str
    stop_reason: str | None
    error_message: str | None
    total_steps: int
    total_ai_calls: int
    total_tokens: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_hit_tokens: int | None
    llm_cost_estimate: LLMCostEstimateResponse | None
    worker_rss_before_bytes: int | None
    worker_rss_after_bytes: int | None
    worker_rss_delta_bytes: int | None
    working_memory_bytes: int | None
    action_trace_count: int
    final_text: str | None
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[AgentRunStepResponse]
    evidence_index_id: str | None = None
    exploration_status: str | None = None
    evidence_gap: list[str] = Field(default_factory=list)
    page_timeline: list[str] = Field(default_factory=list)


class AgentRunEnvelopeResponse(BaseModel):
    """任务最近一次 Agent 运行响应；任务尚未产生运行记录时 run 为空。"""

    run: AgentRunDetailResponse | None


@lru_cache
def _get_task_submission_service() -> TaskSubmissionService:
    settings = get_settings()
    redis_client = build_redis_client(settings.queue)
    queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.agent_run_queue_key,
    )
    web_queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.web_agent_run_queue_key,
    )
    return TaskSubmissionService(
        task_repository=PostgresTaskRepository(),
        agent_run_queue=queue,
        web_agent_run_queue=web_queue,
        state_store=build_agent_run_state_store(),
        app_qc_enabled=settings.workflow.app_qc_enabled,
        default_workflow_version=settings.workflow.version,
    )


@lru_cache
def _get_task_query_service() -> TaskQueryService:
    return TaskQueryService(repository=PostgresTaskRepository())


@lru_cache
def _get_task_insight_repository() -> PostgresTaskInsightRepository:
    return PostgresTaskInsightRepository()


@router.post(
    "/tasks",
    response_model=SubmitTaskResponse,
    status_code=202,
    summary="提交一次质检任务，并派发后台 Agent Run 执行",
)
async def submit_task(
    payload: SubmitTaskRequest,
    service: Annotated[TaskSubmissionService, Depends(_get_task_submission_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> SubmitTaskResponse:
    try:
        submission_options = {}
        if idempotency_key is not None:
            submission_options["idempotency_key"] = idempotency_key
        task_config = dict(payload.task_config)
        if payload.target_platform is TaskTargetPlatform.APP:
            journey_steps = task_config.get("journey_steps")
            journey_steps = journey_steps if isinstance(journey_steps, list) else []
            task_config["app_exploration_target"] = build_app_exploration_target_snapshot(
                journey_steps=journey_steps,
            ).to_dict()
        result: TaskSubmissionResult = await service.submit_task(
            access_scope=access_scope,
            target=payload.to_target(),
            ruleset_code=payload.ruleset_code,
            ruleset_version=payload.ruleset_version,
            ruleset_codes=payload.ruleset_codes,
            channel=payload.channel,
            priority=payload.priority,
            task_config=task_config,
            page_sampling_policy=payload.page_sampling_policy,
            loop_guard=payload.loop_guard,
            **submission_options,
        )
    except TaskRulesetSelectionError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except TaskRulesetNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except AppQcDisabledError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except AppWorkflowVersionError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyConflictError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyKeyError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except TimeoutError as exc:
        raise AliothError(
            message="提交任务超时：数据库或队列繁忙，请稍后重试",
            status_code=503,
        ) from exc
    except (OSError, ConnectionError) as exc:
        raise AliothError(
            message="提交任务失败：基础设施暂时不可用，请稍后重试",
            status_code=503,
        ) from exc
    return SubmitTaskResponse(
        task_id=result.task_id,
        run_id=result.run_id,
        ruleset_id=result.ruleset_id,
        status=result.status,
    )


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="查询质检任务列表",
)
async def list_tasks(
    service: Annotated[TaskQueryService, Depends(_get_task_query_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    status: Literal["queued", "running", "review", "done", "failed", "canceled"] | None = None,
    limit: int = 50,
) -> TaskListResponse:
    safe_limit = min(max(limit, 1), 100)
    items = await service.list_tasks(
        status=status,
        limit=safe_limit,
        access_scope=access_scope,
    )
    return TaskListResponse(items=[_task_list_item_response(item) for item in items])


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    summary="查询质检任务详情",
)
async def get_task(
    task_id: str,
    service: Annotated[TaskQueryService, Depends(_get_task_query_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> TaskDetailResponse:
    detail = await service.get_task_detail(task_id, access_scope=access_scope)
    if detail is None:
        raise AliothError(message=f"未找到 task_id={task_id} 对应的质检任务", status_code=404)
    return _task_detail_response(detail)


@router.get(
    "/tasks/{task_id}/rule-executions",
    response_model=RuleExecutionListResponse,
    summary="查询质检任务的规则执行结果",
)
async def list_rule_executions(
    task_id: str,
    service: Annotated[TaskQueryService, Depends(_get_task_query_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    agent_run_id: str | None = None,
) -> RuleExecutionListResponse:
    executions = await service.list_rule_executions(
        task_id,
        agent_run_id=agent_run_id,
        access_scope=access_scope,
    )
    return RuleExecutionListResponse(
        items=[_rule_execution_response(execution) for execution in executions]
    )


@router.get(
    "/tasks/{task_id}/candidate-rules",
    response_model=CandidateRuleListResponse,
    summary="查询质检任务当前候选规则",
)
async def list_candidate_rules(
    task_id: str,
    service: Annotated[TaskQueryService, Depends(_get_task_query_service)],
    insight_repository: Annotated[
        PostgresTaskInsightRepository, Depends(_get_task_insight_repository)
    ],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    page_type: str | None = None,
) -> CandidateRuleListResponse:
    if page_type is not None:
        try:
            page_type_code = get_default_page_type_registry().require_code(page_type).code
        except InvalidPageTypeCodeError as exc:
            raise AliothError(message=str(exc), status_code=400) from exc
        rules = await service.list_candidate_rules(
            task_id=task_id,
            page_type=page_type_code,
            access_scope=access_scope,
        )
        return CandidateRuleListResponse(items=[_candidate_rule_response(rule) for rule in rules])
    # 不带 page_type 时返回"已识别页面"的候选并集：前端任务详情页整页概览用，
    # 尚无页面采集（如排队中）时为合法空列表，而不是 422。
    visited_page_types = await insight_repository.list_task_page_types(task_id=task_id)
    merged: dict[str, CandidateRule] = {}
    for code in sorted(visited_page_types):
        for rule in await service.list_candidate_rules(
            task_id=task_id,
            page_type=code,
            access_scope=access_scope,
        ):
            merged.setdefault(rule.rule_id, rule)
    ordered = sorted(merged.values(), key=lambda rule: (rule.sort_order, rule.rule_code))
    return CandidateRuleListResponse(items=[_candidate_rule_response(rule) for rule in ordered])


@router.get(
    "/tasks/{task_id}/agent-run",
    response_model=AgentRunEnvelopeResponse,
    summary="查询质检任务最近一次 Agent 运行状态与轨迹",
)
async def get_agent_run(
    task_id: str,
    service: Annotated[TaskQueryService, Depends(_get_task_query_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> AgentRunEnvelopeResponse:
    run = await service.get_latest_agent_run(task_id, access_scope=access_scope)
    return AgentRunEnvelopeResponse(run=None if run is None else _agent_run_response(run))


def _task_list_item_response(item: TaskListItem) -> TaskListItemResponse:
    return TaskListItemResponse(
        task_id=item.task_id,
        target_platform=TaskTargetPlatform(item.target_platform),
        target_url=item.target_url,
        ruleset_code=item.ruleset_code,
        ruleset_version=item.ruleset_version,
        channel=item.channel,
        status=item.status,
        priority=item.priority,
        run_id=item.run_id,
        created_at=item.created_at,
        started_at=item.started_at,
        finished_at=item.finished_at,
    )


def _task_detail_response(detail: TaskDetail) -> TaskDetailResponse:
    return TaskDetailResponse(
        task_id=detail.task_id,
        target_platform=TaskTargetPlatform(detail.target_platform),
        target_url=detail.target_url,
        ruleset_id=detail.ruleset_id,
        ruleset_code=detail.ruleset_code,
        ruleset_version=detail.ruleset_version,
        channel=detail.channel,
        status=detail.status,
        priority=detail.priority,
        run_id=detail.run_id,
        created_at=detail.created_at,
        started_at=detail.started_at,
        finished_at=detail.finished_at,
        last_error=detail.last_error,
        evidence_index_id=detail.evidence_index_id,
        exploration_status=detail.exploration_status,
        evidence_gap=list(detail.evidence_gap),
        page_timeline=list(detail.page_timeline),
    )


def _rule_execution_response(execution: RuleExecutionSummary) -> RuleExecutionSummaryResponse:
    attribution = attribute_rule_execution(
        source=execution.source,
        execution_status=execution.execution_status,
        diagnostic_codes=execution.diagnostic_codes,
        result_summary=execution.result_summary,
    )
    return RuleExecutionSummaryResponse(
        execution_id=execution.execution_id,
        rule_id=execution.rule_id,
        rule_code=execution.rule_code,
        rule_name=execution.rule_name,
        page_capture_id=execution.page_capture_id,
        evidence_input_set_id=execution.evidence_input_set_id,
        observation_packet_id=execution.observation_packet_id,
        execution_status=execution.execution_status,
        judge_strategy=execution.judge_strategy,
        confidence=execution.confidence,
        severity=execution.severity,
        result_summary=execution.result_summary,
        problem_desc=execution.problem_desc,
        review_status=execution.review_status,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        source=execution.source,
        outcome_class=attribution.outcome_class,
        capability_issue_code=attribution.capability_issue_code,
        diagnostic_codes=list(execution.diagnostic_codes),
        missing_evidence=list(execution.missing_evidence),
        evidence_artifact_ids=list(execution.evidence_artifact_ids),
    )


def _candidate_rule_response(rule: CandidateRule) -> CandidateRuleResponse:
    return CandidateRuleResponse(
        rule_id=rule.rule_id,
        rule_code=rule.rule_code,
        name=rule.name,
        description=rule.description,
        category=rule.category,
        severity=rule.severity,
        executor_type=rule.executor_type,
        scope=rule.scope,
        page_type=rule.page_type,
        applicable_page_types=rule.applicable_page_types,
        url_pattern=rule.url_pattern,
        steps=rule.steps,
        input_contract=rule.input_contract,
        check_point=rule.check_point,
        judge_config=rule.judge_config,
        manual_review_policy=rule.manual_review_policy,
        sort_order=rule.sort_order,
        match_reason=rule.match_reason,
        execution_route=rule_execution_route_payload(
            executor_type=rule.executor_type,
            judge_config=rule.judge_config,
        ),
    )


def _agent_run_response(run: AgentRunDetail) -> AgentRunDetailResponse:
    settings = get_settings()
    if run.total_cost is not None and run.pricing_configured:
        llm_cost_estimate = LLMCostEstimate(
            provider=settings.llm.provider,
            model=settings.llm.model,
            currency=(run.cost_currency or settings.llm.cost_currency).strip().upper() or "CNY",
            input_tokens=run.input_tokens or 0,
            output_tokens=run.output_tokens or 0,
            cache_hit_tokens=run.cache_hit_tokens or 0,
            billable_input_tokens=max(0, (run.input_tokens or 0) - (run.cache_hit_tokens or 0)),
            input_cost=run.input_cost or 0,
            output_cost=run.output_cost or 0,
            cache_hit_cost=run.cache_hit_cost or 0,
            total_cost=run.total_cost,
            pricing_unit="1m_tokens",
            pricing_source=run.pricing_source or "runtime_config_per_call",
            pricing_configured=bool(run.pricing_configured),
            applied_pricing_tiers=run.applied_pricing_tiers,
            pricing_tier=run.applied_pricing_tiers[0] if run.applied_pricing_tiers else "",
            context_tokens=run.input_tokens or 0,
            input_price_per_million_tokens=settings.llm.input_price_per_million_tokens,
            output_price_per_million_tokens=settings.llm.output_price_per_million_tokens,
            cache_hit_price_per_million_tokens=(settings.llm.cache_hit_price_per_million_tokens),
            calculation_formula=(
                "(billable_input_tokens * input_price_per_million_tokens "
                "+ output_tokens * output_price_per_million_tokens "
                "+ cache_hit_tokens * cache_hit_price_per_million_tokens) / 1000000"
            ),
            pricing_breakdown=(),
        )
    else:
        llm_cost_estimate = estimate_llm_run_cost(
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_hit_tokens=run.cache_hit_tokens,
            provider=settings.llm.provider,
            model=settings.llm.model,
            currency=settings.llm.cost_currency,
            input_price_per_million_tokens=settings.llm.input_price_per_million_tokens,
            output_price_per_million_tokens=settings.llm.output_price_per_million_tokens,
            cache_hit_price_per_million_tokens=(settings.llm.cache_hit_price_per_million_tokens),
            long_context_threshold_tokens=settings.llm.long_context_threshold_tokens,
            long_context_input_price_per_million_tokens=(
                settings.llm.long_context_input_price_per_million_tokens
            ),
            long_context_output_price_per_million_tokens=(
                settings.llm.long_context_output_price_per_million_tokens
            ),
            long_context_cache_hit_price_per_million_tokens=(
                settings.llm.long_context_cache_hit_price_per_million_tokens
            ),
        )
        if llm_cost_estimate is None and run.total_cost is not None:
            llm_cost_estimate = LLMCostEstimate(
                provider=settings.llm.provider,
                model=settings.llm.model,
                currency=(run.cost_currency or settings.llm.cost_currency).strip().upper() or "CNY",
                input_tokens=run.input_tokens or 0,
                output_tokens=run.output_tokens or 0,
                cache_hit_tokens=run.cache_hit_tokens or 0,
                billable_input_tokens=max(
                    0,
                    (run.input_tokens or 0) - (run.cache_hit_tokens or 0),
                ),
                input_cost=run.input_cost or 0,
                output_cost=run.output_cost or 0,
                cache_hit_cost=run.cache_hit_cost or 0,
                total_cost=run.total_cost,
                pricing_unit="1m_tokens",
                pricing_source=run.pricing_source or "runtime_config_per_call",
                pricing_configured=bool(run.pricing_configured),
                applied_pricing_tiers=run.applied_pricing_tiers,
                pricing_tier=run.applied_pricing_tiers[0] if run.applied_pricing_tiers else "",
                context_tokens=run.input_tokens or 0,
                input_price_per_million_tokens=settings.llm.input_price_per_million_tokens,
                output_price_per_million_tokens=settings.llm.output_price_per_million_tokens,
                cache_hit_price_per_million_tokens=(
                    settings.llm.cache_hit_price_per_million_tokens
                ),
                calculation_formula=(
                    "(billable_input_tokens * input_price_per_million_tokens "
                    "+ output_tokens * output_price_per_million_tokens "
                    "+ cache_hit_tokens * cache_hit_price_per_million_tokens) / 1000000"
                ),
                pricing_breakdown=(),
            )
    return AgentRunDetailResponse(
        run_id=run.run_id,
        status=run.status,
        stop_reason=run.stop_reason,
        error_message=run.error_message,
        total_steps=run.total_steps,
        total_ai_calls=run.total_ai_calls,
        total_tokens=run.total_tokens,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_hit_tokens=run.cache_hit_tokens,
        llm_cost_estimate=_llm_cost_estimate_response(llm_cost_estimate),
        worker_rss_before_bytes=run.worker_rss_before_bytes,
        worker_rss_after_bytes=run.worker_rss_after_bytes,
        worker_rss_delta_bytes=run.worker_rss_delta_bytes,
        working_memory_bytes=run.working_memory_bytes,
        action_trace_count=run.action_trace_count,
        final_text=run.final_text,
        started_at=run.started_at,
        finished_at=run.finished_at,
        evidence_index_id=run.evidence_index_id,
        exploration_status=run.exploration_status,
        evidence_gap=list(run.evidence_gap),
        page_timeline=list(run.page_timeline),
        steps=[
            AgentRunStepResponse(
                step_seq=step.step_seq,
                phase=step.phase,
                thought_summary=step.thought_summary,
                selected_capability=step.selected_capability,
                capability_kind=step.capability_kind,
                status=step.status,
                latency_ms=step.latency_ms,
                error_code=step.error_code,
                error_message=step.error_message,
                capability_input=step.capability_input,
                capability_output=step.capability_output,
            )
            for step in run.steps
        ],
    )


def _llm_cost_estimate_response(
    estimate: LLMCostEstimate | None,
) -> LLMCostEstimateResponse | None:
    if estimate is None:
        return None
    return LLMCostEstimateResponse(
        provider=estimate.provider,
        model=estimate.model,
        currency=estimate.currency,
        input_tokens=estimate.input_tokens,
        output_tokens=estimate.output_tokens,
        cache_hit_tokens=estimate.cache_hit_tokens,
        billable_input_tokens=estimate.billable_input_tokens,
        input_cost=estimate.input_cost,
        output_cost=estimate.output_cost,
        cache_hit_cost=estimate.cache_hit_cost,
        total_cost=estimate.total_cost,
        pricing_unit=estimate.pricing_unit,
        pricing_source=estimate.pricing_source,
        pricing_configured=estimate.pricing_configured,
        applied_pricing_tiers=list(estimate.applied_pricing_tiers),
        pricing_tier=estimate.pricing_tier,
        input_price_per_million_tokens=estimate.input_price_per_million_tokens,
        output_price_per_million_tokens=estimate.output_price_per_million_tokens,
        cache_hit_price_per_million_tokens=estimate.cache_hit_price_per_million_tokens,
        calculation_formula=estimate.calculation_formula,
    )
