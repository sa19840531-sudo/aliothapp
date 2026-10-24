"""规则相关业务 Tool：候选规则查询与规则执行结果写回。"""

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from pydantic import BaseModel, Field, ValidationError

from services.api.app.agents.runtime.run_context import require_agent_run_context
from services.api.app.agents.runtime.tool_failure_policy import is_infrastructure_exception
from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.application.capture_plan import build_capture_plan
from services.api.app.application.rule_batching import build_rule_execution_batch_key
from services.api.app.application.rule_execution import (
    RecordRuleExecutionCommand,
    ReviewStatus,
    RuleExecutionService,
    RuleExecutionTargetNotFoundError,
    RuleExecutionValidationError,
)
from services.api.app.application.rule_judge import rule_execution_route_payload
from services.api.app.application.task_query import CandidateRule, TaskQueryService
from services.api.app.application.web_sequence_errors import WebRuleContractIncompatibleError
from services.api.app.domain.page.catalog import InvalidPageTypeCodeError
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

MAX_RULE_EXECUTION_BATCH_SIZE = 50
_CATEGORY_SKILL_NAMES = {
    "ad": "ad-rule",
    "input_risk": "input-risk-rule",
    "interaction": "interaction-rule",
    "payment": "payment-rule",
    "player": "player-rule",
    "text": "text-rule",
    "visual": "visual-rule",
}


class RuleExecutionBatchItem(BaseModel):
    """批量规则写回中的单条结果。"""

    rule_id: str | None = None
    rule_code: str | None = None
    observation_packet_id: str | None = None
    execution_status: str
    judge_strategy: str | None = None
    confidence: float | None = None
    severity: str | None = None
    result_summary: str | None = None
    problem_desc: str | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    judge_input: dict[str, Any] = Field(default_factory=dict)
    judge_output: dict[str, Any] = Field(default_factory=dict)
    review_status: ReviewStatus | None = None
    review_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None


# 探索/弹窗链路会把阻断态 blocked 直接透传成执行状态；写入词表不含该值，
# 统一降级为证据不足，避免整条判定被写入守卫拒收成「执行异常」（2026-08-25
# 线上 alioth_21 实锤：非法规则执行状态：blocked ×3）。原始态保留在 judge_output。
_EXECUTION_STATUS_FALLBACKS = {"blocked": "evidence_insufficient"}


def _normalized_execution_status(value: str | None) -> str | None:
    if value is None:
        return None
    return _EXECUTION_STATUS_FALLBACKS.get(value, value)


@lru_cache
def _get_task_query_service() -> TaskQueryService:
    return TaskQueryService(repository=PostgresTaskRepository())


@lru_cache
def _get_rule_execution_service() -> RuleExecutionService:
    return RuleExecutionService(repository=PostgresTaskRepository())


