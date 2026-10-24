"""候选规则持久化工作项发布与 Enforced 消费门禁。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import (
    TaskRuntimeStateProvider,
    extract_arguments,
)
from services.api.app.agents.runtime.inspection_work_item_context import (
    bind_inspection_judgement_sink,
)
from services.api.app.agents.runtime.inspection_work_item_execution import (
    InspectionWorkItemAttempt,
    replay_recoverable_judgement,
    tool_result_from_execution_links,
)
from services.api.app.agents.runtime.run_context import get_agent_run_context
from services.api.app.application.inspection_candidate_publish import (
    publish_enforced_batches_from_candidate_result,
)
from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    FindCompletedInspectionWorkItemCommand,
    InspectionWorkItemService,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)

logger = logging.getLogger(__name__)

class InspectionWorkItemMiddleware(FunctionMiddleware):
    """把候选执行批次幂等发布到 PostgreSQL，并以工作项租约门禁正式判定链。"""

    def __init__(
        self,
        provider: TaskRuntimeStateProvider,
        *,
        service: InspectionWorkItemService | None = None,
        candidate_tool: str = "candidate_rules_tool",
        record_tool: str = "record_rule_execution_tool",
        batch_record_tool: str = "record_rule_executions_batch_tool",
        judge_tool: str = "rule_judge_tool",
        batch_judge_tool: str = "rule_judge_batch_tool",
        report_tool: str = "report_generation_tool",
    ) -> None:
        self._provider = provider
        self._service = service or InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository()
        )
        self._candidate_tool = candidate_tool
        self._record_tools = {record_tool, batch_record_tool}
        self._judge_tools = {judge_tool, batch_judge_tool}
        self._report_tool = report_tool

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        tool_name = context.function.name
        inspection_settings = get_settings().inspection_work_items
        if tool_name == self._report_tool:
            if await self._has_nonterminal_work():
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "INSPECTION_WORK_ITEMS_NOT_TERMINAL",
                    "retryable": True,
                    "message": "持久化质检工作项尚未收敛，禁止生成完成报告。",
                }
                return

        if tool_name in self._judge_tools | self._record_tools:
            await self._consume_enforced_tool(
                context=context,
                call_next=call_next,
                lease_seconds=inspection_settings.lease_seconds,
                retry_backoff_seconds=inspection_settings.retry_base_seconds,
            )
            return

        await call_next()
        result = _result_mapping(context.result)
        if result is None:
            return

        if tool_name == self._candidate_tool:
            await self._publish_candidate_batches(
                context=context,
                result=result,
            )

    async def _publish_candidate_batches(
        self,
        *,
        context: FunctionInvocationContext,
        result: dict[str, Any],
    ) -> None:
        if _is_blocked_or_error(result):
            return
        rules = _mapping_list(result.get("rules"))
        plan = result.get("execution_plan")
        batches = _mapping_list(plan.get("batches")) if isinstance(plan, Mapping) else []
        if not rules or not batches:
            result["inspection_work_items"] = {
                "status": "not_applicable",
                "published_count": 0,
            }
            context.result = result
            return

        run_context = get_agent_run_context()
        task_id = str(run_context.task_id or "").strip() if run_context else ""
        agent_run_id = str(run_context.agent_run_id or "").strip() if run_context else ""
        page_capture_id = str(result.get("page_capture_id") or "").strip()
        if not task_id or not agent_run_id or not page_capture_id:
            await self._handle_publish_failure(
                context=context,
                page_capture_id=page_capture_id or None,
                error_code="INSPECTION_WORK_ITEM_CONTEXT_MISSING",
                message="候选规则缺少任务、Run 或页面采集上下文",
            )
            return

        try:
            snapshot = await self._provider.get_task_snapshot(task_id)
            if snapshot is None or not snapshot.ruleset_id:
                raise RuntimeError("任务锁定规则集快照不存在")
            published = await publish_enforced_batches_from_candidate_result(
                service=self._service,
                snapshot=snapshot,
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                rules=rules,
                batches=batches,
            )
            result["inspection_work_items"] = published.to_dict()
            context.result = result
        except Exception:  # noqa: BLE001 - 发布失败必须结构化阻断，不得静默继续
            logger.warning(
                "task_id=%s page_capture_id=%s 持久化质检工作项发布失败。",
                task_id,
                page_capture_id,
                exc_info=True,
            )
            await self._handle_publish_failure(
                context=context,
                page_capture_id=page_capture_id,
                error_code="INSPECTION_WORK_ITEM_PUBLISH_FAILED",
                message="持久化质检工作项发布失败",
            )

    async def _consume_enforced_tool(
        self,
        *,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
        lease_seconds: int,
        retry_backoff_seconds: float,
    ) -> None:
        run_context = get_agent_run_context()
        task_id = str(run_context.task_id or "").strip() if run_context else ""
        agent_run_id = str(run_context.agent_run_id or "").strip() if run_context else ""
        arguments = extract_arguments(context.arguments)
        page_capture_id = str(arguments.get("page_capture_id") or "").strip()
        rule_codes = _tool_rule_codes(context.function.name, arguments)
        if not task_id or not agent_run_id or not page_capture_id or not rule_codes:
            context.result = {
                "execution_status": "blocked",
                "error_code": "INSPECTION_WORK_ITEM_CONSUME_CONTEXT_MISSING",
                "retryable": False,
                "page_capture_id": page_capture_id or None,
                "message": "正式质检消费缺少任务、Run、页面采集或规则批次上下文。",
            }
            return
        lease = await self._service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=task_id,
                consumer_agent_run_id=agent_run_id,
                lease_owner=f"agent-run:{agent_run_id}",
                lease_seconds=lease_seconds,
                page_capture_id=page_capture_id,
                rule_codes=tuple(rule_codes),
            )
        )
        if lease is None:
            completed = await self._service.find_completed_result(
                FindCompletedInspectionWorkItemCommand(
                    task_id=task_id,
                    page_capture_id=page_capture_id,
                    rule_codes=tuple(rule_codes),
                )
            )
            if completed is not None:
                context.result = _with_work_item_summary(
                    tool_result_from_execution_links(
                        page_capture_id=completed.page_capture_id,
                        links=completed.links,
                    ),
                    completed.work_item_id,
                    status="recovered",
                )
                return
            logger.info(
                "task_id=%s agent_run_id=%s page_capture_id=%s rule_codes=%s "
                "精确工作项不可领取，判定工具已阻断。",
                task_id,
                agent_run_id,
                page_capture_id,
                rule_codes,
            )
            context.result = {
                "execution_status": "blocked",
                "error_code": "INSPECTION_WORK_ITEM_NOT_CLAIMABLE",
                "retryable": True,
                "page_capture_id": page_capture_id,
                "rule_codes": rule_codes,
                "message": "未找到与本次结构化派发完全匹配的可领取正式工作项。",
            }
            return

        attempt = InspectionWorkItemAttempt(
            service=self._service,
            lease=lease,
            consumer_agent_run_id=agent_run_id,
            lease_seconds=lease_seconds,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        try:
            await attempt.start()
            existing_result = await attempt.recover_formal_results()
            if existing_result is not None:
                context.result = _with_work_item_summary(existing_result, lease.work_item_id)
                return
            async with attempt.lease_heartbeat():
                if lease.recoverable_result_payload:
                    await attempt.persist_recovered_judgement(lease.recoverable_result_payload)
                    result = await replay_recoverable_judgement(
                        lease=lease,
                        result_payload=lease.recoverable_result_payload,
                    )
                    context.result = result
                else:
                    if context.function.name in self._record_tools:
                        await attempt.persist_judgement(
                            _record_arguments_as_attempt_payload(arguments)
                        )
                    async with bind_inspection_judgement_sink(attempt):
                        await call_next()
                    result = _result_mapping(context.result) or {}
                finalized_result = await attempt.finalize_after_tool(result)
            context.result = _with_work_item_summary(
                finalized_result or result,
                lease.work_item_id,
                status=("completed" if finalized_result is not None else "deferred"),
            )
        except Exception as exc:  # noqa: BLE001 - On 模式必须结构化失败关闭
            logger.exception(
                "task_id=%s work_item_id=%s 正式质检工作项消费失败。",
                task_id,
                lease.work_item_id,
            )
            try:
                await attempt.fail_incomplete(
                    retryable=True,
                    error_code="INSPECTION_WORK_ITEM_CONSUME_FAILED",
                    error_message=str(exc) or exc.__class__.__name__,
                )
            except Exception:  # noqa: BLE001 - 租约已失效时保留原始失败
                logger.exception(
                    "work_item_id=%s 消费失败后的状态收口失败。",
                    lease.work_item_id,
                )
            context.result = {
                "execution_status": "blocked",
                "error_code": "INSPECTION_WORK_ITEM_CONSUME_FAILED",
                "retryable": True,
                "page_capture_id": page_capture_id,
                "work_item_id": lease.work_item_id,
                "message": "持久化质检消费失败，正式规则链未被视为完成。",
            }

    async def _has_nonterminal_work(self) -> bool:
        run_context = get_agent_run_context()
        task_id = str(run_context.task_id or "").strip() if run_context else ""
        return bool(task_id and await self._service.has_nonterminal_work(task_id=task_id))

    async def _handle_publish_failure(
        self,
        *,
        context: FunctionInvocationContext,
        page_capture_id: str | None,
        error_code: str,
        message: str,
    ) -> None:
        context.result = {
            "execution_status": "blocked",
            "error_code": error_code,
            "retryable": True,
            "page_capture_id": page_capture_id,
            "message": message,
        }


def _result_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else None
    return None


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    return (
        [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, list)
        else []
    )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_blocked_or_error(result: Mapping[str, Any]) -> bool:
    return bool(
        result.get("execution_status") == "blocked"
        or result.get("status") in {"blocked", "error"}
        or isinstance(result.get("error"), Mapping)
    )


def _tool_rule_codes(tool_name: str, arguments: Mapping[str, Any]) -> list[str]:
    if tool_name in {"rule_judge_tool", "record_rule_execution_tool"}:
        rule_code = _optional_text(arguments.get("rule_code"))
        return [rule_code] if rule_code is not None else []
    collection_name = "rules" if tool_name == "rule_judge_batch_tool" else "executions"
    raw_items = arguments.get(collection_name)
    if not isinstance(raw_items, list):
        return []
    return [
        rule_code
        for item in raw_items
        if isinstance(item, Mapping)
        if (rule_code := _optional_text(item.get("rule_code"))) is not None
    ]


def _record_arguments_as_attempt_payload(arguments: Mapping[str, Any]) -> dict[str, Any]:
    raw_items = arguments.get("executions")
    if isinstance(raw_items, list):
        items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
    else:
        items = [dict(arguments)]
    return {
        "source": "deterministic_rule_write",
        "items": items,
    }


def _with_work_item_summary(
    result: Mapping[str, Any],
    work_item_id: str,
    *,
    status: str = "completed",
) -> dict[str, Any]:
    return {
        **dict(result),
        "inspection_work_item": {
            "work_item_id": work_item_id,
            "status": status,
        },
    }
