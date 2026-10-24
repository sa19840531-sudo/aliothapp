"""根质检任务的统一状态、迁移矩阵与并发快照。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

TASK_STATUS_PROTOCOL_VERSION = "task-status.v1"


class TaskStatus(StrEnum):
    """`qc_task.status` 的唯一领域词汇。"""

    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """返回任务是否已停止自动执行。"""
        return self in {
            self.DONE,
            self.FAILED,
            self.CANCELED,
        }


TASK_STATUS_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = MappingProxyType(
    {
        TaskStatus.QUEUED: frozenset(
            {
                TaskStatus.RUNNING,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
            }
        ),
        TaskStatus.RUNNING: frozenset(
            {
                TaskStatus.REVIEW,
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
            }
        ),
        TaskStatus.REVIEW: frozenset(
            {
                TaskStatus.DONE,
                TaskStatus.FAILED,
            }
        ),
        # 未封存的 Legacy 任务允许人工复核重新打开；sealed 后由 Finalization 防线拒绝。
        TaskStatus.DONE: frozenset({TaskStatus.REVIEW}),
        TaskStatus.FAILED: frozenset(),
        TaskStatus.CANCELED: frozenset(),
    }
)


class TaskStatusValidationError(ValueError):
    """根任务状态或迁移命令不符合冻结规则。"""


class TaskStatusConflictError(RuntimeError):
    """任务状态、版本或 Finalization 状态已被其他执行者推进。"""


@dataclass(frozen=True, kw_only=True)
class TaskStatusSnapshot:
    """供应用层并发写回使用的根任务状态快照。"""

    task_id: str
    status: TaskStatus
    row_version: int
    finalization_state: str

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        if not isinstance(self.status, TaskStatus):
            raise TaskStatusValidationError("status 类型不正确")
        if (
            isinstance(self.row_version, bool)
            or not isinstance(self.row_version, int)
            or self.row_version < 0
        ):
            raise TaskStatusValidationError("row_version 必须为非负整数")
        _require_text(self.finalization_state, "finalization_state")


def validate_task_status_transition(
    *,
    current_status: TaskStatus,
    target_status: TaskStatus,
    finalization_state: str,
) -> None:
    """校验根任务状态迁移；同状态重放始终是幂等操作。"""
    if not isinstance(current_status, TaskStatus) or not isinstance(
        target_status,
        TaskStatus,
    ):
        raise TaskStatusValidationError("current_status/target_status 类型不正确")
    normalized_finalization = _require_text(
        finalization_state,
        "finalization_state",
    )
    if current_status is target_status:
        return
    if normalized_finalization == "sealed":
        raise TaskStatusConflictError("任务已经 sealed，拒绝迟到状态写回")
    if target_status not in TASK_STATUS_TRANSITIONS[current_status]:
        raise TaskStatusValidationError(
            f"非法任务状态转换：{current_status.value} -> {target_status.value}"
        )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TaskStatusValidationError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