async def get_candidate_rules_for_current_run(
    *,
    page_type: str,
    page_capture_id: str,
    page_state: str | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """查询当前 Agent Run 指定页面采集在对应页面类型下的候选规则。"""
    context = require_agent_run_context()
    catalog = get_default_page_type_registry().catalog
    try:
        page_type_definition = catalog.require_code(page_type)
        page_type = page_type_definition.code
    except InvalidPageTypeCodeError as exc:
        return {
            "task_id": context.task_id,
            "page_capture_id": page_capture_id,
            "page_type": page_type,
            "rules": [],
            "error": {
                "code": "INVALID_PAGE_TYPE",
                "message": str(exc),
                "allowed_page_types": list(exc.allowed_page_types),
            },
        }
    if not page_type_definition.rule_matchable:
        return {
            "task_id": context.task_id,
            "page_capture_id": page_capture_id,
            "page_type": page_type,
            "rules": [],
            "error": {
                "code": "PAGE_TYPE_NOT_RULE_MATCHABLE",
                "message": f"页面类型 {page_type} 不允许进入规则匹配。",
            },
        }
    query_service = service if service is not None else _get_task_query_service()
    page_url = await query_service.get_page_capture_url(
        task_id=context.task_id,
        page_capture_id=page_capture_id,
    )
    rules = await query_service.list_candidate_rules(
        task_id=context.task_id,
        page_type=page_type,
        page_url=page_url,
    )
    serialized_rules = [_candidate_rule_to_dict(rule) for rule in rules]
    normalized_page_state = str(page_state or "").strip().casefold()
    serialized_rules = [
        rule
        for rule in serialized_rules
        if _rule_matches_required_page_state(rule, normalized_page_state)
    ]
    if normalized_page_state in {
        "popup",
        "modal",
        "overlay",
        "dialog",
    }:
        serialized_rules = [rule for rule in serialized_rules if _is_popup_relevant_rule(rule)]
    return {
        "task_id": context.task_id,
        "page_capture_id": page_capture_id,
        "page_type": page_type,
        "rules": serialized_rules,
        "execution_plan": _build_execution_plan(serialized_rules),
        "capture_plan": build_capture_plan(
            page_type=page_type,
            rules=serialized_rules,
            page_state=page_state,
        ).to_dict(),
    }


async def record_rule_execution_for_current_run(
    *,
    execution_status: str,
    rule_id: str | None = None,
    rule_code: str | None = None,
    page_capture_id: str | None = None,
    observation_packet_id: str | None = None,
    judge_strategy: str | None = None,
    confidence: float | None = None,
    severity: str | None = None,
    result_summary: str | None = None,
    problem_desc: str | None = None,
    evidence_artifact_ids: list[str] | None = None,
    judge_input: dict[str, Any] | None = None,
    judge_output: dict[str, Any] | None = None,
    review_status: ReviewStatus | None = None,
    review_reason: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """记录当前 Agent Run 对某条规则的执行结果。"""
    write_started_at = time.perf_counter()
    context = require_agent_run_context()
    execution_service = service if service is not None else _get_rule_execution_service()
    normalized_status = _normalized_execution_status(execution_status)
    resolved_judge_output = dict(judge_output or {})
    if normalized_status != execution_status:
        resolved_judge_output.setdefault("original_execution_status", execution_status)
    command = RecordRuleExecutionCommand(
        task_id=context.task_id,
        agent_run_id=context.agent_run_id,
        rule_id=rule_id,
        rule_code=rule_code,
        page_capture_id=page_capture_id,
        observation_packet_id=observation_packet_id,
        execution_status=normalized_status,
        judge_strategy=judge_strategy,
        confidence=confidence,
        severity=severity,
        result_summary=result_summary,
        problem_desc=problem_desc,
        evidence_artifact_ids=evidence_artifact_ids or [],
        judge_input=judge_input or {},
        judge_output=resolved_judge_output,
        review_status=review_status,
        review_reason=review_reason,
        error_code=error_code,
        error_message=error_message,
        started_at=started_at,
        finished_at=finished_at or datetime.now(UTC),
        work_item_id=context.work_item_id,
        attempt_id=context.attempt_id,
        lease_token=context.lease_token,
    )
    try:
        result = await execution_service.record_rule_execution(command)
    except RuleExecutionValidationError as exc:
        # 模型载荷不完整属于规则级执行失败，必须形成可统计终态，不能只留在内存。
        result = await execution_service.record_rule_execution(
            _payload_error_command(command=command, message=str(exc))
        )
    return {
        "execution_id": result.execution_id,
        "rule_id": result.rule_id,
        "rule_code": result.rule_code,
        "execution_status": result.execution_status,
        "quality_outcome": result.quality_outcome,
        "page_capture_id": result.page_capture_id,
        "metrics": {
            "rule_write_back_ms": round(
                (time.perf_counter() - write_started_at) * 1000,
                3,
            )
        },
    }


def _payload_error_command(
    *,
    command: RecordRuleExecutionCommand,
    message: str,
) -> RecordRuleExecutionCommand:
    """把不可接受的模型判定载荷转换为可审计规则错误终态。"""
    return RecordRuleExecutionCommand(
        task_id=command.task_id,
        agent_run_id=command.agent_run_id,
        rule_id=command.rule_id,
        rule_code=command.rule_code,
        page_capture_id=command.page_capture_id,
        observation_packet_id=command.observation_packet_id,
        execution_status="error",
        judge_strategy=None,
        severity=None,
        result_summary="规则判定载荷未通过校验，已记录错误终态。",
        problem_desc=message,
        review_status=ReviewStatus.NEEDS_RECHECK,
        review_reason="规则判定载荷无效，需要人工复核。",
        error_code="RULE_EXECUTION_PAYLOAD_INVALID",
        error_message=message,
        started_at=command.started_at,
        finished_at=command.finished_at or datetime.now(UTC),
        work_item_id=command.work_item_id,
        attempt_id=command.attempt_id,
        lease_token=command.lease_token,
    )


async def record_rule_executions_for_current_run(
    *,
    page_capture_id: str,
    executions: list[RuleExecutionBatchItem | dict[str, Any]],
    service: Any | None = None,
) -> dict[str, Any]:
    """批量写入同一页面采集下的规则执行结果。"""
    write_started_at = time.perf_counter()
    context = require_agent_run_context()
    execution_service = service if service is not None else _get_rule_execution_service()
    items: list[dict[str, Any]] = []
    succeeded_count = 0
    deferred_infrastructure_error: tuple[str, str] | None = None

    for index, raw_item in enumerate(executions):
        rule_id, rule_code = _raw_rule_identity(raw_item)
        if deferred_infrastructure_error is not None:
            code, message = deferred_infrastructure_error
            items.append(
                _rule_batch_error(
                    status="error",
                    rule_id=rule_id,
                    rule_code=rule_code,
                    retryable=True,
                    code=code,
                    message=message,
                    item_index=index,
                    deferred=True,
                )
            )
            continue
        try:
            # Agent Framework 对嵌套模型可能保留为 dict，业务入口必须逐项显式校验。
            item = RuleExecutionBatchItem.model_validate(raw_item)
        except ValidationError as exc:
            items.append(
                _rule_batch_error(
                    status="blocked",
                    rule_id=rule_id,
                    rule_code=rule_code,
                    retryable=False,
                    code="RULE_EXECUTION_ITEM_INVALID",
                    message=str(exc),
                    item_index=index,
                )
            )
            continue
        item_status = _normalized_execution_status(item.execution_status)
        item_judge_output = dict(item.judge_output)
        if item_status != item.execution_status:
            item_judge_output.setdefault("original_execution_status", item.execution_status)
        try:
            result = await execution_service.record_rule_execution(
                RecordRuleExecutionCommand(
                    task_id=context.task_id,
                    agent_run_id=context.agent_run_id,
                    rule_id=item.rule_id,
                    rule_code=item.rule_code,
                    page_capture_id=page_capture_id,
                    observation_packet_id=item.observation_packet_id,
                    execution_status=item_status,
                    judge_strategy=item.judge_strategy,
                    confidence=item.confidence,
                    severity=item.severity,
                    result_summary=item.result_summary,
                    problem_desc=item.problem_desc,
                    evidence_artifact_ids=list(item.evidence_artifact_ids),
                    judge_input=dict(item.judge_input),
                    judge_output=item_judge_output,
                    review_status=item.review_status,
                    review_reason=item.review_reason,
                    error_code=item.error_code,
                    error_message=item.error_message,
                    finished_at=datetime.now(UTC),
                    work_item_id=context.work_item_id,
                    attempt_id=context.attempt_id,
                    lease_token=context.lease_token,
                )
            )
        except RuleExecutionValidationError as exc:
            try:
                result = await execution_service.record_rule_execution(
                    _payload_error_command(
                        command=RecordRuleExecutionCommand(
                            task_id=context.task_id,
                            agent_run_id=context.agent_run_id,
                            rule_id=item.rule_id,
                            rule_code=item.rule_code,
                            page_capture_id=page_capture_id,
                            observation_packet_id=item.observation_packet_id,
                            execution_status=item_status,
                            judge_strategy=item.judge_strategy,
                            severity=item.severity,
                            started_at=None,
                            finished_at=datetime.now(UTC),
                            work_item_id=context.work_item_id,
                            attempt_id=context.attempt_id,
                            lease_token=context.lease_token,
                        ),
                        message=str(exc),
                    )
                )
            except (RuleExecutionValidationError, RuleExecutionTargetNotFoundError) as fallback_exc:
                items.append(
                    _rule_batch_error(
                        status="blocked",
                        rule_id=item.rule_id,
                        rule_code=item.rule_code,
                        retryable=False,
                        code="RULE_EXECUTION_ERROR_FALLBACK_REJECTED",
                        message=f"{exc}；错误终态持久化失败：{fallback_exc}",
                        item_index=index,
                    )
                )
                continue
            except WebRuleContractIncompatibleError:
                # 已认证 Web 合同冲突须由当前 Consumer 专属事务终结，不能降为普通条目错误。
                raise
            except Exception as fallback_exc:  # noqa: BLE001 - 错误终态也需隔离基础设施故障
                infrastructure_error = is_infrastructure_exception(fallback_exc)
                code = (
                    "RULE_EXECUTION_WRITE_UNAVAILABLE"
                    if infrastructure_error
                    else "RULE_EXECUTION_ERROR_FALLBACK_FAILED"
                )
                message = str(fallback_exc) or fallback_exc.__class__.__name__
                items.append(
                    _rule_batch_error(
                        status="error",
                        rule_id=item.rule_id,
                        rule_code=item.rule_code,
                        retryable=infrastructure_error,
                        code=code,
                        message=message,
                        item_index=index,
                    )
                )
                if infrastructure_error:
                    deferred_infrastructure_error = (
                        "RULE_EXECUTION_WRITE_DEFERRED",
                        f"前序数据库写入不可用，当前条目延后处理：{message}",
                    )
                continue

            succeeded_count += 1
            items.append(
                {
                    "status": "ok",
                    "degraded": True,
                    "fallback_code": "RULE_EXECUTION_PAYLOAD_INVALID",
                    "fallback_message": str(exc),
                    "execution_id": result.execution_id,
                    "rule_id": result.rule_id,
                    "rule_code": result.rule_code,
                    "execution_status": result.execution_status,
                    "quality_outcome": result.quality_outcome,
                    "page_capture_id": result.page_capture_id,
                }
            )
            continue
        except RuleExecutionTargetNotFoundError as exc:
            items.append(
                _rule_batch_error(
                    status="blocked",
                    rule_id=item.rule_id,
                    rule_code=item.rule_code,
                    retryable=False,
                    code="RULE_EXECUTION_TARGET_NOT_FOUND",
                    message=str(exc),
                    item_index=index,
                )
            )
            continue
        except WebRuleContractIncompatibleError:
            # 仅传播这一专属信号；普通 H5/App 单项及基础设施错误仍按原批次合同隔离。
            raise
        except Exception as exc:  # noqa: BLE001 - 批量写回必须隔离单项与基础设施故障
            infrastructure_error = is_infrastructure_exception(exc)
            code = (
                "RULE_EXECUTION_WRITE_UNAVAILABLE"
                if infrastructure_error
                else ("RULE_EXECUTION_WRITE_FAILED")
            )
            message = str(exc) or exc.__class__.__name__
            items.append(
                _rule_batch_error(
                    status="error",
                    rule_id=item.rule_id,
                    rule_code=item.rule_code,
                    retryable=infrastructure_error,
                    code=code,
                    message=message,
                    item_index=index,
                )
            )
            if infrastructure_error:
                deferred_infrastructure_error = (
                    "RULE_EXECUTION_WRITE_DEFERRED",
                    f"前序数据库写入不可用，当前条目延后处理：{message}",
                )
            continue

        succeeded_count += 1
        items.append(
            {
                "status": "ok",
                "execution_id": result.execution_id,
                "rule_id": result.rule_id,
                "rule_code": result.rule_code,
                "execution_status": result.execution_status,
                "quality_outcome": result.quality_outcome,
                "page_capture_id": result.page_capture_id,
            }
        )

    failed_count = len(items) - succeeded_count
    return {
        "execution_status": (
            "success"
            if failed_count == 0
            else ("partial_success" if succeeded_count else "blocked")
        ),
        "page_capture_id": page_capture_id,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "items": items,
        "metrics": {
            "rule_write_back_ms": round(
                (time.perf_counter() - write_started_at) * 1000,
                3,
            )
        },
    }


def _raw_rule_identity(
    raw_item: RuleExecutionBatchItem | dict[str, Any],
) -> tuple[str | None, str | None]:
    """尽力从未校验的批量条目提取规则标识。"""
    if isinstance(raw_item, BaseModel):
        payload = raw_item.model_dump()
    elif isinstance(raw_item, dict):
        payload = raw_item
    else:
        return None, None
    rule_id = str(payload.get("rule_id") or "").strip() or None
    rule_code = str(payload.get("rule_code") or "").strip() or None
    return rule_id, rule_code


def _rule_batch_error(
    *,
    status: str,
    rule_id: str | None,
    rule_code: str | None,
    retryable: bool,
    code: str,
    message: str,
    item_index: int,
    deferred: bool = False,
) -> dict[str, Any]:
    """构造统一的规则批量失败条目。"""
    return {
        "status": status,
        "failure_scope": "rule",
        "rule_id": rule_id,
        "rule_code": rule_code,
        "item_index": item_index,
        "retryable": retryable,
        "deferred": deferred,
        "error": {"code": code, "message": message},
    }


@tool(
    name="candidate_rules_tool",
    description=(
        "查询当前 Agent Run 中指定页面采集在对应页面类型下的候选质检规则，"
        "返回数据库规则定义、证据契约、人工复核策略与执行路由；"
        "page_capture_id 必须取自 page_capture_tool 的同一次返回。"
    ),
    category="规则",
    permission_level=PermissionLevel.SAFE,
)
async def candidate_rules_tool(
    page_type: str,
    page_capture_id: str,
    page_state: str | None = None,
) -> dict[str, Any]:
    """查询当前页面类型的候选质检规则。"""
    return await get_candidate_rules_for_current_run(
        page_type=page_type,
        page_capture_id=page_capture_id,
        page_state=page_state,
    )


@tool(
    name="record_rule_execution_tool",
    description=(
        "写入当前 Agent Run 对某条页面质检规则的执行结果。page_capture_id 对全部状态必填，"
        "且必须取自当前 Run 的 page_capture_tool 返回；execution_status 为 "
        "pass/fail/warning/manual_review 时还必须提供非空 evidence_artifact_ids，"
        "否则写入会被证据完整性校验阻断；skipped/evidence_insufficient/error "
        "需在 result_summary 或 problem_desc 说明原因。"
    ),
    category="规则",
    permission_level=PermissionLevel.SENSITIVE,
)
async def record_rule_execution_tool(
    execution_status: str,
    page_capture_id: Annotated[
        str,
        Field(
            description=(
                "本条规则结论对应的页面采集 ID，全部状态必填；取自当前 Run 中 "
                "page_capture_tool 返回的 page_capture_id"
            )
        ),
    ],
    rule_id: str | None = None,
    rule_code: str | None = None,
    observation_packet_id: Annotated[
        str | None,
        Field(
            description=(
                "统一 Judge 创建的 Observation Packet ID；由判定结果交接，"
                "不得由 Main Agent 自行构造"
            )
        ),
    ] = None,
    judge_strategy: str | None = None,
    confidence: Annotated[
        float | None,
        Field(
            description=(
                "判定置信度，范围 0-1；llm/hybrid 的结论状态必填，"
                "低于任务锁定规则 min_confidence 时自动转 manual_review"
            )
        ),
    ] = None,
    severity: str | None = None,
    result_summary: str | None = None,
    problem_desc: str | None = None,
    evidence_artifact_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "支撑本条结论的证据 artifact_id 列表，取自 page_capture_tool 返回的 "
                "artifact_ids；execution_status 为 pass/fail/warning/manual_review 时必填非空"
            )
        ),
    ] = None,
    judge_input: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "判定所需的结构化观测摘要；llm/hybrid 结论状态必填非空对象。"
                "禁止传入完整 DOM、HTML、截图 Base64 或页面源码"
            )
        ),
    ] = None,
    judge_output: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "结构化判定结果。llm/hybrid 必须包含 schema_version=1.0、"
                "conclusion、reasoning_summary；hybrid 还必须包含非空 tool_results"
            )
        ),
    ] = None,
    review_status: ReviewStatus | None = None,
    review_reason: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, str | None]:
    """写入当前规则执行结果。"""
    return await record_rule_execution_for_current_run(
        execution_status=execution_status,
        rule_id=rule_id,
        rule_code=rule_code,
        page_capture_id=page_capture_id,
        observation_packet_id=observation_packet_id,
        judge_strategy=judge_strategy,
        confidence=confidence,
        severity=severity,
        result_summary=result_summary,
        problem_desc=problem_desc,
        evidence_artifact_ids=evidence_artifact_ids,
        judge_input=judge_input,
        judge_output=judge_output,
        review_status=review_status,
        review_reason=review_reason,
        error_code=error_code,
        error_message=error_message,
    )


