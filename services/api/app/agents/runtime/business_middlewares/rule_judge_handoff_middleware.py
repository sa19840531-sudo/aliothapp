"""规则判定结果交接：保证确定性判定结果完整传入规则写回 Tool。"""

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import extract_arguments
from services.api.app.application.rule_execution import (
    EVIDENCE_REQUIRED_STATUSES,
    VALID_RULE_EXECUTION_STATUSES,
)

_HANDOFF_FIELDS = (
    "rule_id",
    "rule_code",
    "execution_status",
    "judge_strategy",
    "confidence",
    "result_summary",
    "problem_desc",
    "observation_packet_id",
    "evidence_artifact_ids",
    "judge_input",
    "judge_output",
)
_IDENTITY_FIELDS = {"rule_id", "rule_code"}


class RuleJudgeHandoffMiddleware(FunctionMiddleware):
    """在同一次 Run 内精确交接确定性判定结果。"""

    def __init__(
        self,
        *,
        judge_tool: str = "rule_judge_tool",
        batch_judge_tool: str = "rule_judge_batch_tool",
        record_tool: str = "record_rule_execution_tool",
        batch_record_tool: str = "record_rule_executions_batch_tool",
    ) -> None:
        self._judge_tool = judge_tool
        self._batch_judge_tool = batch_judge_tool
        self._record_tool = record_tool
        self._batch_record_tool = batch_record_tool
        self._handoffs: dict[tuple[str, str], dict[str, Any]] = {}

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        tool_name = context.function.name
        if tool_name in {self._judge_tool, self._batch_judge_tool}:
            await call_next()
            self._remember_judge_result(tool_name, context.result)
            return

        if tool_name == self._record_tool:
            arguments = extract_arguments(context.arguments)
            enriched, conflicting_fields = self._enrich_item(
                page_capture_id=_text(arguments.get("page_capture_id")),
                item=arguments,
            )
            if conflicting_fields:
                context.result = _conflict_result(
                    page_capture_id=_text(arguments.get("page_capture_id")),
                    rule_code=_text(arguments.get("rule_code")),
                    conflicting_fields=conflicting_fields,
                )
                return
            context.arguments = enriched
            await call_next()
            return

        if tool_name == self._batch_record_tool:
            arguments = extract_arguments(context.arguments)
            enriched, conflicts = self._enrich_batch(arguments)
            if conflicts:
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "RULE_JUDGE_HANDOFF_CONFLICT",
                    "retryable": True,
                    "invalid_items": conflicts,
                    "message": (
                        "批量写回参数与同一次规则判定结果不一致；"
                        "请原样使用 rule_judge_batch_tool 返回的字段后重试。"
                    ),
                }
                return
            context.arguments = enriched
            await call_next()
            return

        await call_next()

    def _remember_judge_result(self, tool_name: str, raw_result: Any) -> None:
        result = _result_mapping(raw_result)
        if result is None:
            return
        if tool_name == self._judge_tool:
            self._remember_item(result)
            return
        page_capture_id = _text(result.get("page_capture_id"))
        raw_items = result.get("items")
        if page_capture_id is None or not isinstance(raw_items, list):
            return
        for raw_item in raw_items:
            item = _item_mapping(raw_item)
            if item.get("status") != "ok":
                continue
            item.setdefault("page_capture_id", page_capture_id)
            self._remember_item(item)

    def _remember_item(self, item: Mapping[str, Any]) -> None:
        execution_status = _text(item.get("execution_status"))
        page_capture_id = _text(item.get("page_capture_id"))
        evidence_artifact_ids = _string_list(item.get("evidence_artifact_ids"))
        if (
            execution_status not in VALID_RULE_EXECUTION_STATUSES
            or page_capture_id is None
            or (
                execution_status in EVIDENCE_REQUIRED_STATUSES
                and not evidence_artifact_ids
            )
        ):
            return
        payload = dict(item)
        payload["evidence_artifact_ids"] = evidence_artifact_ids
        for rule_key in _rule_keys(item):
            self._handoffs[(page_capture_id, rule_key)] = payload

    def _enrich_batch(
        self, arguments: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        enriched = dict(arguments)
        page_capture_id = _text(arguments.get("page_capture_id"))
        raw_items = arguments.get("executions")
        if not isinstance(raw_items, list):
            return enriched, []

        executions: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw_items):
            item = _item_mapping(raw_item)
            enriched_item, conflicting_fields = self._enrich_item(
                page_capture_id=page_capture_id,
                item=item,
            )
            executions.append(enriched_item)
            if conflicting_fields:
                conflicts.append(
                    {
                        "index": index,
                        "rule_code": _text(item.get("rule_code")),
                        "conflicting_fields": conflicting_fields,
                    }
                )
        enriched["executions"] = executions
        return enriched, conflicts

    def _enrich_item(
        self,
        *,
        page_capture_id: str | None,
        item: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        enriched = dict(item)
        handoff = self._find_handoff(page_capture_id=page_capture_id, item=item)
        if handoff is None:
            return enriched, []

        conflicts: list[str] = []
        for field in _HANDOFF_FIELDS:
            expected = handoff.get(field)
            if _is_missing(expected):
                continue
            current = enriched.get(field)
            if field in _IDENTITY_FIELDS and not _is_missing(current) and current != expected:
                conflicts.append(field)
                continue
            # 判定 Tool 的结构化结果是可信交接源，覆盖模型遗漏或重组后的非身份字段。
            enriched[field] = expected
        return enriched, conflicts

    def _find_handoff(
        self,
        *,
        page_capture_id: str | None,
        item: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if page_capture_id is None:
            return None
        for rule_key in _rule_keys(item):
            handoff = self._handoffs.get((page_capture_id, rule_key))
            if handoff is not None:
                return handoff
        return None


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


def _item_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _rule_keys(value: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    if rule_id := _text(value.get("rule_id")):
        keys.append(f"id:{rule_id}")
    if rule_code := _text(value.get("rule_code")):
        keys.append(f"code:{rule_code}")
    return keys


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _text(item)) is not None]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _conflict_result(
    *,
    page_capture_id: str | None,
    rule_code: str | None,
    conflicting_fields: list[str],
) -> dict[str, Any]:
    return {
        "execution_status": "blocked",
        "error_code": "RULE_JUDGE_HANDOFF_CONFLICT",
        "retryable": True,
        "page_capture_id": page_capture_id,
        "rule_code": rule_code,
        "conflicting_fields": conflicting_fields,
        "message": (
            "规则写回参数与同一次 rule_judge_tool 判定结果不一致；"
            "请原样使用判定 Tool 返回的字段后重试。"
        ),
    }
