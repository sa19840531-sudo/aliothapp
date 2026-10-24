"""H5 证据到共享质检工作的持久化 Publication 聚合。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid5

H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION = "h5-inspection-publication.v1"
H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE = "alioth.v2.h5-inspection-publication.available"
_PUBLICATION_NAMESPACE = UUID("7c1cd8cb-e135-5c0b-ae7c-8d714f83a37a")


class H5InspectionPublicationState(StrEnum):
    """Publication 的完整持久化状态机。"""

    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    PUBLISHED = "published"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


_TERMINAL_STATES = frozenset(
    {
        H5InspectionPublicationState.PUBLISHED,
        H5InspectionPublicationState.NOT_APPLICABLE,
        H5InspectionPublicationState.NEEDS_REVIEW,
        H5InspectionPublicationState.FAILED,
        H5InspectionPublicationState.CANCELLED,
        H5InspectionPublicationState.SUPERSEDED,
    }
)


class H5InspectionPublicationValidationError(ValueError):
    """Publication 身份、状态或围栏校验失败。"""


@dataclass(frozen=True, kw_only=True)
class H5InspectionPublication:
    """不携带证据正文的 H5 规则发布待办。"""

    publication_id: str
    task_id: str
    source_agent_run_id: str
    page_capture_id: str
    page_type_assessment_id: str
    assessment_hash: str
    ruleset_id: str
    ruleset_version: int
    ruleset_snapshot_hash: str | None
    finalization_epoch: int
    input_hash: str
    status: H5InspectionPublicationState
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    row_version: int
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    candidate_rule_count: int | None = None
    expected_batch_count: int | None = None
    published_work_item_count: int = 0
    excluded_active_session_rule_count: int = 0
    consumer_agent_run_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "publication_id",
            "task_id",
            "source_agent_run_id",
            "page_capture_id",
            "page_type_assessment_id",
            "assessment_hash",
            "ruleset_id",
            "input_hash",
        ):
            if not str(getattr(self, field_name) or "").strip():
                raise H5InspectionPublicationValidationError(f"{field_name} 不能为空")
        if not isinstance(self.status, H5InspectionPublicationState):
            raise H5InspectionPublicationValidationError("status 类型无效")
        if self.ruleset_version <= 0 or self.finalization_epoch < 0:
            raise H5InspectionPublicationValidationError("规则版本或任务 epoch 无效")
        if not 0 <= self.attempt_count <= self.max_attempts or self.max_attempts <= 0:
            raise H5InspectionPublicationValidationError("尝试次数无效")
        if self.row_version <= 0:
            raise H5InspectionPublicationValidationError("消息版本必须为正数")
        leased = self.status is H5InspectionPublicationState.LEASED
        lease_fields = (self.lease_owner, self.lease_token, self.lease_expires_at)
        if leased != all(value is not None for value in lease_fields):
            raise H5InspectionPublicationValidationError("租约字段与状态不一致")
        if not leased and any(value is not None for value in lease_fields):
            raise H5InspectionPublicationValidationError("非租约状态不得保留租约")
        terminal = self.status in _TERMINAL_STATES
        if terminal != (self.finished_at is not None):
            raise H5InspectionPublicationValidationError("终态时间与状态不一致")


def build_h5_inspection_publication_id(
    *,
    task_id: str,
    finalization_epoch: int,
    page_capture_id: str,
    assessment_id: str,
    ruleset_snapshot_hash: str | None,
) -> str:
    """由锁定业务身份派生跨进程稳定 UUID。"""

    parts = (
        _required_text(task_id, "task_id"),
        str(finalization_epoch),
        _required_text(page_capture_id, "page_capture_id"),
        _required_text(assessment_id, "assessment_id"),
        str(ruleset_snapshot_hash or ""),
    )
    if finalization_epoch < 0:
        raise H5InspectionPublicationValidationError("finalization_epoch 不能为负数")
    return str(uuid5(_PUBLICATION_NAMESPACE, ":".join(parts)))


def lease_h5_inspection_publication(
    publication: H5InspectionPublication,
    *,
    consumer_id: str,
    expected_version: int,
    lease_token: str,
    lease_expires_at: datetime,
    now: datetime,
) -> H5InspectionPublication:
    """按消息版本领取待办并设置数据库写回围栏。"""

    if publication.status not in {
        H5InspectionPublicationState.PENDING,
        H5InspectionPublicationState.RETRY_WAIT,
    }:
        raise H5InspectionPublicationValidationError("Publication 当前状态不可领取")
    if publication.row_version != expected_version:
        raise H5InspectionPublicationValidationError("Publication 消息版本已过期")
    if publication.attempt_count >= publication.max_attempts:
        raise H5InspectionPublicationValidationError("Publication 尝试次数已耗尽")
    if lease_expires_at <= now:
        raise H5InspectionPublicationValidationError("租约到期时间必须晚于当前时间")
    return replace(
        publication,
        status=H5InspectionPublicationState.LEASED,
        lease_owner=_required_text(consumer_id, "consumer_id"),
        lease_token=_required_text(lease_token, "lease_token"),
        lease_expires_at=lease_expires_at,
        attempt_count=publication.attempt_count + 1,
        updated_at=now,
    )


def retry_h5_inspection_publication(
    publication: H5InspectionPublication,
    *,
    lease_token: str,
    next_attempt_at: datetime,
    error_code: str,
    error_message: str,
    now: datetime,
) -> H5InspectionPublication:
    """释放失败租约并创建下一轮独立消息版本。"""

    _require_active_lease(publication, lease_token)
    if publication.attempt_count >= publication.max_attempts:
        raise H5InspectionPublicationValidationError("Publication 尝试次数已耗尽")
    if next_attempt_at <= now:
        raise H5InspectionPublicationValidationError("下一次尝试时间必须晚于当前时间")
    return replace(
        publication,
        status=H5InspectionPublicationState.RETRY_WAIT,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        next_attempt_at=next_attempt_at,
        row_version=publication.row_version + 1,
        failure_code=_required_text(error_code, "error_code"),
        failure_message=_bounded_message(error_message),
        updated_at=now,
    )


def finish_h5_inspection_publication(
    publication: H5InspectionPublication,
    *,
    lease_token: str,
    status: H5InspectionPublicationState,
    candidate_rule_count: int,
    expected_batch_count: int,
    published_work_item_count: int,
    excluded_active_session_rule_count: int,
    consumer_agent_run_id: str | None,
    now: datetime,
) -> H5InspectionPublication:
    """以当前租约原子写入计划统计和业务终态。"""

    if publication.status in _TERMINAL_STATES:
        raise H5InspectionPublicationValidationError("Publication 已是终态")
    _require_active_lease(publication, lease_token)
    if status not in {
        H5InspectionPublicationState.PUBLISHED,
        H5InspectionPublicationState.NOT_APPLICABLE,
        H5InspectionPublicationState.NEEDS_REVIEW,
        H5InspectionPublicationState.FAILED,
    }:
        raise H5InspectionPublicationValidationError("目标终态不允许由 Consumer 写入")
    counts = (
        candidate_rule_count,
        expected_batch_count,
        published_work_item_count,
        excluded_active_session_rule_count,
    )
    if any(value < 0 for value in counts):
        raise H5InspectionPublicationValidationError("计划统计不能为负数")
    return replace(
        publication,
        status=status,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        candidate_rule_count=candidate_rule_count,
        expected_batch_count=expected_batch_count,
        published_work_item_count=published_work_item_count,
        excluded_active_session_rule_count=excluded_active_session_rule_count,
        consumer_agent_run_id=consumer_agent_run_id,
        row_version=publication.row_version + 1,
        finished_at=now,
        updated_at=now,
    )


def build_h5_inspection_publication_available(
    publication: H5InspectionPublication,
    *,
    occurred_at: datetime,
) -> dict[str, object]:
    """构造只含稳定引用的 Publication 可用事件。"""

    return {
        "message_type": H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
        "aggregate_type": "h5_inspection_publication",
        "aggregate_id": publication.publication_id,
        "aggregate_version": publication.row_version,
        "task_id": publication.task_id,
        "idempotency_key": (
            f"h5-inspection-publication:{publication.publication_id}:{publication.row_version}"
        ),
        "occurred_at": occurred_at.isoformat(),
        "payload": {
            "protocol_version": H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
            "publication_id": publication.publication_id,
            "task_id": publication.task_id,
            "finalization_epoch": publication.finalization_epoch,
            "message_version": publication.row_version,
        },
    }


def _require_active_lease(
    publication: H5InspectionPublication,
    lease_token: str,
) -> None:
    if publication.status is not H5InspectionPublicationState.LEASED:
        raise H5InspectionPublicationValidationError("Publication 当前没有活动租约")
    if publication.lease_token != lease_token:
        raise H5InspectionPublicationValidationError("Publication 租约围栏不匹配")


def _required_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise H5InspectionPublicationValidationError(f"{field_name} 不能为空")
    return text


def _bounded_message(value: str) -> str:
    return _required_text(value, "error_message")[:1000]