@tool(
    name="record_rule_executions_batch_tool",
    description=(
        "批量写入同一页面采集下的多条候选规则结果。候选规则超过一条时优先使用本 Tool，"
        "共享 page_capture_id，并逐项返回成功或阻断结果，减少 Agent 往返和工具调用步数。"
    ),
    category="规则",
    permission_level=PermissionLevel.SENSITIVE,
)
async def record_rule_executions_batch_tool(
    page_capture_id: Annotated[
        str,
        Field(description="本批规则共同对应的 page_capture_tool 页面采集 ID"),
    ],
    executions: Annotated[
        list[RuleExecutionBatchItem],
        Field(
            min_length=1,
            max_length=MAX_RULE_EXECUTION_BATCH_SIZE,
            description="同一页面采集下待写入的规则执行结果列表",
        ),
    ],
) -> dict[str, Any]:
    """批量写入当前页面的规则执行结果。"""
    return await record_rule_executions_for_current_run(
        page_capture_id=page_capture_id,
        executions=executions,
    )


def _candidate_rule_to_dict(rule: CandidateRule) -> dict[str, Any]:
    payload = asdict(rule)
    payload["execution_route"] = rule_execution_route_payload(
        executor_type=rule.executor_type,
        judge_config=rule.judge_config,
        input_contract=rule.input_contract,
    )
    return payload


