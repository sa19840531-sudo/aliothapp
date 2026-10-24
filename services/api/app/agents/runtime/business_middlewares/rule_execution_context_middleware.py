"""规则执行上下文中间件：自动交接页面证据与候选规则元数据。"""

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import extract_arguments
from services.api.app.agents.runtime.rule_execution_context import RuleExecutionContext


class RuleExecutionContextMiddleware(FunctionMiddleware):
    """在单次 Run 内维护可信规则执行上下文。"""

    def __init__(
        self,
        execution_context: RuleExecutionContext,
        *,
        page_capture_tool: str = "page_capture_tool",
        candidate_rules_tool: str = "candidate_rules_tool",
        judge_tool: str = "rule_judge_tool",
        batch_judge_tool: str = "rule_judge_batch_tool",
        record_tool: str = "record_rule_execution_tool",
        batch_record_tool: str = "record_rule_executions_batch_tool",
    ) -> None:
        self._execution_context = execution_context
        self._page_capture_tool = page_capture_tool
        self._candidate_rules_tool = candidate_rules_tool
        self._judge_tool = judge_tool
        self._batch_judge_tool = batch_judge_tool
        self._record_tool = record_tool
        self._batch_record_tool = batch_record_tool

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        tool_name = context.function.name
        arguments = extract_arguments(context.arguments)
        if tool_name in {self._judge_tool, self._batch_judge_tool}:
            context.arguments = self._execution_context.enrich_judge(arguments)
        elif tool_name == self._record_tool:
            enriched = self._execution_context.enrich_single(arguments)
            if error := self._execution_context.validation_error(enriched):
                context.result = error
                return
            context.arguments = enriched
        elif tool_name == self._batch_record_tool:
            enriched = self._execution_context.enrich_batch(arguments)
            invalid_items = [
                {"index": index, **error}
                for index, item in enumerate(enriched.get("executions", []))
                if isinstance(item, Mapping)
                if (error := self._execution_context.validation_error(item)) is not None
            ]
            if invalid_items:
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "RULE_EXECUTION_CONTEXT_INVALID",
                    "retryable": True,
                    "invalid_items": invalid_items,
                    "message": "批量写回包含非候选规则或冲突的规则身份。",
                }
                return
            context.arguments = enriched

        await call_next()
        result = _result_mapping(context.result)
        if result is None or result.get("execution_status") == "blocked":
            return
        if tool_name == self._page_capture_tool:
            self._execution_context.remember_page_capture(result)
            return
        if tool_name != self._candidate_rules_tool or "error" in result:
            return
        page_capture_id = _text(result.get("page_capture_id") or arguments.get("page_capture_id"))
        payload = self._execution_context.remember_candidate_rules(
            page_capture_id=page_capture_id,
            page_type=_text(result.get("page_type") or arguments.get("page_type")),
            rules=result.get("rules"),
        )
        if payload is not None:
            result["rule_execution_context"] = payload
            context.result = result


def _result_mapping(result: Any) -> dict[str, Any] | None:
    value = result
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    text = getattr(value, "text", None)
    if isinstance(text, str):
        value = text
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return dict(value) if isinstance(value, Mapping) else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
