"""持久化质检工作项单次租约的执行、恢复与状态收口。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.application.inspection_work_items import (
    InspectionAttemptStatus,
    InspectionWorkItemLease,
    InspectionWorkItemRuleStatus,
    InspectionWorkItemService,
    InspectionWorkItemStatus,
    InspectionWorkItemSyncResult,
    RenewInspectionWorkItemLeaseCommand,
    StartInspectionAttemptCommand,
    TransitionInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    TransitionInspectionWorkItemRuleCommand,
)

logger = logging.getLogger(__name__)


class InspectionWorkItemAttempt:
    """管理一个 Enforced 工作项租约及其持久化 Judge Attempt。"""

    def __init__(
        self,
        *,
        service: InspectionWorkItemService,
        lease: InspectionWorkItemLease,
        consumer_agent_run_id: str,
        lease_seconds: int,
        retry_backoff_seconds: float,
        allow_terminal_web_owner_recovery: bool = False,
    ) -> None:
        self._service = service
        self.lease = lease
        self._consumer_agent_run_id = consumer_agent_run_id
        self._lease_seconds = lease_seconds
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._allow_terminal_web_owner_recovery = allow_terminal_web_owner_recovery
        self._row_version = lease.row_version
        self._attempt_id: str | None = None
        self._attempt_status: InspectionAttemptStatus | None = None
        self._lease_released = False
        self._state_lock = asyncio.Lock()

    @property
    def attempt_id(self) -> str:
        """返回已经持久化的 Attempt 身份，供只读 Handoff 使用。"""
        return self._require_attempt_id()

    async def start(self) -> None:
        """把租约推进为 running，并登记当前尝试。"""
        async with self._state_lock:
            self._row_version = await self._service.transition(
                TransitionInspectionWorkItemCommand(
                    work_item_id=self.lease.work_item_id,
                    expected_status=InspectionWorkItemStatus.LEASED,
                    target_status=InspectionWorkItemStatus.RUNNING,
                    row_version=self._row_version,
                    lease_token=self.lease.lease_token,
                )
            )
            self._attempt_id = await self._service.start_attempt(
                StartInspectionAttemptCommand(
                    work_item_id=self.lease.work_item_id,
                    consumer_agent_run_id=self._consumer_agent_run_id,
                    attempt_no=self.lease.attempt_no,
                    lease_token=self.lease.lease_token,
                    input_hash=self.lease.input_hash,
                    work_item_input_hash=self.lease.work_item_input_hash,
                )
            )
            self._attempt_status = InspectionAttemptStatus.RUNNING

    async def persist_judgement(self, result: Mapping[str, Any]) -> None:
        """先保存 Judge 输出，再把可验证的成员推进到 judged。"""
        await self._persist_attempt_payload(result, mark_members=True)

    async def persist_recovered_judgement(self, result: Mapping[str, Any]) -> None:
        """复制旧 Attempt 的可信输出；成员状态由正式写回同步，不重复推进。"""
        await self._persist_attempt_payload(result, mark_members=False)

    async def _persist_attempt_payload(
        self,
        result: Mapping[str, Any],
        *,
        mark_members: bool,
    ) -> None:
        attempt_id = self._require_attempt_id()
        if self._attempt_status != InspectionAttemptStatus.RUNNING:
            return
        payload = _sanitize_attempt_payload(result)
        await self._service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=attempt_id,
                expected_status=InspectionAttemptStatus.RUNNING,
                target_status=InspectionAttemptStatus.JUDGED,
                lease_token=self.lease.lease_token,
                result_payload=payload,
            )
        )
        self._attempt_status = InspectionAttemptStatus.JUDGED
        if not mark_members:
            return
        rules_by_code = {rule.rule_code: rule for rule in self.lease.rules}
        for item in _judgement_items(payload):
            rule = rules_by_code.get(_text(item.get("rule_code")) or "")
            packet_id = _text(item.get("observation_packet_id"))
            if rule is None or packet_id is None or _text(item.get("execution_status")) is None:
                continue
            await self._service.transition_rule(
                TransitionInspectionWorkItemRuleCommand(
                    work_item_id=self.lease.work_item_id,
                    rule_id=rule.rule_id,
                    expected_status=InspectionWorkItemRuleStatus.PENDING,
                    target_status=InspectionWorkItemRuleStatus.JUDGED,
                    lease_token=self.lease.lease_token,
                    observation_packet_id=packet_id,
                )
            )

    async def recover_formal_results(self) -> dict[str, Any] | None:
        """若正式结果已经存在，则不重复 Judge，直接收口并返回 Tool 摘要。"""
        synced = await self._service.sync_formal_results(
            work_item_id=self.lease.work_item_id,
            lease_token=self.lease.lease_token,
        )
        if synced.terminal_status is None:
            return None
        if self._attempt_status == InspectionAttemptStatus.RUNNING:
            await self._persist_attempt_payload(
                {
                    "recovered_from_rule_executions": True,
                    "items": [
                        {
                            "rule_id": link.rule_id,
                            "rule_code": link.rule_code,
                            "execution_status": link.execution_status,
                            "observation_packet_id": link.observation_packet_id,
                            "execution_id": link.rule_execution_id,
                        }
                        for link in synced.links
                    ],
                },
                mark_members=False,
            )
        await self._finish_attempt_written()
        await self._finish_work_item(synced.terminal_status)
        return tool_result_from_execution_links(
            page_capture_id=self.lease.page_capture_id,
            links=synced.links,
        )

    async def finalize_after_tool(
        self,
        result: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """同步正式写回；完整则成功收口，否则进入重试或待证据状态。"""
        synced = await self._service.sync_formal_results(
            work_item_id=self.lease.work_item_id,
            lease_token=self.lease.lease_token,
        )
        if synced.terminal_status is not None:
            if self._attempt_status == InspectionAttemptStatus.RUNNING:
                await self._persist_attempt_payload(
                    {
                        "recovered_from_rule_executions": True,
                        "items": [
                            {
                                "rule_id": link.rule_id,
                                "rule_code": link.rule_code,
                                "execution_status": link.execution_status,
                                "observation_packet_id": link.observation_packet_id,
                                "execution_id": link.rule_execution_id,
                            }
                            for link in synced.links
                        ],
                    },
                    mark_members=False,
                )
            await self._finish_attempt_written()
            await self._finish_work_item(synced.terminal_status)
            return tool_result_from_execution_links(
                page_capture_id=self.lease.page_capture_id,
                links=synced.links,
            )

        if _evidence_requests_cover_unresolved_rules(
            result,
            lease=self.lease,
            synced=synced,
        ):
            await self.fail_incomplete(
                retryable=False,
                error_code="INSPECTION_WORK_ITEM_WAITING_EVIDENCE",
                error_message="缺证规则已生成正式补采请求，等待补采终态",
            )
            return None

        # 只有 Judge 漏返回成员、正式写回暂缺或下游短暂失败才消耗工作项重试预算。
        # 完整 Evidence Request 已在上方进入 waiting_evidence，由补采状态机负责重开或结算。
        await self.fail_incomplete(
            retryable=True,
            error_code="INSPECTION_WORK_ITEM_RETRYABLE_INCOMPLETE",
            error_message="统一 Judge 或正式规则写回未覆盖工作项全部成员",
        )
        return None

    async def fail_incomplete(
        self,
        *,
        retryable: bool,
        error_code: str,
        error_message: str,
    ) -> None:
        """把未完成尝试收敛为可重试或待最终完整性处理的状态。"""
        attempt_id = self._require_attempt_id()
        expected_attempt_status = self._attempt_status or InspectionAttemptStatus.RUNNING
        target_attempt_status = (
            InspectionAttemptStatus.RETRYABLE_ERROR
            if retryable
            else InspectionAttemptStatus.TERMINAL_ERROR
        )
        if expected_attempt_status in {
            InspectionAttemptStatus.RUNNING,
            InspectionAttemptStatus.JUDGED,
        }:
            await self._service.transition_attempt(
                TransitionInspectionAttemptCommand(
                    attempt_id=attempt_id,
                    expected_status=expected_attempt_status,
                    target_status=target_attempt_status,
                    lease_token=self.lease.lease_token,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            self._attempt_status = target_attempt_status

        async with self._state_lock:
            if retryable:
                target_status = InspectionWorkItemStatus.RETRY_WAIT
                next_attempt_at = datetime.now(UTC) + timedelta(seconds=self._retry_backoff_seconds)
            else:
                target_status = InspectionWorkItemStatus.WAITING_EVIDENCE
                next_attempt_at = None
            self._row_version = await self._service.transition(
                TransitionInspectionWorkItemCommand(
                    work_item_id=self.lease.work_item_id,
                    expected_status=InspectionWorkItemStatus.RUNNING,
                    target_status=target_status,
                    row_version=self._row_version,
                    lease_token=self.lease.lease_token,
                    next_attempt_at=next_attempt_at,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            self._lease_released = True

    async def terminate_web_contract_incompatibility(self, *, command) -> None:
        """仅认证 Web 合同不兼容调用；保留原判定，技术终结而非产品失败。"""
        from services.api.app.application.inspection_work_items import (
            InspectionWorkItemConflictError,
        )

        if (command.work_item_id != self.lease.work_item_id
            or command.attempt_id != self._require_attempt_id()
            or command.lease_token != self.lease.lease_token
            or command.agent_run_id != self._consumer_agent_run_id):
            raise InspectionWorkItemConflictError("Web 技术收口命令不是当前执行租约")
        async with self._state_lock:
            self._row_version = await self._service.terminate_web_contract_incompatibility(command=command)
            self._attempt_status = InspectionAttemptStatus.TERMINAL_ERROR
            self._lease_released = True

    @asynccontextmanager
    async def lease_heartbeat(self) -> AsyncIterator[None]:
        """长 Judge 调用期间按租约三分之一周期续租。"""
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            self._renew_until_stopped(stop_event),
            name=f"alioth-inspection-lease-{self.lease.work_item_id}",
        )
        try:
            yield
        finally:
            stop_event.set()
            await task

    async def _renew_until_stopped(self, stop_event: asyncio.Event) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            if self._lease_released:
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            async with self._state_lock:
                if self._lease_released or stop_event.is_set():
                    return
                self._row_version = await self._service.renew_lease(
                    RenewInspectionWorkItemLeaseCommand(
                        work_item_id=self.lease.work_item_id,
                        consumer_agent_run_id=self._consumer_agent_run_id,
                        lease_token=self.lease.lease_token,
                        row_version=self._row_version,
                        lease_seconds=self._lease_seconds,
                        allow_terminal_web_owner_recovery=(
                            self._allow_terminal_web_owner_recovery
                        ),
                    )
                )
                logger.info(
                    "inspection_lease_renewed task_id=%s work_item_id=%s "
                    "consumer_agent_run_id=%s row_version=%d",
                    self.lease.task_id,
                    self.lease.work_item_id,
                    self._consumer_agent_run_id,
                    self._row_version,
                )

    async def _finish_attempt_written(self) -> None:
        attempt_id = self._require_attempt_id()
        if self._attempt_status != InspectionAttemptStatus.JUDGED:
            return
        await self._service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=attempt_id,
                expected_status=InspectionAttemptStatus.JUDGED,
                target_status=InspectionAttemptStatus.WRITTEN,
                lease_token=self.lease.lease_token,
            )
        )
        self._attempt_status = InspectionAttemptStatus.WRITTEN

    async def _finish_work_item(self, target_status: InspectionWorkItemStatus) -> None:
        async with self._state_lock:
            self._row_version = await self._service.transition(
                TransitionInspectionWorkItemCommand(
                    work_item_id=self.lease.work_item_id,
                    expected_status=InspectionWorkItemStatus.RUNNING,
                    target_status=target_status,
                    row_version=self._row_version,
                    lease_token=self.lease.lease_token,
                )
            )
            self._lease_released = True

    def _require_attempt_id(self) -> str:
        if self._attempt_id is None:
            raise RuntimeError("持久化质检 Attempt 尚未启动")
        return self._attempt_id


def _judgement_items(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_items = result.get("items")
    if isinstance(raw_items, list):
        return [dict(item) for item in raw_items if isinstance(item, Mapping)]
    return [dict(result)]


def _evidence_requests_cover_unresolved_rules(
    result: Mapping[str, Any],
    *,
    lease: InspectionWorkItemLease,
    synced: InspectionWorkItemSyncResult,
) -> bool:
    """确认 H5 尚无正式结果的成员都已进入补采状态机。"""

    if str(lease.target_platform or "").strip().casefold() != "h5":
        return False

    terminal_rule_codes = {link.rule_code for link in synced.links}
    unresolved_rule_codes = {
        rule.rule_code for rule in lease.rules if rule.rule_code not in terminal_rule_codes
    }
    if not unresolved_rule_codes:
        return False
    raw_requests = result.get("evidence_requests")
    if not isinstance(raw_requests, list):
        return False
    admitted_request_ids = result.get("admitted_evidence_request_ids")
    if not isinstance(admitted_request_ids, list):
        return False
    admitted = {
        request_id
        for value in admitted_request_ids
        if (request_id := _text(value)) is not None
    }
    requested_rule_codes = {
        rule_code
        for item in raw_requests
        if isinstance(item, Mapping)
        and _text(item.get("request_id")) in admitted
        and _text(item.get("work_item_id")) == lease.work_item_id
        and (rule_code := _text(item.get("rule_code"))) is not None
    }
    return unresolved_rule_codes <= requested_rule_codes


# 持久化校验（inspection_work_items._reject_raw_evidence）拒绝深度>12 的 result_payload；
# 生产端按 10 层提前压平，留 2 层余量，避免深层 Judge 输出把整个 Attempt 卡成不可持久化。
_ATTEMPT_PAYLOAD_MAX_DEPTH = 10
_FLATTENED_BRANCH_MAX_CHARS = 2000


def _sanitize_attempt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Attempt 只持久化判定摘要和证据引用，禁止携带截图/DOM 原文。"""
    sanitized = _sanitize_attempt_value(value)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def _sanitize_attempt_value(value: Any, *, depth: int | None = 0) -> Any:
    if (
        depth is not None
        and depth >= _ATTEMPT_PAYLOAD_MAX_DEPTH
        and isinstance(value, (Mapping, list, tuple))
    ):
        return _flatten_deep_branch(value)
    next_depth = None if depth is None else depth + 1
    if isinstance(value, Mapping):
        payload: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_raw_evidence_key(key):
                continue
            payload[key] = _sanitize_attempt_value(item, depth=next_depth)
        return payload
    if isinstance(value, list):
        return [_sanitize_attempt_value(item, depth=next_depth) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_attempt_value(item, depth=next_depth) for item in value]
    if isinstance(value, str) and value.strip().casefold().startswith("data:"):
        return "[raw evidence data url omitted]"
    return value


