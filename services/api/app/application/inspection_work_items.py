"""页面质检持久化工作项协议、状态机与应用服务。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

INSPECTION_WORK_ITEM_PROTOCOL_VERSION = "inspection-work-item.v1"
MAX_INSPECTION_ATTEMPT_PAYLOAD_BYTES = 256_000
INSPECTION_WORK_ITEM_CONTROL_MODES = frozenset({"shadow", "enforced"})


class InspectionWorkItemStatus(StrEnum):
    """持久化质检批次状态。"""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    WAITING_EVIDENCE = "waiting_evidence"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class InspectionWorkItemRuleStatus(StrEnum):
    """持久化质检批次内单条规则状态。"""

    PENDING = "pending"
    JUDGED = "judged"
    WRITTEN = "written"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    ERROR = "error"
    SKIPPED = "skipped"


class InspectionAttemptStatus(StrEnum):
    """一次 Judge 尝试的持久化状态。"""

    RUNNING = "running"
    JUDGED = "judged"
    WRITTEN = "written"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"
    ABANDONED = "abandoned"


TERMINAL_WORK_ITEM_STATUSES = frozenset(
    {
        InspectionWorkItemStatus.SUCCEEDED,
        InspectionWorkItemStatus.PARTIAL,
        InspectionWorkItemStatus.FAILED,
        InspectionWorkItemStatus.CANCELLED,
        InspectionWorkItemStatus.SUPERSEDED,
    }
)

_WORK_ITEM_TRANSITIONS = {
    InspectionWorkItemStatus.LEASED: frozenset(
        {
            InspectionWorkItemStatus.RUNNING,
            InspectionWorkItemStatus.RETRY_WAIT,
            InspectionWorkItemStatus.WAITING_EVIDENCE,
            InspectionWorkItemStatus.FAILED,
            InspectionWorkItemStatus.CANCELLED,
            InspectionWorkItemStatus.SUPERSEDED,
        }
    ),
    InspectionWorkItemStatus.RUNNING: frozenset(
        {
            InspectionWorkItemStatus.RETRY_WAIT,
            InspectionWorkItemStatus.WAITING_EVIDENCE,
            InspectionWorkItemStatus.SUCCEEDED,
            InspectionWorkItemStatus.PARTIAL,
            InspectionWorkItemStatus.FAILED,
            InspectionWorkItemStatus.CANCELLED,
            InspectionWorkItemStatus.SUPERSEDED,
        }
    ),
    InspectionWorkItemStatus.WAITING_EVIDENCE: frozenset(
        {
            InspectionWorkItemStatus.PENDING,
            InspectionWorkItemStatus.SUCCEEDED,
            InspectionWorkItemStatus.PARTIAL,
            InspectionWorkItemStatus.FAILED,
            InspectionWorkItemStatus.CANCELLED,
            InspectionWorkItemStatus.SUPERSEDED,
        }
    ),
}

_ATTEMPT_TRANSITIONS = {
    InspectionAttemptStatus.RUNNING: frozenset(
        {
            InspectionAttemptStatus.JUDGED,
            InspectionAttemptStatus.RETRYABLE_ERROR,
            InspectionAttemptStatus.TERMINAL_ERROR,
            InspectionAttemptStatus.ABANDONED,
        }
    ),
    InspectionAttemptStatus.JUDGED: frozenset(
        {
            InspectionAttemptStatus.WRITTEN,
            InspectionAttemptStatus.RETRYABLE_ERROR,
            InspectionAttemptStatus.TERMINAL_ERROR,
            InspectionAttemptStatus.ABANDONED,
        }
    ),
}

_RULE_TRANSITIONS = {
    InspectionWorkItemRuleStatus.PENDING: frozenset(
        {
            InspectionWorkItemRuleStatus.JUDGED,
            InspectionWorkItemRuleStatus.WRITTEN,
            InspectionWorkItemRuleStatus.EVIDENCE_INSUFFICIENT,
            InspectionWorkItemRuleStatus.ERROR,
            InspectionWorkItemRuleStatus.SKIPPED,
        }
    ),
    InspectionWorkItemRuleStatus.JUDGED: frozenset(
        {
            InspectionWorkItemRuleStatus.WRITTEN,
            InspectionWorkItemRuleStatus.EVIDENCE_INSUFFICIENT,
            InspectionWorkItemRuleStatus.ERROR,
            InspectionWorkItemRuleStatus.SKIPPED,
        }
    ),
}

_RULE_CONTRACT_FIELDS = (
    "rule_id",
    "rule_code",
    "category",
    "executor_type",
    "severity",
    "check_point",
    "input_contract",
    "judge_config",
    "manual_review_policy",
    "execution_route",
    "steps",
)


@dataclass(frozen=True)
class InspectionWorkItemRule:
    """批次中的规则成员及其不可变契约摘要。"""

    rule_id: str
    rule_code: str
    ordinal: int
    contract_hash: str
    evidence_input_set_id: str | None = None


@dataclass(frozen=True)
class InspectionRuleEvidenceBinding:
    """一条规则通过已满足 Evidence Request 锁定的补采 Manifest。"""

    rule_id: str
    rule_code: str
    page_capture_id: str
    producer_agent_run_id: str
    manifest_id: str
    manifest_version: int
    artifact_ids: tuple[str, ...]
    observation_packet_ids: tuple[str, ...] = ()
    # 补采接上时重新封存的输入集合；旧补采（封存上线前）没有，按原样重判
    evidence_input_set_id: str | None = None
    evidence_input_set_hash: str | None = None
    evidence_capture_roles: tuple[tuple[str, str], ...] = ()
    evidence_manifest_refs: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "rule_id",
            "rule_code",
            "page_capture_id",
            "producer_agent_run_id",
            "manifest_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        if (self.evidence_input_set_id is None) != (self.evidence_input_set_hash is None):
            raise InspectionWorkItemValidationError(
                "evidence_input_set_id 与 evidence_input_set_hash 必须同时提供"
            )
        roles: list[tuple[str, str]] = []
        for item in self.evidence_capture_roles:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise InspectionWorkItemValidationError(
                    "evidence_capture_roles 必须为角色与 Capture ID 二元组"
                )
            role, capture_id = item
            _require_text(role, "evidence_capture_roles.role")
            _require_text(capture_id, "evidence_capture_roles.capture_id")
            roles.append((role, capture_id))
        if len({role for role, _ in roles}) != len(roles):
            raise InspectionWorkItemValidationError("evidence_capture_roles 不能重复角色")
        object.__setattr__(self, "evidence_capture_roles", tuple(roles))
        manifest_refs = tuple(self.evidence_manifest_refs)
        for role, manifest_id, version in manifest_refs:
            _require_text(role, "evidence_manifest_refs.role")
            _require_text(manifest_id, "evidence_manifest_refs.manifest_id")
            _require_text(version, "evidence_manifest_refs.manifest_version")
        if len({role for role, _, _ in manifest_refs}) != len(manifest_refs):
            raise InspectionWorkItemValidationError("evidence_manifest_refs 不能重复角色")
        object.__setattr__(self, "evidence_manifest_refs", manifest_refs)
        if self.manifest_version <= 0:
            raise InspectionWorkItemValidationError("manifest_version 必须大于 0")
        if not self.artifact_ids:
            raise InspectionWorkItemValidationError("补采 Manifest 必须包含 Artifact")
        for field_name, values in (
            ("artifact_ids", self.artifact_ids),
            ("observation_packet_ids", self.observation_packet_ids),
        ):
            if len(set(values)) != len(values):
                raise InspectionWorkItemValidationError(f"{field_name} 不能重复")
            for value in values:
                _require_text(value, field_name)


@dataclass(frozen=True)
class PublishInspectionWorkItemCommand:
    """发布一个与 execution_plan 批次一致的持久化工作项。"""

    task_id: str
    page_capture_id: str
    producer_agent_run_id: str
    ruleset_id: str
    ruleset_version: int
    batch_key: str
    strategy: str
    session_dependency: str
    rules: tuple[InspectionWorkItemRule, ...]
    ruleset_snapshot_hash: str | None = None
    category: str | None = None
    session_owner_agent_run_id: str | None = None
    priority: int = 100
    max_attempts: int = 3
    control_mode: str = "enforced"
    defer_wakeup: bool = False
    protocol_version: str = INSPECTION_WORK_ITEM_PROTOCOL_VERSION


@dataclass(frozen=True)
class PublishedInspectionWorkItem:
    """幂等发布后的工作项身份。"""

    work_item_id: str
    status: InspectionWorkItemStatus
    input_hash: str
    created: bool


@dataclass(frozen=True)
class ClaimInspectionWorkItemCommand:
    """按任务、会话依赖和租约所有者领取一个批次。"""

    task_id: str
    consumer_agent_run_id: str
    lease_owner: str
    lease_seconds: int
    session_dependency: str | None = None
    page_capture_id: str | None = None
    rule_codes: tuple[str, ...] = ()
    allow_terminal_web_owner_recovery: bool = False


@dataclass(frozen=True)
class FindCompletedInspectionWorkItemCommand:
    """按页面和精确规则批次读取已完成的正式结果。"""

    task_id: str
    page_capture_id: str
    rule_codes: tuple[str, ...]


@dataclass(frozen=True)
class InspectionWorkItemLease:
    """成功领取的工作项及其租约。"""

    work_item_id: str
    task_id: str
    page_capture_id: str
    batch_key: str
    strategy: str
    category: str | None
    session_dependency: str
    input_hash: str
    producer_agent_run_id: str
    consumer_agent_run_id: str
    lease_token: str
    attempt_no: int
    row_version: int
    rules: tuple[InspectionWorkItemRule, ...]
    recoverable_result_payload: dict[str, Any] | None = None
    target_platform: str | None = None
    work_item_input_hash: str | None = None
    rule_evidence: tuple[InspectionRuleEvidenceBinding, ...] = ()


@dataclass(frozen=True)
class InspectionWorkItemExecutionLink:
    """规则成员与正式规则执行结果的持久化关联。"""

    rule_id: str
    rule_code: str
    execution_status: str
    observation_packet_id: str | None
    rule_execution_id: str


@dataclass(frozen=True)
class InspectionWorkItemSyncResult:
    """有效租约下从正式规则结果同步工作项成员后的摘要。"""

    work_item_id: str
    expected_rule_count: int
    terminal_rule_count: int
    pending_rule_count: int
    written_rule_count: int
    links: tuple[InspectionWorkItemExecutionLink, ...]

    @property
    def terminal_status(self) -> InspectionWorkItemStatus | None:
        if self.pending_rule_count > 0:
            return None
        if self.expected_rule_count == self.written_rule_count:
            return InspectionWorkItemStatus.SUCCEEDED
        return InspectionWorkItemStatus.PARTIAL


@dataclass(frozen=True)
class CompletedInspectionWorkItemResult:
    """已完成工作项及其正式规则执行结果。"""

    work_item_id: str
    page_capture_id: str
    links: tuple[InspectionWorkItemExecutionLink, ...]


@dataclass(frozen=True)
class InspectionWorkItemFinalization:
    """任务收口时持久化工作项的恢复与失败关闭摘要。"""

    task_id: str
    recovered_work_item_count: int
    failed_work_item_count: int
    active_session_failed_count: int
    abandoned_attempt_count: int
    deferred_retry_count: int = 0


@dataclass(frozen=True)
class InspectionWorkItemClosureSettlement:
    """收口前对停滞工作项的结算摘要；按停滞原因分类计数，便于排障。"""

    task_id: str
    settled_work_item_count: int = 0
    settled_rule_count: int = 0
    reason_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.settled_work_item_count > 0


@dataclass(frozen=True)
class RenewInspectionWorkItemLeaseCommand:
    """在租约过期前续租，并同步推进乐观版本。"""

    work_item_id: str
    consumer_agent_run_id: str
    lease_token: str
    row_version: int
    lease_seconds: int
    allow_terminal_web_owner_recovery: bool = False


@dataclass(frozen=True)
class TransitionInspectionWorkItemCommand:
    """使用租约和乐观版本推进工作项状态。"""

    work_item_id: str
    expected_status: InspectionWorkItemStatus
    target_status: InspectionWorkItemStatus
    row_version: int
    lease_token: str | None = None
    next_attempt_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class StartInspectionAttemptCommand:
    """在有效租约下登记一次 Judge 尝试。"""

    work_item_id: str
    consumer_agent_run_id: str
    attempt_no: int
    lease_token: str
    input_hash: str
    work_item_input_hash: str | None = None


@dataclass(frozen=True)
class TransitionInspectionAttemptCommand:
    """持久化 Judge 输出或尝试终态。"""

    attempt_id: str
    expected_status: InspectionAttemptStatus
    target_status: InspectionAttemptStatus
    lease_token: str
    result_payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class TransitionInspectionWorkItemRuleCommand:
    """在有效工作项租约下推进单条规则成员。"""

    work_item_id: str
    rule_id: str
    expected_status: InspectionWorkItemRuleStatus
    target_status: InspectionWorkItemRuleStatus
    lease_token: str
    observation_packet_id: str | None = None
    rule_execution_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class InspectionWorkItemValidationError(ValueError):
    """持久化工作项输入或状态转换不符合协议。"""


class InspectionWorkItemConflictError(RuntimeError):
    """租约、行版本或状态已被其他执行者推进。"""


class InspectionWorkItemRepository(Protocol):
    """持久化质检工作项基础仓储契约。"""

    async def publish(
        self,
        *,
        command: PublishInspectionWorkItemCommand,
        input_hash: str,
    ) -> PublishedInspectionWorkItem: ...

    async def claim(
        self,
        *,
        command: ClaimInspectionWorkItemCommand,
        lease_token: str,
    ) -> InspectionWorkItemLease | None: ...

    async def find_completed_result(
        self,
        *,
        command: FindCompletedInspectionWorkItemCommand,
    ) -> CompletedInspectionWorkItemResult | None: ...

    async def transition(
        self,
        *,
        command: TransitionInspectionWorkItemCommand,
    ) -> int | None: ...

    async def renew_lease(
        self,
        *,
        command: RenewInspectionWorkItemLeaseCommand,
    ) -> int | None: ...

    async def start_attempt(
        self,
        *,
        command: StartInspectionAttemptCommand,
    ) -> str: ...

    async def transition_attempt(
        self,
        *,
        command: TransitionInspectionAttemptCommand,
    ) -> bool: ...

    async def transition_rule(
        self,
        *,
        command: TransitionInspectionWorkItemRuleCommand,
    ) -> bool: ...

    async def sync_formal_results(
        self,
        *,
        work_item_id: str,
        lease_token: str,
    ) -> InspectionWorkItemSyncResult: ...

    async def has_nonterminal_work(self, *, task_id: str) -> bool: ...

    async def find_active_consumer_run(self, *, task_id: str) -> str | None: ...

    async def count_inflight_leased_work(self, *, task_id: str) -> int: ...

    async def count_claimable_pending_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int: ...

    async def count_waitable_nonterminal_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int: ...

    async def finish_h5_consumer_run_if_idle(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool: ...

    async def finish_publication_consumer_run_if_idle(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool: ...

    async def record_consumer_usage(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        usage: Mapping[str, Any],
    ) -> bool: ...

    async def reopen_waiting_evidence(self, *, task_id: str) -> int: ...

    async def cancel_nonterminal(self, *, task_id: str, reason: str) -> int: ...

    async def terminate_web_contract_incompatibility(self, *, command) -> int: ...

    async def finalize_task_work(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
    ) -> InspectionWorkItemFinalization: ...

    async def settle_stalled_work_for_closure(
        self,
        *,
        task_id: str,
        active_session_agent_run_id: str | None = None,
        snapshot_generation_closed: bool = False,
        active_session_exhausted_only: bool = False,
    ) -> InspectionWorkItemClosureSettlement: ...


class InspectionWorkItemService:
    """校验协议并通过仓储原子推进持久化质检工作。"""

    def __init__(self, *, repository: InspectionWorkItemRepository) -> None:
        self._repository = repository

    async def publish(
        self,
        command: PublishInspectionWorkItemCommand,
    ) -> PublishedInspectionWorkItem:
        validate_inspection_work_item_publish_command(command)
        input_hash = build_inspection_work_item_input_hash(command)
        return await self._repository.publish(command=command, input_hash=input_hash)

    async def read_original_page_inputs(
        self, *, task_id, source_agent_run_id, page_capture_id, rule_contracts,
    ):
        reader = getattr(self._repository, "read_original_page_inputs", None)
        if reader is None:
            return {}
        return await reader(task_id=task_id, source_agent_run_id=source_agent_run_id,
                            page_capture_id=page_capture_id, rule_contracts=rule_contracts)

    async def claim(
        self,
        command: ClaimInspectionWorkItemCommand,
    ) -> InspectionWorkItemLease | None:
        _require_text(command.task_id, "task_id")
        _require_text(command.consumer_agent_run_id, "consumer_agent_run_id")
        _require_text(command.lease_owner, "lease_owner")
        if command.lease_seconds <= 0:
            raise InspectionWorkItemValidationError("lease_seconds 必须大于 0")
        if command.session_dependency not in {None, "snapshot", "active_session"}:
            raise InspectionWorkItemValidationError("session_dependency 不受支持")
        if not isinstance(command.allow_terminal_web_owner_recovery, bool):
            raise InspectionWorkItemValidationError(
                "allow_terminal_web_owner_recovery 必须为布尔值"
            )
        if (
            command.allow_terminal_web_owner_recovery
            and command.session_dependency != "active_session"
        ):
            raise InspectionWorkItemValidationError(
                "终态 Web owner 恢复只允许 active_session 工作"
            )
        if command.page_capture_id is not None:
            _require_text(command.page_capture_id, "page_capture_id")
        normalized_rule_codes = tuple(
            rule_code.strip() for rule_code in command.rule_codes if rule_code.strip()
        )
        if len(normalized_rule_codes) != len(command.rule_codes):
            raise InspectionWorkItemValidationError("rule_codes 不能包含空值")
        if len(set(normalized_rule_codes)) != len(normalized_rule_codes):
            raise InspectionWorkItemValidationError("rule_codes 不能重复")
        return await self._repository.claim(
            command=(
                command
                if normalized_rule_codes == command.rule_codes
                else ClaimInspectionWorkItemCommand(
                    task_id=command.task_id,
                    consumer_agent_run_id=command.consumer_agent_run_id,
                    lease_owner=command.lease_owner,
                    lease_seconds=command.lease_seconds,
                    session_dependency=command.session_dependency,
                    page_capture_id=command.page_capture_id,
                    rule_codes=normalized_rule_codes,
                    allow_terminal_web_owner_recovery=(
                        command.allow_terminal_web_owner_recovery
                    ),
                )
            ),
            lease_token=str(uuid4()),
        )

    async def find_completed_result(
        self,
        command: FindCompletedInspectionWorkItemCommand,
    ) -> CompletedInspectionWorkItemResult | None:
        _require_text(command.task_id, "task_id")
        _require_text(command.page_capture_id, "page_capture_id")
        normalized_rule_codes = tuple(
            rule_code.strip() for rule_code in command.rule_codes if rule_code.strip()
        )
        if not normalized_rule_codes:
            raise InspectionWorkItemValidationError("rule_codes 不能为空")
        if len(normalized_rule_codes) != len(command.rule_codes):
            raise InspectionWorkItemValidationError("rule_codes 不能包含空值")
        if len(set(normalized_rule_codes)) != len(normalized_rule_codes):
            raise InspectionWorkItemValidationError("rule_codes 不能重复")
        return await self._repository.find_completed_result(
            command=(
                command
                if normalized_rule_codes == command.rule_codes
                else FindCompletedInspectionWorkItemCommand(
                    task_id=command.task_id,
                    page_capture_id=command.page_capture_id,
                    rule_codes=normalized_rule_codes,
                )
            )
        )

    async def transition(
        self,
        command: TransitionInspectionWorkItemCommand,
    ) -> int:
        allowed = _WORK_ITEM_TRANSITIONS.get(command.expected_status, frozenset())
        if command.target_status not in allowed:
            raise InspectionWorkItemValidationError(
                f"非法工作项状态转换：{command.expected_status.value}"
                f" -> {command.target_status.value}"
            )
        if command.row_version < 0:
            raise InspectionWorkItemValidationError("row_version 不能小于 0")
        if command.expected_status in {
            InspectionWorkItemStatus.LEASED,
            InspectionWorkItemStatus.RUNNING,
        }:
            _require_text(command.lease_token, "lease_token")
        if command.target_status == InspectionWorkItemStatus.RETRY_WAIT:
            if command.next_attempt_at is None:
                raise InspectionWorkItemValidationError("retry_wait 必须提供 next_attempt_at")
        elif command.next_attempt_at is not None:
            raise InspectionWorkItemValidationError("只有 retry_wait 可以设置 next_attempt_at")
        next_row_version = await self._repository.transition(command=command)
        if next_row_version is None:
            raise InspectionWorkItemConflictError("工作项状态、租约或 row_version 已变化")
        return next_row_version

    async def renew_lease(
        self,
        command: RenewInspectionWorkItemLeaseCommand,
    ) -> int:
        _require_text(command.work_item_id, "work_item_id")
        _require_text(command.consumer_agent_run_id, "consumer_agent_run_id")
        _require_text(command.lease_token, "lease_token")
        if command.row_version < 0:
            raise InspectionWorkItemValidationError("row_version 不能小于 0")
        if command.lease_seconds <= 0:
            raise InspectionWorkItemValidationError("lease_seconds 必须大于 0")
        if not isinstance(command.allow_terminal_web_owner_recovery, bool):
            raise InspectionWorkItemValidationError(
                "allow_terminal_web_owner_recovery 必须为布尔值"
            )
        next_row_version = await self._repository.renew_lease(command=command)
        if next_row_version is None:
            raise InspectionWorkItemConflictError("工作项租约已过期或 row_version 已变化")
        return next_row_version

    async def start_attempt(
        self,
        command: StartInspectionAttemptCommand,
    ) -> str:
        _require_text(command.work_item_id, "work_item_id")
        _require_text(command.consumer_agent_run_id, "consumer_agent_run_id")
        _require_text(command.lease_token, "lease_token")
        _require_text(command.input_hash, "input_hash")
        if command.work_item_input_hash is not None:
            _require_text(command.work_item_input_hash, "work_item_input_hash")
        if command.attempt_no <= 0:
            raise InspectionWorkItemValidationError("attempt_no 必须大于 0")
        return await self._repository.start_attempt(command=command)

    async def transition_attempt(
        self,
        command: TransitionInspectionAttemptCommand,
    ) -> None:
        allowed = _ATTEMPT_TRANSITIONS.get(command.expected_status, frozenset())
        if command.target_status not in allowed:
            raise InspectionWorkItemValidationError(
                f"非法判定尝试状态转换：{command.expected_status.value}"
                f" -> {command.target_status.value}"
            )
        _require_text(command.lease_token, "lease_token")
        if command.target_status == InspectionAttemptStatus.JUDGED:
            if not command.result_payload:
                raise InspectionWorkItemValidationError("judged 状态必须保存非空 result_payload")
        if command.result_payload is not None:
            _validate_attempt_payload(command.result_payload)
        updated = await self._repository.transition_attempt(command=command)
        if not updated:
            raise InspectionWorkItemConflictError("判定尝试状态已变化")

    async def transition_rule(
        self,
        command: TransitionInspectionWorkItemRuleCommand,
    ) -> None:
        allowed = _RULE_TRANSITIONS.get(command.expected_status, frozenset())
        if command.target_status not in allowed:
            raise InspectionWorkItemValidationError(
                f"非法规则成员状态转换：{command.expected_status.value}"
                f" -> {command.target_status.value}"
            )
        _require_text(command.work_item_id, "work_item_id")
        _require_text(command.rule_id, "rule_id")
        _require_text(command.lease_token, "lease_token")
        if command.target_status == InspectionWorkItemRuleStatus.JUDGED:
            _require_text(command.observation_packet_id, "observation_packet_id")
            if command.rule_execution_id is not None:
                raise InspectionWorkItemValidationError("judged 状态不能提前绑定 rule_execution_id")
        elif command.target_status in {
            InspectionWorkItemRuleStatus.WRITTEN,
            InspectionWorkItemRuleStatus.EVIDENCE_INSUFFICIENT,
            InspectionWorkItemRuleStatus.ERROR,
            InspectionWorkItemRuleStatus.SKIPPED,
        }:
            _require_text(command.rule_execution_id, "rule_execution_id")
        updated = await self._repository.transition_rule(command=command)
        if not updated:
            raise InspectionWorkItemConflictError("规则成员状态、租约或证据归属已变化")

    async def sync_formal_results(
        self,
        *,
        work_item_id: str,
        lease_token: str,
    ) -> InspectionWorkItemSyncResult:
        _require_text(work_item_id, "work_item_id")
        _require_text(lease_token, "lease_token")
        return await self._repository.sync_formal_results(
            work_item_id=work_item_id,
            lease_token=lease_token,
        )

    async def has_nonterminal_work(self, *, task_id: str) -> bool:
        _require_text(task_id, "task_id")
        return await self._repository.has_nonterminal_work(task_id=task_id)

    async def find_active_consumer_run(self, *, task_id: str) -> str | None:
        _require_text(task_id, "task_id")
        return await self._repository.find_active_consumer_run(task_id=task_id)

    async def count_inflight_leased_work(self, *, task_id: str) -> int:
        _require_text(task_id, "task_id")
        return await self._repository.count_inflight_leased_work(task_id=task_id)

    async def count_claimable_pending_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int:
        _require_text(task_id, "task_id")
        _require_text(consumer_agent_run_id, "consumer_agent_run_id")
        return await self._repository.count_claimable_pending_work(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )

    async def count_waitable_nonterminal_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int:
        """统计当前消费者后续仍可推进的积压，包含尚未到期的计划重试。"""
        _require_text(task_id, "task_id")
        _require_text(consumer_agent_run_id, "consumer_agent_run_id")
        return await self._repository.count_waitable_nonterminal_work(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )

    async def finish_h5_consumer_run_if_idle(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool:
        _require_text(task_id, "task_id")
        _require_text(consumer_agent_run_id, "consumer_agent_run_id")
        return await self._repository.finish_h5_consumer_run_if_idle(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )

    async def finish_publication_consumer_run_if_idle(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool:
        _require_text(task_id, "task_id")
        _require_text(consumer_agent_run_id, "consumer_agent_run_id")
        return await self._repository.finish_publication_consumer_run_if_idle(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )

    async def record_consumer_usage(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        usage: Mapping[str, Any],
    ) -> bool:
        _require_text(task_id, "task_id")
        _require_text(consumer_agent_run_id, "consumer_agent_run_id")
        if not usage:
            return False
        return await self._repository.record_consumer_usage(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
            usage=dict(usage),
        )

    async def reopen_waiting_evidence(self, *, task_id: str) -> int:
        _require_text(task_id, "task_id")
        return await self._repository.reopen_waiting_evidence(task_id=task_id)

    async def terminate_web_contract_incompatibility(self, *, command) -> int:
        """Web 专属仓储必须重验原判定和真实合同差异，不能借用旧正式结果。"""
        for field_name in ("task_id", "work_item_id", "attempt_id", "agent_run_id", "lease_token"):
            _require_text(getattr(command, field_name, None), field_name)
        return await self._repository.terminate_web_contract_incompatibility(command=command)

    async def finalize_task_work(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
    ) -> InspectionWorkItemFinalization:
        _require_text(task_id, "task_id")
        _require_text(error_code, "error_code")
        _require_text(error_message, "error_message")
        return await self._repository.finalize_task_work(
            task_id=task_id,
            error_code=error_code,
            error_message=error_message,
        )

    async def settle_stalled_work_for_closure(
        self,
        *,
        task_id: str,
        active_session_agent_run_id: str | None = None,
        snapshot_generation_closed: bool = False,
        active_session_exhausted_only: bool = False,
    ) -> InspectionWorkItemClosureSettlement:
        """收口前结算再也推进不了的工作项；正常还能重试或还在补采的一律不碰。

        关浏览器会话前调用时传 ``active_session_agent_run_id``：该 Run 名下依赖活会话的
        工作项一并结算，会话关掉后它们不允许跨会话重放，再等也拿不到证据。

        H5 主 Run 已停止生成新快照、但任务尚未进入 ``session_draining`` 时，传
        ``snapshot_generation_closed=True``。该事实只允许结算补采已终结且无法重开的
        Snapshot 工作，不放宽在途请求和剩余重试预算。

        H5 页面切换检查点传 ``active_session_exhausted_only=True``，仅结算该 Run 已耗尽
        重试且没有租约、在途补采的活会话工作；检查点不等于整个浏览器会话关闭。
        """
        _require_text(task_id, "task_id")
        run_id = (
            _require_text(active_session_agent_run_id, "active_session_agent_run_id")
            if active_session_agent_run_id is not None
            else None
        )
        if active_session_exhausted_only and run_id is None:
            raise ValueError("H5 检查点结算必须指定 active_session_agent_run_id")
        checkpoint_options = (
            {"active_session_exhausted_only": True} if active_session_exhausted_only else {}
        )
        return await self._repository.settle_stalled_work_for_closure(
            task_id=task_id,
            active_session_agent_run_id=run_id,
            snapshot_generation_closed=snapshot_generation_closed,
            **checkpoint_options,
        )


def build_inspection_rule_contract_hash(rule: Mapping[str, Any]) -> str:
    """对影响判定的规则字段生成稳定哈希，消费时可检测规则漂移。"""
    payload = {field: rule.get(field) for field in _RULE_CONTRACT_FIELDS if field in rule}
    return _stable_hash(payload)


def build_inspection_work_item_input_hash(
    command: PublishInspectionWorkItemCommand,
) -> str:
    """对工作项的不可变输入生成幂等键。"""
    payload = {
        "protocol_version": command.protocol_version,
        "task_id": command.task_id,
        "page_capture_id": command.page_capture_id,
        "ruleset_id": command.ruleset_id,
        "ruleset_version": command.ruleset_version,
        "ruleset_snapshot_hash": command.ruleset_snapshot_hash,
        "batch_key": command.batch_key,
        "strategy": command.strategy,
        "category": command.category,
        "session_dependency": command.session_dependency,
        "control_mode": command.control_mode,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "rule_code": rule.rule_code,
                "ordinal": rule.ordinal,
                "contract_hash": rule.contract_hash,
                **({"evidence_input_set_id": rule.evidence_input_set_id}
                   if rule.evidence_input_set_id is not None else {}),
            }
            for rule in command.rules
        ],
    }
    return _stable_hash(payload)


def build_inspection_attempt_input_hash(
    *,
    work_item_input_hash: str,
    rule_evidence: tuple[InspectionRuleEvidenceBinding, ...],
) -> str:
    """把补采 Manifest 与重新封存的输入集合身份纳入 Attempt 输入，防止复用补采前判定。"""
    if not rule_evidence:
        return work_item_input_hash
    return _stable_hash(
        {
            "work_item_input_hash": work_item_input_hash,
            "rule_evidence": [
                {
                    "rule_id": item.rule_id,
                    "rule_code": item.rule_code,
                    "page_capture_id": item.page_capture_id,
                    "producer_agent_run_id": item.producer_agent_run_id,
                    "manifest_id": item.manifest_id,
                    "manifest_version": item.manifest_version,
                    "artifact_ids": list(item.artifact_ids),
                    "observation_packet_ids": list(item.observation_packet_ids),
                    **({
                        "evidence_capture_roles": list(item.evidence_capture_roles),
                        "evidence_manifest_refs": list(item.evidence_manifest_refs),
                    } if item.evidence_manifest_refs else {}),
                    # 旧补采没有封存集合，不带这两个键，已有 Attempt 的哈希保持不变
                    **(
                        {
                            "evidence_input_set_id": item.evidence_input_set_id,
                            "evidence_input_set_hash": item.evidence_input_set_hash,
                        }
                        if item.evidence_input_set_id is not None
                        else {}
                    ),
                }
                for item in sorted(rule_evidence, key=lambda value: value.rule_code)
            ],
        }
    )


def validate_inspection_work_item_publish_command(
    command: PublishInspectionWorkItemCommand,
) -> None:
    """统一校验普通发布与 H5 Publication 单事务发布使用的 Work 命令。"""
    for field_name in (
        "task_id",
        "page_capture_id",
        "producer_agent_run_id",
        "ruleset_id",
        "batch_key",
        "strategy",
        "session_dependency",
        "protocol_version",
    ):
        _require_text(getattr(command, field_name), field_name)
    if command.ruleset_version <= 0:
        raise InspectionWorkItemValidationError("ruleset_version 必须大于 0")
    if command.strategy not in {"rule_engine", "llm", "hybrid"}:
        raise InspectionWorkItemValidationError("strategy 不受支持")
    if command.category not in {
        None,
        "text",
        "visual",
        "interaction",
        "ad",
        "payment",
        "input_risk",
        "player",
        "compliance",
    }:
        raise InspectionWorkItemValidationError("category 不受支持")
    if command.session_dependency not in {"snapshot", "active_session"}:
        raise InspectionWorkItemValidationError("session_dependency 不受支持")
    if command.control_mode not in INSPECTION_WORK_ITEM_CONTROL_MODES:
        raise InspectionWorkItemValidationError("control_mode 不受支持")
    if not isinstance(command.defer_wakeup, bool):
        raise InspectionWorkItemValidationError("defer_wakeup 必须为布尔值")
    if command.priority < 0:
        raise InspectionWorkItemValidationError("priority 不能小于 0")
    if command.max_attempts <= 0:
        raise InspectionWorkItemValidationError("max_attempts 必须大于 0")
    if not command.rules:
        raise InspectionWorkItemValidationError("工作项至少需要一条候选规则")
    if command.session_dependency == "active_session":
        _require_text(
            command.session_owner_agent_run_id,
            "active_session 工作项的 session_owner_agent_run_id",
        )
    rule_ids: set[str] = set()
    rule_codes: set[str] = set()
    ordinals: set[int] = set()
    for rule in command.rules:
        _require_text(rule.rule_id, "rule_id")
        _require_text(rule.rule_code, "rule_code")
        _require_text(rule.contract_hash, "contract_hash")
        if rule.ordinal < 0:
            raise InspectionWorkItemValidationError("规则 ordinal 不能小于 0")
        if rule.rule_id in rule_ids or rule.rule_code in rule_codes:
            raise InspectionWorkItemValidationError("工作项包含重复规则")
        if rule.ordinal in ordinals:
            raise InspectionWorkItemValidationError("工作项包含重复 ordinal")
        rule_ids.add(rule.rule_id)
        rule_codes.add(rule.rule_code)
        ordinals.add(rule.ordinal)


def _stable_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_attempt_payload(value: dict[str, Any]) -> None:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if len(serialized.encode("utf-8")) > MAX_INSPECTION_ATTEMPT_PAYLOAD_BYTES:
        raise InspectionWorkItemValidationError("result_payload 超出持久化大小上限")
    _reject_raw_evidence(value)


def _reject_raw_evidence(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise InspectionWorkItemValidationError("result_payload 嵌套层级过深")
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).casefold()
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
            raw_evidence_field = any(
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
            if raw_evidence_field and not evidence_reference and not bounded_evidence_metadata:
                raise InspectionWorkItemValidationError(
                    f"result_payload 禁止包含原始证据字段：{raw_key}"
                )
            _reject_raw_evidence(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_raw_evidence(item, depth=depth + 1)
        return
    if isinstance(value, str) and value.strip().casefold().startswith("data:"):
        raise InspectionWorkItemValidationError("result_payload 禁止包含 data URL")


def _require_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InspectionWorkItemValidationError(f"{field_name} 不能为空")
    return text