def _is_popup_relevant_rule(rule: dict[str, Any]) -> bool:
    """遮挡页只执行广告或显式弹窗契约，避免每层广告重复扫描底层内容。"""
    searchable = " ".join(
        str(rule.get(key) or "") for key in ("rule_code", "name", "check_point", "description")
    ).casefold()
    judge_config = rule.get("judge_config")
    if isinstance(judge_config, dict):
        searchable += " " + json.dumps(judge_config, ensure_ascii=False).casefold()
    popup_marked = any(
        marker in searchable for marker in ("popup", "dialog", "overlay", "弹窗", "遮罩", "二维码")
    )
    capability = (
        judge_config.get("capability_config")
        if isinstance(judge_config, dict)
        and isinstance(judge_config.get("capability_config"), dict)
        else {}
    )
    if capability.get("requires_journey") is True and not popup_marked:
        return False
    if str(rule.get("category") or "").casefold() == "ad":
        return True
    return popup_marked


def _rule_matches_required_page_state(rule: dict[str, Any], page_state: str) -> bool:
    """仅在规则声明的页面状态下返回候选，避免无弹窗时运行弹窗专项规则。"""
    judge_config = rule.get("judge_config")
    engine_config = (
        judge_config.get("engine_config")
        if isinstance(judge_config, dict) and isinstance(judge_config.get("engine_config"), dict)
        else {}
    )
    required_states = engine_config.get("required_page_states")
    if not isinstance(required_states, list) or not required_states:
        return True
    normalized_required_states = {
        str(state).strip().casefold() for state in required_states if str(state).strip()
    }
    return page_state in normalized_required_states


