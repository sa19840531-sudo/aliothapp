"""统一规则判定 Tool：执行任务锁定规则中的确定性判定配置。"""

import asyncio
import logging
import time
from dataclasses import asdict
from functools import lru_cache
from typing import Annotated, Any

from pydantic import BaseModel, Field, ValidationError

from services.api.app.agents.runtime.inspection_work_item_context import (
    persist_inspection_judgement,
)
from services.api.app.agents.runtime.run_context import (
    get_agent_run_context,
    require_agent_run_context,
)
from services.api.app.agents.runtime.tool_failure_policy import is_infrastructure_exception
from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.agents.tools.rule_tools import (
    record_rule_execution_for_current_run,
    record_rule_executions_for_current_run,
)
from services.api.app.application.observation_packet import (
    ObservationPacketService,
    ObservationPacketValidationError,
)
from services.api.app.application.pipeline_shadow import (
    PipelineShadowService,
    ShadowComparison,
)
from services.api.app.application.rule_batching import MAX_RULE_JUDGE_BATCH_SIZE
from services.api.app.application.rule_judge import (
    RuleJudgeContractDriftError,
    RuleJudgeNotConfiguredError,
    RuleJudgeRequest,
    RuleJudgeService,
    RuleJudgeValidationError,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.db.pipeline_shadow_repository import (
    PostgresPipelineShadowRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    LLMSemanticRuleJudge,
)
from services.api.app.infrastructure.rule_judge_evidence import (
    PostgresRuleJudgeEvidenceReader,
)

logger = logging.getLogger(__name__)


class RuleJudgeBatchItem(BaseModel):
    """批量确定性判定中的单条规则输入。"""

    rule_code: str
    judge_input: dict[str, Any] = Field(default_factory=dict)
    expected_contract_hash: str | None = Field(default=None, min_length=1)


@lru_cache
def _get_rule_judge_service() -> RuleJudgeService:
    settings = get_settings()
    optimization = settings.quality_optimization
    return RuleJudgeService(
        repository=PostgresTaskRepository(),
        evidence_reader=PostgresRuleJudgeEvidenceReader(),
        observation_packet_service=ObservationPacketService(
            repository=PostgresObservationPacketRepository()
        ),
        semantic_judge=LLMSemanticRuleJudge(
            request_timeout_seconds=optimization.semantic_request_timeout_seconds,
            circuit_seconds=optimization.semantic_circuit_seconds,
        ),
        verdict_mode=optimization.verdict_mode,
    )


@lru_cache
def _get_pipeline_shadow_service() -> PipelineShadowService:
    return PipelineShadowService(repository=PostgresPipelineShadowRepository())


# 已确认终态的 Run；终态不会回退为 running，记住即可零成本短路后续判定。
_TERMINATED_RUNS: set[str] = set()


async def _agent_run_is_terminal(agent_run_id: str | None) -> bool:
    """Run 已终态时不再发起判定：结果必然在工作项收尾时被作废。

    实测一次任务里 82% 的判定 token 花在 Run 失败之后，产出 100% 作废。
    查询异常一律放行（fail-open）——宁可多判一次，也不能因为一次数据库
    抖动误杀正在进行的判定，那会真的掉质检率。
    """
    if not agent_run_id:
        return False
    if agent_run_id in _TERMINATED_RUNS:
        return True
    try:
        terminal = await _get_rule_judge_repository().agent_run_is_terminal(
            agent_run_id=agent_run_id
        )
    except Exception:  # noqa: BLE001 - 守卫失败必须放行，不能反过来压低质检率
        logger.warning(
            "agent_run_terminal_check_failed agent_run_id=%s，本次判定放行",
            agent_run_id,
            exc_info=True,
        )
        return False
    if terminal:
        _TERMINATED_RUNS.add(agent_run_id)
    return terminal


@lru_cache
def _get_rule_judge_repository() -> PostgresTaskRepository:
    return PostgresTaskRepository()


async def judge_rule_for_current_run(
    *,
    rule_code: str,
    page_capture_id: str,
    evidence_artifact_ids: list[str],
    judge_input: dict[str, Any],
    expected_contract_hash: str | None = None,
    evidence_capture_roles: dict[str, str] | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """执行当前 Agent Run 绑定任务中的一条确定性规则。"""
    judge_started_at = time.perf_counter()
    context = require_agent_run_context()
    judge_service = service if service is not None else _get_rule_judge_service()
    try:
        result = await judge_service.judge(
            RuleJudgeRequest(
                task_id=context.task_id,
                rule_code=rule_code,
                page_capture_id=page_capture_id,
                evidence_artifact_ids=tuple(evidence_artifact_ids),
                judge_input=judge_input,
                agent_run_id=context.agent_run_id,
                evidence_capture_roles=evidence_capture_roles,
                expected_contract_hash=expected_contract_hash,
            )
        )
    except RuleJudgeContractDriftError as exc:
        return _contract_drift_result(
            rule_code=rule_code,
            page_capture_id=page_capture_id,
            expected_contract_hash=expected_contract_hash,
            message=str(exc),
        )
    except RuleJudgeNotConfiguredError as exc:
        return {
            "status": "blocked",
            "execution_status": "blocked",
            "rule_code": rule_code,
            "page_capture_id": page_capture_id,
            "retryable": False,
            "error": {
                "code": "RULE_ENGINE_NOT_READY",
                "message": str(exc),
            },
        }
    except (RuleJudgeValidationError, ObservationPacketValidationError) as exc:
        return {
            "status": "blocked",
            "execution_status": "blocked",
            "rule_code": rule_code,
            "page_capture_id": page_capture_id,
            "retryable": False,
            "error": {
                "code": "TRUSTED_EVIDENCE_INVALID",
                "message": str(exc),
            },
        }
    payload = asdict(result)
    payload["evidence_artifact_ids"] = list(result.evidence_artifact_ids)
    payload["metrics"] = {
        "rule_judge_ms": round(
            (time.perf_counter() - judge_started_at) * 1000,
            3,
        )
    }
    return payload


async def judge_and_record_rule_for_current_run(
    *,
    rule_code: str,
    page_capture_id: str,
    evidence_artifact_ids: list[str],
    judge_input: dict[str, Any],
    expected_contract_hash: str | None = None,
    evidence_capture_roles: dict[str, str] | None = None,
    judge_service: Any | None = None,
    execution_service: Any | None = None,
) -> dict[str, Any]:
    """在同一个受监督 Tool 内完成判定与可信结果写回。

    统一 Judge 的输出已经包含规则身份、Observation Packet 与完整判定字段，
    不应再让模型复制这些字段调用写回 Tool。原子写回既避免兼容模型参数漂移，
    也确保页面工作记忆只在数据库持久化成功后推进。
    """
    judged = await judge_rule_for_current_run(
        rule_code=rule_code,
        page_capture_id=page_capture_id,
        evidence_artifact_ids=evidence_artifact_ids,
        judge_input=judge_input,
        expected_contract_hash=expected_contract_hash,
        evidence_capture_roles=evidence_capture_roles,
        service=judge_service,
    )
    await persist_inspection_judgement({"items": [judged]})
    if judged.get("execution_status") is None:
        return judged
    recorded = await record_rule_execution_for_current_run(
        execution_status=str(judged["execution_status"]),
        rule_id=_optional_text(judged.get("rule_id")),
        rule_code=_optional_text(judged.get("rule_code")),
        page_capture_id=_optional_text(judged.get("page_capture_id")),
        observation_packet_id=_optional_text(judged.get("observation_packet_id")),
        judge_strategy=_optional_text(judged.get("judge_strategy")),
        confidence=_optional_float(judged.get("confidence")),
        severity=_optional_text(judged.get("severity")),
        result_summary=_optional_text(judged.get("result_summary")),
        problem_desc=_optional_text(judged.get("problem_desc")),
        evidence_artifact_ids=_string_list(judged.get("evidence_artifact_ids")),
        judge_input=_mapping(judged.get("judge_input")),
        judge_output=_mapping(judged.get("judge_output")),
        review_reason=_optional_text(judged.get("review_reason")),
        service=execution_service,
    )
    return {**judged, **recorded, "persisted": True}


def _run_terminated_batch_result(
    *, rules: list[Any], page_capture_id: str, started_at: float
) -> dict[str, Any]:
    """Run 已终态时的整批短路结果；不调用模型也不写回。"""
    items = [
        _judge_batch_error(
            status="blocked",
            rule_code=_raw_rule_code(raw_item),
            retryable=False,
            code="RULE_JUDGE_RUN_TERMINATED",
            message="Agent Run 已终态，判定结果无法写回，已跳过以免空耗模型调用。",
            item_index=index,
        )
        for index, raw_item in enumerate(rules)
    ]
    return {
        "execution_status": "blocked",
        "page_capture_id": page_capture_id,
        "succeeded_count": 0,
        "failed_count": len(items),
        "items": items,
        "metrics": {
            "rule_judge_ms": round((time.perf_counter() - started_at) * 1000, 3)
        },
    }


async def judge_rules_for_current_run(
    *,
    page_capture_id: str,
    evidence_artifact_ids: list[str],
    rules: list[RuleJudgeBatchItem | dict[str, Any]],
    evidence_capture_roles: dict[str, str] | None = None,
    service: Any | None = None,
    shadow_service: Any | None = None,
) -> dict[str, Any]:
    """按原顺序隔离结果；影子模式只记录候选差异，不改正式结论。"""
    judge_started_at = time.perf_counter()
    items: list[dict[str, Any] | None] = [None] * len(rules)
    run_context = get_agent_run_context()
    if await _agent_run_is_terminal(run_context.agent_run_id if run_context else None):
        return _run_terminated_batch_result(
            rules=rules, page_capture_id=page_capture_id, started_at=judge_started_at
        )
    validated: list[tuple[int, RuleJudgeBatchItem]] = []
    for index, raw_item in enumerate(rules):
        raw_rule_code = _raw_rule_code(raw_item)
        try:
            # Agent Framework 对嵌套模型可能保留为 dict，业务入口必须逐项显式校验。
            item = RuleJudgeBatchItem.model_validate(raw_item)
        except ValidationError as exc:
            items[index] = _judge_batch_error(
                status="blocked",
                rule_code=raw_rule_code,
                retryable=False,
                code="RULE_JUDGE_ITEM_INVALID",
                message=str(exc),
                item_index=index,
            )
            continue
        validated.append((index, item))

    settings = get_settings().quality_optimization
    durations: dict[int, int] = {}

    async def run_one(
        index: int,
        item: RuleJudgeBatchItem,
        duration_sink: dict[int, int] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        started_at = time.perf_counter()
        try:
            result = await judge_rule_for_current_run(
                rule_code=item.rule_code,
                page_capture_id=page_capture_id,
                evidence_artifact_ids=evidence_artifact_ids,
                judge_input=item.judge_input,
                expected_contract_hash=item.expected_contract_hash,
                evidence_capture_roles=evidence_capture_roles,
                service=service,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不得污染同页其他规则
            infrastructure_error = is_infrastructure_exception(exc)
            if not infrastructure_error:
                return index, {
                    "status": "ok",
                    "rule_id": None,
                    "rule_code": item.rule_code,
                    "page_capture_id": page_capture_id,
                    "execution_status": "manual_review",
                    "judge_strategy": None,
                    "confidence": 0.0,
                    "severity": None,
                    "result_summary": "语义判定单条拆分后仍失败，已转人工复核。",
                    "problem_desc": str(exc) or exc.__class__.__name__,
                    "evidence_artifact_ids": list(evidence_artifact_ids),
                    "judge_input": {
                        **dict(item.judge_input),
                        "semantic_fallback": {"rule_code": item.rule_code},
                    },
                    "judge_output": {
                        "schema_version": "1.0",
                        "conclusion": "manual_review",
                        "reasoning_summary": "语义单条回退未能形成可信自动结论。",
                        "tool_results": [
                            {
                                "tool_name": "semantic_rule_judge",
                                "status": "error",
                                "summary": str(exc) or exc.__class__.__name__,
                            }
                        ],
                        "error_code": "SEMANTIC_SINGLE_FALLBACK_FAILED",
                    },
                    "review_reason": "批次结构失败且单条判定仍无法取得可信输出。",
                }
            return index, _judge_batch_error(
                status="error",
                rule_code=item.rule_code,
                retryable=infrastructure_error,
                code=("RULE_JUDGE_UNAVAILABLE" if infrastructure_error else "RULE_JUDGE_FAILED"),
                message=str(exc) or exc.__class__.__name__,
                item_index=index,
            )
        finally:
            if duration_sink is not None:
                duration_sink[index] = round((time.perf_counter() - started_at) * 1000)
        return index, (
            {"status": "ok", **result} if result.get("execution_status") is not None else result
        )

    async def run_legacy() -> None:
        deferred_error: tuple[str, str] | None = None
        for index, item in validated:
            if deferred_error is not None:
                code, message = deferred_error
                items[index] = _judge_batch_error(
                    status="error",
                    rule_code=item.rule_code,
                    retryable=True,
                    code=code,
                    message=message,
                    item_index=index,
                    deferred=True,
                )
                continue
            _, result = await run_one(index, item, durations)
            items[index] = result
            error = result.get("error")
            if (
                result.get("retryable") is True
                and isinstance(error, dict)
                and error.get("code") == "RULE_JUDGE_UNAVAILABLE"
            ):
                deferred_error = (
                    "RULE_JUDGE_DEFERRED",
                    "前序规则判定基础设施不可用，当前条目延后处理："
                    + str(error.get("message") or "未知错误"),
                )

    async def run_optimized_batches(
        destination: list[dict[str, Any] | None],
        duration_sink: dict[int, int] | None = None,
    ) -> dict[str, int]:
        batch_size = settings.semantic_batch_size
        concurrency = settings.semantic_concurrency
        batch_timeout_seconds = float(getattr(settings, "semantic_batch_timeout_seconds", 90.0))
        batch_gate = asyncio.Semaphore(concurrency)
        batches = [
            validated[start : start + batch_size] for start in range(0, len(validated), batch_size)
        ]

        async def run_batch(
            batch: list[tuple[int, RuleJudgeBatchItem]],
        ) -> list[tuple[int, dict[str, Any]]]:
            async with batch_gate:
                started_at = time.perf_counter()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + batch_timeout_seconds

                def remaining_seconds() -> float:
                    return max(0.001, deadline - loop.time())

                def timeout_results() -> list[tuple[int, dict[str, Any]]]:
                    batch_duration = round((time.perf_counter() - started_at) * 1000)
                    resolved: list[tuple[int, dict[str, Any]]] = []
                    for index, item in batch:
                        if duration_sink is not None:
                            duration_sink[index] = batch_duration
                        resolved.append(
                            (
                                index,
                                {
                                    "status": "ok",
                                    "rule_id": None,
                                    "rule_code": item.rule_code,
                                    "page_capture_id": page_capture_id,
                                    "execution_status": "manual_review",
                                    "judge_strategy": None,
                                    "confidence": 0.0,
                                    "severity": None,
                                    "result_summary": (
                                        "语义批判定超过墙钟预算，已转人工复核并继续探索。"
                                    ),
                                    "problem_desc": (
                                        f"批次判定超过 {batch_timeout_seconds:g} 秒。"
                                    ),
                                    "evidence_artifact_ids": list(evidence_artifact_ids),
                                    "judge_input": {
                                        **dict(item.judge_input),
                                        "semantic_timeout": {
                                            "rule_code": item.rule_code,
                                            "timeout_seconds": batch_timeout_seconds,
                                        },
                                    },
                                    "judge_output": {
                                        "schema_version": "1.0",
                                        "conclusion": "manual_review",
                                        "reasoning_summary": (
                                            "语义批判定超过墙钟预算，未形成可信自动结论。"
                                        ),
                                        "tool_results": [
                                            {
                                                "tool_name": "semantic_rule_judge",
                                                "status": "timeout",
                                                "summary": (
                                                    "模型供应商未在批次预算内返回结果。"
                                                ),
                                            }
                                        ],
                                        "error_code": "SEMANTIC_BATCH_TIMEOUT",
                                        "timeout_seconds": batch_timeout_seconds,
                                    },
                                    "review_reason": ("模型供应商未在批次预算内返回可信结果。"),
                                },
                            )
                        )
                    return resolved

                judge_service = service if service is not None else _get_rule_judge_service()
                judge_many = getattr(judge_service, "judge_many", None)
                if callable(judge_many) and len(batch) > 1:
                    context = require_agent_run_context()
                    try:
                        batch_results = await asyncio.wait_for(
                            judge_many(
                                [
                                    RuleJudgeRequest(
                                        task_id=context.task_id,
                                        rule_code=item.rule_code,
                                        page_capture_id=page_capture_id,
                                        evidence_artifact_ids=tuple(evidence_artifact_ids),
                                        judge_input=item.judge_input,
                                        agent_run_id=context.agent_run_id,
                                        evidence_capture_roles=evidence_capture_roles,
                                        expected_contract_hash=(
                                            item.expected_contract_hash
                                        ),
                                    )
                                    for _, item in batch
                                ]
                            ),
                            timeout=remaining_seconds(),
                        )
                        results_by_code = {result.rule_code: result for result in batch_results}
                        if set(results_by_code) != {item.rule_code for _, item in batch}:
                            raise ValueError("规则批次输出 rule_code 与请求不一致")
                        batch_duration = round((time.perf_counter() - started_at) * 1000)
                        resolved: list[tuple[int, dict[str, Any]]] = []
                        for index, item in batch:
                            result = results_by_code[item.rule_code]
                            if duration_sink is not None:
                                duration_sink[index] = batch_duration
                            payload = asdict(result)
                            payload["evidence_artifact_ids"] = list(result.evidence_artifact_ids)
                            resolved.append((index, {"status": "ok", **payload}))
                        return resolved
                    except TimeoutError:
                        return timeout_results()
                    except Exception:  # noqa: BLE001 - 批次失败只拆分一次为单条
                        pass
                try:
                    return list(
                        await asyncio.wait_for(
                            asyncio.gather(
                                *(run_one(index, item, duration_sink) for index, item in batch)
                            ),
                            timeout=remaining_seconds(),
                        )
                    )
                except TimeoutError:
                    return timeout_results()

        batch_groups = await asyncio.gather(*(run_batch(batch) for batch in batches))
        for batch_results in batch_groups:
            for index, result in batch_results:
                destination[index] = result
        return {
            "batch_size": batch_size,
            "concurrency": concurrency,
            "batch_count": len(batches),
        }

    if settings.pipeline_mode == "on":
        dispatch = await run_optimized_batches(items, durations)
    else:
        await run_legacy()
        dispatch = {
            "batch_size": 1,
            "concurrency": 1,
            "batch_count": len(validated),
        }

    shadow_dispatch: dict[str, int] | None = None
    if settings.pipeline_mode == "shadow" and validated:
        candidate_items = list(items)
        candidate_durations: dict[int, int] = {}
        shadow_dispatch = await run_optimized_batches(
            candidate_items,
            candidate_durations,
        )
        comparison_service = shadow_service or _get_pipeline_shadow_service()
        context = require_agent_run_context()
        for index, item in validated:
            legacy_item = items[index] or {}
            candidate_item = candidate_items[index] or {}
            try:
                await comparison_service.record(
                    ShadowComparison(
                        task_id=context.task_id,
                        agent_run_id=context.agent_run_id,
                        page_capture_id=page_capture_id,
                        rule_id=_optional_text(legacy_item.get("rule_id")),
                        rule_code=item.rule_code,
                        comparison_mode="historical_replay",
                        legacy_conclusion=_optional_text(legacy_item.get("execution_status")),
                        candidate_conclusion=_optional_text(candidate_item.get("execution_status")),
                        legacy_duration_ms=durations.get(index),
                        candidate_duration_ms=candidate_durations.get(index),
                        legacy_evidence_artifact_ids=tuple(
                            _string_list(legacy_item.get("evidence_artifact_ids"))
                        ),
                        candidate_evidence_artifact_ids=tuple(
                            _string_list(candidate_item.get("evidence_artifact_ids"))
                        ),
                        summary={
                            "candidate_status": candidate_item.get("status"),
                            "candidate_retryable": candidate_item.get("retryable"),
                        },
                    )
                )
            except Exception:  # noqa: BLE001 - 影子审计失败不得污染正式结论
                continue

    resolved_items = [item or {} for item in items]
    succeeded_count = sum(1 for item in resolved_items if item.get("status") == "ok")
    failed_count = len(resolved_items) - succeeded_count
    response: dict[str, Any] = {
        "execution_status": (
            "success"
            if failed_count == 0
            else ("partial_success" if succeeded_count else "blocked")
        ),
        "page_capture_id": page_capture_id,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "items": resolved_items,
        "dispatch": dispatch,
        "metrics": {
            "rule_judge_ms": round(
                (time.perf_counter() - judge_started_at) * 1000,
                3,
            ),
            "item_duration_ms": {str(index): value for index, value in sorted(durations.items())},
        },
    }
    if shadow_dispatch is not None:
        response["shadow_dispatch"] = shadow_dispatch
    return response


async def judge_and_record_rules_for_current_run(
    *,
    page_capture_id: str,
    evidence_artifact_ids: list[str],
    rules: list[RuleJudgeBatchItem | dict[str, Any]],
    evidence_capture_roles: dict[str, str] | None = None,
    judge_service: Any | None = None,
    execution_service: Any | None = None,
) -> dict[str, Any]:
    """批量判定后仅把成功条目原子写回，并保持逐条失败隔离。"""
    judged = await judge_rules_for_current_run(
        page_capture_id=page_capture_id,
        evidence_artifact_ids=evidence_artifact_ids,
        rules=rules,
        evidence_capture_roles=evidence_capture_roles,
        service=judge_service,
    )
    await persist_inspection_judgement(judged)
    judged_items = judged.get("items")
    if not isinstance(judged_items, list):
        return judged
    successful_judgements = [
        dict(item)
        for item in judged_items
        if isinstance(item, dict)
        and item.get("status") == "ok"
        and item.get("execution_status") is not None
    ]
    if not successful_judgements:
        return judged

    recorded = await record_rule_executions_for_current_run(
        page_capture_id=page_capture_id,
        executions=successful_judgements,
        service=execution_service,
    )
    raw_recorded_items = recorded.get("items")
    recorded_items = raw_recorded_items if isinstance(raw_recorded_items, list) else []
    recorded_by_key: dict[str, dict[str, Any]] = {}
    for raw_item in recorded_items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        rule_id = _optional_text(item.get("rule_id"))
        rule_code = _optional_text(item.get("rule_code"))
        if rule_id is not None:
            recorded_by_key[f"id:{rule_id}"] = item
        if rule_code is not None:
            recorded_by_key[f"code:{rule_code}"] = item
    merged_items: list[dict[str, Any]] = []
    succeeded_count = 0
    for raw_item in judged_items:
        item = dict(raw_item) if isinstance(raw_item, dict) else {}
        if item.get("status") != "ok":
            merged_items.append(item)
            continue
        persisted = recorded_by_key.get(_rule_key(item) or "")
        if persisted is None:
            merged_items.append(
                {
                    **item,
                    "status": "error",
                    "retryable": True,
                    "error": {
                        "code": "RULE_EXECUTION_WRITE_RESULT_MISSING",
                        "message": "统一 Judge 已完成，但批量写回未返回对应规则结果。",
                    },
                }
            )
            continue
        merged = {**item, **persisted}
        if persisted.get("status") == "ok" and persisted.get("execution_id"):
            merged["persisted"] = True
            succeeded_count += 1
        merged_items.append(merged)

    failed_count = len(merged_items) - succeeded_count
    return {
        **judged,
        "execution_status": (
            "success"
            if failed_count == 0
            else ("partial_success" if succeeded_count else "blocked")
        ),
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "items": merged_items,
    }


def _raw_rule_code(raw_item: RuleJudgeBatchItem | dict[str, Any]) -> str | None:
    """尽力从未校验的判定条目提取规则编码。"""
    if isinstance(raw_item, BaseModel):
        payload = raw_item.model_dump()
    elif isinstance(raw_item, dict):
        payload = raw_item
    else:
        return None
    return str(payload.get("rule_code") or "").strip() or None


def _judge_batch_error(
    *,
    status: str,
    rule_code: str | None,
    retryable: bool,
    code: str,
    message: str,
    item_index: int,
    deferred: bool = False,
) -> dict[str, Any]:
    """构造统一的规则判定批量失败条目。"""
    return {
        "status": status,
        "failure_scope": "rule",
        "rule_code": rule_code,
        "item_index": item_index,
        "retryable": retryable,
        "deferred": deferred,
        "error": {"code": code, "message": message},
    }


def _contract_drift_result(
    *,
    rule_code: str,
    page_capture_id: str,
    expected_contract_hash: str | None,
    message: str,
) -> dict[str, Any]:
    """规则漂移是锁定输入失效，转人工终态且禁止误触发证据补采。"""
    return {
        "status": "ok",
        "execution_status": "manual_review",
        "rule_code": rule_code,
        "page_capture_id": page_capture_id,
        "confidence": 0.0,
        "result_summary": "工作项锁定的规则契约已漂移，自动判定已停止。",
        "problem_desc": message,
        "judge_input": {
            "expected_contract_hash": expected_contract_hash,
        },
        "judge_output": {
            "schema_version": "1.0",
            "conclusion": "manual_review",
            "error_code": "RULE_CONTRACT_DRIFT",
        },
        "review_reason": "必须基于锁定规则快照重新发布工作项。",
        "retryable": False,
        "error": {
            "code": "RULE_CONTRACT_DRIFT",
            "message": message,
        },
    }


@tool(
    name="rule_judge_tool",
    description=(
        "按照当前任务锁定规则组装 Observation Packet，并根据 executor_type 执行"
        "确定性、独立语义或混合判定；返回可直接写回的统一结果。"
    ),
    category="规则",
    permission_level=PermissionLevel.SAFE,
)
async def rule_judge_tool(
    rule_code: str,
    page_capture_id: Annotated[
        str,
        Field(description="本次判定对应的 page_capture_tool 页面采集 ID"),
    ],
    expected_contract_hash: Annotated[
        str | None,
        Field(description="Inspection Work Item 锁定的规则契约哈希"),
    ] = None,
    evidence_artifact_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "可选的证据引用提示；真实证据由后端按 page_capture_id 自动加载，"
                "Main Agent 无需复制 artifact_id"
            )
        ),
    ] = None,
    judge_input: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "可选的规则特有结构化参数；默认由可信 Observation Packet 生成。"
                "仅填写 execution_route.required_input_fields 声明的字段；"
                "trusted_evidence 字段由后端从证据原文读取，模型传值会被忽略。"
                "禁止传入截图 Base64 或完整 DOM"
            )
        ),
    ] = None,
    evidence_capture_roles: Annotated[
        dict[str, str] | None,
        Field(
            description=(
                "多采集判定的角色到 page_capture_id 映射；必须包含 primary，"
                "且 primary 与 page_capture_id 一致。单采集判定可省略"
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Agent 可调用的统一规则判定入口。"""
    return _compact_agent_judge_response(
        await judge_and_record_rule_for_current_run(
            rule_code=rule_code,
            expected_contract_hash=expected_contract_hash,
            page_capture_id=page_capture_id,
            evidence_artifact_ids=evidence_artifact_ids or [],
            judge_input=judge_input or {},
            evidence_capture_roles=evidence_capture_roles,
        )
    )


@tool(
    name="rule_judge_batch_tool",
    description=(
        "批量执行同一页面采集下 execution_route.ready=true 的规则，逐条组装"
        "Observation Packet 并返回可写入结果；候选规则超过一条时优先使用。"
    ),
    category="规则",
    permission_level=PermissionLevel.SAFE,
)
async def rule_judge_batch_tool(
    page_capture_id: Annotated[
        str,
        Field(description="本批判定对应的 page_capture_tool 页面采集 ID"),
    ],
    rules: Annotated[
        list[RuleJudgeBatchItem],
        Field(
            min_length=1,
            max_length=MAX_RULE_JUDGE_BATCH_SIZE,
            description="同策略、同类别的小批规则；每项至少提供 rule_code",
        ),
    ],
    evidence_artifact_ids: Annotated[
        list[str] | None,
        Field(description=("可选的共享证据引用提示；真实证据由后端按 page_capture_id 自动加载")),
    ] = None,
    evidence_capture_roles: Annotated[
        dict[str, str] | None,
        Field(description="本批共享的采集角色映射；单采集判定可省略"),
    ] = None,
) -> dict[str, Any]:
    """批量执行确定性规则判定。"""
    return _compact_agent_judge_response(
        await judge_and_record_rules_for_current_run(
            page_capture_id=page_capture_id,
            evidence_artifact_ids=evidence_artifact_ids or [],
            rules=rules,
            evidence_capture_roles=evidence_capture_roles,
        )
    )


def _compact_agent_judge_response(result: dict[str, Any]) -> dict[str, Any]:
    """只向 Main Agent 返回调度所需摘要，完整判定输入输出已持久化到审计表。"""
    scalar_keys = (
        "status",
        "execution_status",
        "page_capture_id",
        "rule_id",
        "rule_code",
        "execution_id",
        "observation_packet_id",
        "judge_strategy",
        "confidence",
        "severity",
        "persisted",
        "failure_isolated",
        "retryable",
        "already_terminal",
        "succeeded_count",
        "failed_count",
        "dispatch",
        "metrics",
    )
    compact = {key: result[key] for key in scalar_keys if key in result}
    for key in ("result_summary", "problem_desc", "review_reason", "message"):
        value = result.get(key)
        if value is not None:
            compact[key] = str(value)[:600]
    raw_error = result.get("error")
    if isinstance(raw_error, dict):
        compact["error"] = {
            "code": raw_error.get("code"),
            "message": str(raw_error.get("message") or "")[:600],
        }
    raw_items = result.get("items")
    if isinstance(raw_items, list):
        compact["items"] = [
            _compact_agent_judge_response(dict(item))
            for item in raw_items
            if isinstance(item, dict)
        ]
    return compact


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _optional_text(item)) is not None]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _rule_key(value: dict[str, Any]) -> str | None:
    rule_id = _optional_text(value.get("rule_id"))
    if rule_id is not None:
        return f"id:{rule_id}"
    rule_code = _optional_text(value.get("rule_code"))
    return f"code:{rule_code}" if rule_code is not None else None
