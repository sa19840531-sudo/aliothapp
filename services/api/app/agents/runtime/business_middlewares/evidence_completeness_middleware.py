"""证据完整性校验：关键结论写入前必须提供证据。"""

from collections.abc import Awaitable, Callable, Iterable, Mapping

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import extract_arguments
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.rule_execution import EVIDENCE_REQUIRED_STATUSES


class EvidenceCompletenessMiddleware(FunctionMiddleware):
    """校验规则执行结果写入时的证据字段。"""

    def __init__(
        self,
        *,
        required_statuses: Iterable[str] = EVIDENCE_REQUIRED_STATUSES,
        target_tool: str = "record_rule_execution_tool",
        batch_target_tool: str = "record_rule_executions_batch_tool",
        working_memory: WorkingMemory | None = None,
    ) -> None:
        self._required_statuses = {item.strip().lower() for item in required_statuses if item}
        self._target_tool = target_tool
        self._batch_target_tool = batch_target_tool
        self._working_memory = working_memory

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if context.function.name not in {self._target_tool, self._batch_target_tool}:
            await call_next()
            return

        arguments = self._bind_page_evidence(extract_arguments(context.arguments))
        context.arguments = arguments
        if context.function.name == self._batch_target_tool:
            invalid_items = _invalid_batch_items(
                arguments,
                required_statuses=self._required_statuses,
            )
            if invalid_items:
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "EVIDENCE_REQUIRED",
                    "retryable": True,
                    "invalid_items": invalid_items,
                    "message": (
                        "批量规则结果中存在缺少有效页面采集或证据引用的条目；"
                        "请使用同一次 page_capture_tool 返回的 page_capture_id 和 artifact_ids "
                        "补齐对应条目，缺少有效证据时改写为 evidence_insufficient。"
                    ),
                }
                return
            await call_next()
            return

        execution_status = str(arguments.get("execution_status") or "").strip().lower()
        page_capture_id = str(arguments.get("page_capture_id") or "").strip()
        evidence_ids = arguments.get("evidence_artifact_ids")
        has_evidence = (
            isinstance(evidence_ids, list)
            and bool(evidence_ids)
            and all(isinstance(item, str) and item.strip() for item in evidence_ids)
        )
        missing_fields: list[str] = []
        if execution_status in self._required_statuses and not page_capture_id:
            missing_fields.append("page_capture_id")
        if execution_status in self._required_statuses and not has_evidence:
            missing_fields.append("evidence_artifact_ids")
        if evidence_ids and not page_capture_id and "page_capture_id" not in missing_fields:
            missing_fields.append("page_capture_id")
        if missing_fields:
            context.result = {
                "execution_status": "blocked",
                "error_code": "EVIDENCE_REQUIRED",
                "retryable": True,
                "missing_fields": missing_fields,
                "message": (
                    f"execution_status={execution_status} 时必须提供有效的页面采集与证据引用；"
                    "请使用同一次 page_capture_tool 返回的 page_capture_id 和 artifact_ids "
                    "补齐后重试，"
                    "缺少有效证据时改写为 evidence_insufficient。"
                ),
            }
            return

        await call_next()

    def _bind_page_evidence(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """用同一页面采集的真实证据补齐模型遗漏，仍由后续门禁统一校验。"""
        enriched = dict(arguments)
        if self._working_memory is None:
            return enriched
        page_capture_id = str(arguments.get("page_capture_id") or "").strip()
        if not page_capture_id:
            return enriched
        artifact_ids = self._working_memory.page_artifact_ids(page_capture_id)
        if not artifact_ids:
            return enriched

        if "executions" not in arguments:
            execution_status = str(arguments.get("execution_status") or "").strip().lower()
            if execution_status in self._required_statuses and not _has_valid_evidence_ids(
                arguments.get("evidence_artifact_ids")
            ):
                enriched["evidence_artifact_ids"] = artifact_ids
            return enriched

        raw_items = arguments.get("executions")
        if not isinstance(raw_items, list):
            return enriched
        executions: list[dict[str, object]] = []
        for raw_item in raw_items:
            item = _item_mapping(raw_item)
            execution_status = str(item.get("execution_status") or "").strip().lower()
            if execution_status in self._required_statuses and not _has_valid_evidence_ids(
                item.get("evidence_artifact_ids")
            ):
                item["evidence_artifact_ids"] = artifact_ids
            executions.append(item)
        enriched["executions"] = executions
        return enriched


def _invalid_batch_items(
    arguments: Mapping[str, object],
    *,
    required_statuses: set[str],
) -> list[dict[str, object]]:
    page_capture_id = str(arguments.get("page_capture_id") or "").strip()
    raw_items = arguments.get("executions")
    if not isinstance(raw_items, list):
        return [
            {
                "index": 0,
                "rule_code": None,
                "missing_fields": ["executions"],
            }
        ]

    invalid_items: list[dict[str, object]] = []
    for index, raw_item in enumerate(raw_items):
        item = _item_mapping(raw_item)
        execution_status = str(item.get("execution_status") or "").strip().lower()
        evidence_ids = item.get("evidence_artifact_ids")
        has_evidence = _has_valid_evidence_ids(evidence_ids)
        missing_fields: list[str] = []
        if execution_status in required_statuses and not page_capture_id:
            missing_fields.append("page_capture_id")
        if execution_status in required_statuses and not has_evidence:
            missing_fields.append("evidence_artifact_ids")
        if evidence_ids and not page_capture_id and "page_capture_id" not in missing_fields:
            missing_fields.append("page_capture_id")
        if missing_fields:
            invalid_items.append(
                {
                    "index": index,
                    "rule_code": item.get("rule_code"),
                    "missing_fields": missing_fields,
                }
            )
    return invalid_items


def _item_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _has_valid_evidence_ids(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
    )