def _build_execution_plan(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """按实际执行边界生成紧凑批次，减少模型逐条调度。"""
    batch_required = len(rules) > 1
    write_tool_name = (
        "record_rule_executions_batch_tool" if batch_required else "record_rule_execution_tool"
    )
    batches: dict[str, dict[str, Any]] = {}

    for rule in rules:
        route = rule.get("execution_route")
        route = route if isinstance(route, dict) else {}
        strategy = str(route.get("strategy") or rule.get("executor_type") or "")
        category = str(rule.get("category") or "") or None
        session_dependency = str(route.get("session_dependency") or "snapshot")
        batch_key = build_rule_execution_batch_key(
            strategy=strategy,
            category=category,
            session_dependency=session_dependency,
        )
        if route.get("ready") is True and route.get("tool_name") == "rule_judge_tool":
            judge_tool_name = "rule_judge_batch_tool" if batch_required else "rule_judge_tool"
        else:
            judge_tool_name = None

        batch = batches.setdefault(
            batch_key.batch_id,
            {
                "batch_id": batch_key.batch_id,
                "strategy": batch_key.strategy,
                "category": batch_key.category,
                "skill_name": _CATEGORY_SKILL_NAMES.get(batch_key.category or "", "rule-matching"),
                "session_dependency": batch_key.session_dependency,
                "rule_codes": [],
                "judge_tool_name": judge_tool_name,
                "write_tool_name": write_tool_name,
            },
        )
        rule_code = rule.get("rule_code")
        if isinstance(rule_code, str) and rule_code:
            batch["rule_codes"].append(rule_code)

    return {
        "schema_version": "1.0",
        "candidate_rule_count": len(rules),
        "batch_required": batch_required,
        "write_tool_name": write_tool_name,
        "batches": list(batches.values()),
    }