def _flatten_deep_branch(value: Any) -> str:
    """深层分支压平为字符串摘要：先按同一规则剔除原始证据，再序列化截断。"""
    cleaned = _sanitize_attempt_value(value, depth=None)
    text = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > _FLATTENED_BRANCH_MAX_CHARS:
        text = f"{text[:_FLATTENED_BRANCH_MAX_CHARS]}…[已截断]"
    return f"[嵌套超限压平] {text}"


def _is_raw_evidence_key(raw_key: str) -> bool:
    key = raw_key.casefold()
    evidence_reference = key.endswith(("_id", "_ids"))
    bounded_evidence_metadata = key.endswith(
        (
            "_count",
            "_size",
            "_bytes",
            "_hash",
            "_checksum",
            "_present",
            "_available",
            "_type",
            "_format",
            "_summary",
            "_excerpt",
        )
    )
    if evidence_reference or bounded_evidence_metadata:
        return False
    return any(
        key == marker or key.startswith(f"{marker}_") or key.endswith(f"_{marker}")
        for marker in (
            "dom",
            "html",
            "screenshot",
            "base64",
            "page_content",
            "image_payload",
        )
    )


async def replay_recoverable_judgement(
    *,
    lease: InspectionWorkItemLease,
    result_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """用已持久化 Judge 输出补做正式写回，禁止再次请求判定模型。"""
    from services.api.app.agents.tools.rule_tools import (
        RuleExecutionBatchItem,
        record_rule_executions_for_current_run,
    )

    allowed_rule_codes = {rule.rule_code for rule in lease.rules}
    executions: list[RuleExecutionBatchItem] = []
    for item in _judgement_items(result_payload):
        rule_code = _text(item.get("rule_code"))
        if rule_code not in allowed_rule_codes or _text(item.get("execution_status")) is None:
            continue
        executions.append(RuleExecutionBatchItem.model_validate(item))
    if not executions:
        return {
            "execution_status": "blocked",
            "page_capture_id": lease.page_capture_id,
            "retryable": False,
            "error": {
                "code": "INSPECTION_ATTEMPT_RECOVERY_PAYLOAD_INVALID",
                "message": "持久化 Judge 输出未包含当前未完成成员",
            },
        }
    recorded = await record_rule_executions_for_current_run(
        page_capture_id=lease.page_capture_id,
        executions=executions,
    )
    return {**recorded, "recovered_from_attempt": True}


def tool_result_from_execution_links(
    *,
    page_capture_id: str,
    links: tuple[Any, ...],
) -> dict[str, Any]:
    items = [
        {
            "status": "ok",
            "rule_id": link.rule_id,
            "rule_code": link.rule_code,
            "page_capture_id": page_capture_id,
            "execution_status": link.execution_status,
            "observation_packet_id": link.observation_packet_id,
            "execution_id": link.rule_execution_id,
            "persisted": True,
            "recovered": True,
        }
        for link in links
    ]
    if len(items) == 1:
        return items[0]
    return {
        "execution_status": "success",
        "page_capture_id": page_capture_id,
        "succeeded_count": len(items),
        "failed_count": 0,
        "items": items,
        "persisted": True,
        "recovered": True,
    }


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
