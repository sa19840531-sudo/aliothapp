"""V2 统一任务、页面探索测试与独立规则质检入口。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.object_access import (
    require_task_read_access,
    require_task_write_access,
)
from services.api.app.api.v2.schemas import (
    ExplorationTaskSubmissionResponse,
    RuleEvaluationRunDetailResponse,
    RuleEvaluationRunListResponse,
    RuleEvaluationRunSummary,
    RuleEvaluationSubmissionRequest,
    RuleEvaluationSubmissionResponse,
    V2TaskSubmissionRequest,
    V2TaskSubmissionResponse,
)
from services.api.app.application.execution_ruleset import TaskRulesetSelectionError
from services.api.app.application.h5_shortflows import H5ShortflowError
from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.application.rule_evaluation_runs import (
    RuleEvaluationRun,
    RuleEvaluationRunRepository,
    RuleEvaluationRunService,
    RuleEvaluationTaskNotFoundError,
)
from services.api.app.application.rule_judge import RuleJudgeService
from services.api.app.application.task_submission import (
    AppQcDisabledError,
    AppWorkflowVersionError,
    TaskRulesetNotFoundError,
    TaskSubmissionIdempotencyConflictError,
    TaskSubmissionIdempotencyKeyError,
    TaskSubmissionService,
)
from services.api.app.application.v2_task_entries import (
    AppArtifactSourceNotConfiguredError,
    ExplorationPipelineNotConfiguredError,
    ExplorationTaskEntryPort,
    ExplorationTaskV2EntryService,
    RuleEvaluationEntryPort,
    RuleEvaluationPipelineNotConfiguredError,
    TaskSubmissionV2EntryService,
    V2PlatformHandlerNotConfiguredError,
    V2TaskEntryPort,
)
from services.api.app.application.v2_task_entries import (
    H5V2ModeNotEnabledError as H5V2ModeNotEnabledApplicationError,
)
from services.api.app.application.v2_task_entries import (
    WebV2ModeNotEnabledError as WebV2ModeNotEnabledApplicationError,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import AdultSelfAttestation
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.db.rule_evaluation_run_repository import (
    PostgresRuleEvaluationPageSource,
    PostgresRuleEvaluationRunRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    LLMSemanticRuleJudge,
)
from services.api.app.infrastructure.queue.redis_queue import (
    RedisAgentRunQueue,
    build_redis_client,
)
from services.api.app.infrastructure.queue.state_store import build_agent_run_state_store
from services.api.app.infrastructure.rule_judge_evidence import (
    PostgresRuleJudgeEvidenceReader,
)

router = APIRouter()


@lru_cache
def _get_h5_call_audit_repository():
    from services.api.app.infrastructure.db.h5_call_audit_repository import (
        PostgresH5CallAuditRepository,
    )

    return PostgresH5CallAuditRepository()


@router.get("/tasks/{task_id}/h5-call-audit", summary="只读核对 H5 冻结预算和全部来源调用流水")
async def get_h5_call_audit(
    task_id: str,
    repository: Annotated[object, Depends(_get_h5_call_audit_repository)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
):
    result = await repository.read(task_id=task_id, access_scope=access_scope)
    if result is None:
        raise AliothError(message="H5 任务不存在或不可访问", status_code=404)
    return result


def _configured_web_adult_self_attestation(settings) -> AdultSelfAttestation | None:
    configured = settings.web_adult_self_attestation
    if not configured.enabled:
        return None
    if configured.declared_by is None or configured.declared_at is None:
        raise ValueError("启用的 Web 成年声明配置缺少固定来源或声明时间")
    return AdultSelfAttestation(
        declared=True,
        statement_version=configured.statement_version,
        declared_by=configured.declared_by,
        declared_at=configured.declared_at,
    )


class V2PlatformHandlerNotConnectedError(AliothError):
    """目标平台尚未接入完整 V2 运行时。"""


class AppArtifactSourceNotConnectedError(AliothError):
    """App 制品来源尚未接入解析逻辑。"""


class H5V2ModeNotEnabledError(AliothError):
    """H5 V2 正式运行模式未开启。"""


class WebV2ModeNotEnabledError(AliothError):
    """Web V2 未开启或当前模式不允许该执行类型。"""


class ExplorationPipelineNotConnectedError(AliothError):
    """页面探索测试流程尚未接入。"""


class RuleEvaluationPipelineNotConnectedError(AliothError):
    """独立规则质检流程尚未接入。"""


@lru_cache
def _get_full_task_entry() -> V2TaskEntryPort:
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
    submission_service = TaskSubmissionService(
        task_repository=PostgresTaskRepository(),
        agent_run_queue=queue,
        web_agent_run_queue=web_queue,
        state_store=build_agent_run_state_store(),
        app_qc_enabled=settings.workflow.app_qc_enabled,
        default_workflow_version="v2",
    )
    return TaskSubmissionV2EntryService(
        submission_service,
        h5_v2_mode=settings.workflow.h5_v2_mode,
        web_v2_mode=settings.workflow.web_v2_mode,
        h5_inspection_notification_mode=(settings.workflow.h5_inspection_notification_mode),
        environment_adult_self_attestation=(
            _configured_web_adult_self_attestation(settings)
        ),
    )


@lru_cache
def _get_exploration_task_entry() -> ExplorationTaskEntryPort:
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
    submission_service = TaskSubmissionService(
        task_repository=PostgresTaskRepository(),
        agent_run_queue=queue,
        web_agent_run_queue=web_queue,
        state_store=build_agent_run_state_store(),
        app_qc_enabled=settings.workflow.app_qc_enabled,
        default_workflow_version="v2",
    )
    return ExplorationTaskV2EntryService(
        submission_service,
        h5_v2_mode=settings.workflow.h5_v2_mode,
        web_v2_mode=settings.workflow.web_v2_mode,
        h5_inspection_notification_mode=(settings.workflow.h5_inspection_notification_mode),
        environment_adult_self_attestation=(
            _configured_web_adult_self_attestation(settings)
        ),
    )


@lru_cache
def _get_rule_evaluation_entry() -> RuleEvaluationEntryPort:
    # 判定装配与 agents/rule_judge/bootstrap.py 同源：纯计算、不写正式结果表；
    # 运行结果只落 qc_rule_evaluation_run（与 qc_rule_execution 物理隔离）。
    optimization = get_settings().quality_optimization
    task_repository = PostgresTaskRepository()
    return RuleEvaluationRunService(
        task_lookup=task_repository,
        page_source=PostgresRuleEvaluationPageSource(),
        candidate_source=task_repository,
        judge=RuleJudgeService(
            repository=task_repository,
            evidence_reader=PostgresRuleJudgeEvidenceReader(),
            observation_packet_service=ObservationPacketService(
                repository=PostgresObservationPacketRepository()
            ),
            semantic_judge=LLMSemanticRuleJudge(
                request_timeout_seconds=optimization.semantic_request_timeout_seconds,
                circuit_seconds=optimization.semantic_circuit_seconds,
            ),
            verdict_mode=optimization.verdict_mode,
        ),
        repository=PostgresRuleEvaluationRunRepository(),
    )


@router.post(
    "/tasks",
    response_model=V2TaskSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交一次完整 V2 多 Agent 质检任务",
)
async def submit_v2_task(
    payload: V2TaskSubmissionRequest,
    entry: Annotated[V2TaskEntryPort, Depends(_get_full_task_entry)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> V2TaskSubmissionResponse:
    try:
        submit_options = {"access_scope": access_scope}
        if idempotency_key is not None:
            submit_options["idempotency_key"] = idempotency_key
        result = await entry.submit(payload.to_command(), **submit_options)
    except V2PlatformHandlerNotConfiguredError as exc:
        raise V2PlatformHandlerNotConnectedError(str(exc), status_code=501) from exc
    except H5V2ModeNotEnabledApplicationError as exc:
        raise H5V2ModeNotEnabledError(str(exc), status_code=409) from exc
    except WebV2ModeNotEnabledApplicationError as exc:
        raise WebV2ModeNotEnabledError(str(exc), status_code=409) from exc
    except AppArtifactSourceNotConfiguredError as exc:
        raise AppArtifactSourceNotConnectedError(str(exc), status_code=501) from exc
    except TaskRulesetSelectionError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except H5ShortflowError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except TaskRulesetNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except (AppQcDisabledError, AppWorkflowVersionError) as exc:
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
    return V2TaskSubmissionResponse(
        task_id=result.task_id,
        run_id=result.run_id,
        ruleset_id=result.ruleset_id,
        status=result.status,
    )


@router.post(
    "/exploration-tasks",
    response_model=ExplorationTaskSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交一次只到证据留存的页面探索测试任务",
)
async def submit_exploration_task(
    payload: V2TaskSubmissionRequest,
    entry: Annotated[ExplorationTaskEntryPort, Depends(_get_exploration_task_entry)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> ExplorationTaskSubmissionResponse:
    try:
        submit_options = {"access_scope": access_scope}
        if idempotency_key is not None:
            submit_options["idempotency_key"] = idempotency_key
        result = await entry.submit(payload.to_command(), **submit_options)
    except V2PlatformHandlerNotConfiguredError as exc:
        raise V2PlatformHandlerNotConnectedError(str(exc), status_code=501) from exc
    except H5V2ModeNotEnabledApplicationError as exc:
        raise H5V2ModeNotEnabledError(str(exc), status_code=409) from exc
    except WebV2ModeNotEnabledApplicationError as exc:
        raise WebV2ModeNotEnabledError(str(exc), status_code=409) from exc
    except AppArtifactSourceNotConfiguredError as exc:
        raise AppArtifactSourceNotConnectedError(str(exc), status_code=501) from exc
    except ExplorationPipelineNotConfiguredError as exc:
        raise ExplorationPipelineNotConnectedError(str(exc), status_code=501) from exc
    except TaskRulesetSelectionError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except H5ShortflowError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except TaskRulesetNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except (AppQcDisabledError, AppWorkflowVersionError) as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyConflictError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyKeyError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except TimeoutError as exc:
        raise AliothError(
            message="提交页面探索任务超时：数据库或队列繁忙，请稍后重试",
            status_code=503,
        ) from exc
    except (OSError, ConnectionError) as exc:
        raise AliothError(
            message="提交页面探索任务失败：基础设施暂时不可用，请稍后重试",
            status_code=503,
        ) from exc
    return ExplorationTaskSubmissionResponse(
        task_id=result.task_id,
        run_id=result.run_id,
        status=result.status,
    )


@router.post(
    "/tasks/{task_id}/rule-evaluation-runs",
    response_model=RuleEvaluationSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="基于任务已留存证据提交独立规则质检运行",
)
async def submit_rule_evaluation(
    task_id: str,
    entry: Annotated[RuleEvaluationEntryPort, Depends(_get_rule_evaluation_entry)],
    access_scope: Annotated[AccessScope, Depends(require_task_write_access)],
    payload: RuleEvaluationSubmissionRequest | None = None,
) -> RuleEvaluationSubmissionResponse:
    rule_codes = tuple(payload.rule_codes) if payload and payload.rule_codes else None
    try:
        result = await entry.submit(
            task_id,
            rule_codes=rule_codes,
            access_scope=access_scope,
        )
    except RuleEvaluationTaskNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except RuleEvaluationPipelineNotConfiguredError as exc:
        raise RuleEvaluationPipelineNotConnectedError(str(exc), status_code=501) from exc
    return RuleEvaluationSubmissionResponse(
        task_id=result.task_id,
        rule_evaluation_run_id=result.rule_evaluation_run_id,
        status=result.status,
    )


@lru_cache
def _get_rule_evaluation_run_repository() -> RuleEvaluationRunRepository:
    return PostgresRuleEvaluationRunRepository()


def _run_summary(run: RuleEvaluationRun) -> RuleEvaluationRunSummary:
    return RuleEvaluationRunSummary(
        run_id=run.run_id,
        status=run.status.value,
        page_count=run.page_count,
        rule_count=run.rule_count,
        requested_rule_codes=list(run.requested_rule_codes),
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


@router.get(
    "/tasks/{task_id}/rule-evaluation-runs",
    response_model=RuleEvaluationRunListResponse,
    summary="查看任务的独立规则质检运行列表",
)
async def list_rule_evaluation_runs(
    task_id: str,
    repository: Annotated[
        RuleEvaluationRunRepository, Depends(_get_rule_evaluation_run_repository)
    ],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> RuleEvaluationRunListResponse:
    runs = await repository.list_for_task(
        task_id=task_id,
        access_scope=access_scope,
    )
    return RuleEvaluationRunListResponse(
        task_id=task_id,
        runs=[_run_summary(run) for run in runs],
    )


@router.get(
    "/tasks/{task_id}/rule-evaluation-runs/{run_id}",
    response_model=RuleEvaluationRunDetailResponse,
    summary="查看单次独立规则质检运行的逐规则结果",
)
async def get_rule_evaluation_run(
    task_id: str,
    run_id: str,
    repository: Annotated[
        RuleEvaluationRunRepository, Depends(_get_rule_evaluation_run_repository)
    ],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> RuleEvaluationRunDetailResponse:
    run = await repository.get(
        run_id=run_id,
        task_id=task_id,
        access_scope=access_scope,
    )
    if run is None or run.task_id != task_id:
        raise AliothError(message=f"独立质检运行 {run_id} 不存在", status_code=404)
    summary = _run_summary(run)
    return RuleEvaluationRunDetailResponse(
        **summary.model_dump(),
        task_id=task_id,
        results=list(run.result_payload),
    )
