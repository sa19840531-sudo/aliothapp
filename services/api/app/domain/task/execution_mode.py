"""任务执行模式的稳定领域合同。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class TaskExecutionMode(StrEnum):
    """任务在页面探索结束后的收口方式。"""

    FULL_INSPECTION = "full_inspection"
    EXPLORATION_ONLY = "exploration_only"


def task_execution_mode_from_config(
    config: Mapping[str, object] | None,
) -> TaskExecutionMode:
    """从锁定任务配置读取执行模式；兼容未声明字段的既有任务。"""

    value = (config or {}).get("execution_mode")
    if value is None:
        return TaskExecutionMode.FULL_INSPECTION
    try:
        return TaskExecutionMode(str(value))
    except ValueError as exc:
        raise ValueError(f"execution_mode 不受支持: {value}") from exc


def lock_task_execution_mode(
    config: Mapping[str, object],
    mode: TaskExecutionMode,
) -> dict[str, object]:
    """生成包含稳定执行模式的新任务配置快照。"""

    if not isinstance(mode, TaskExecutionMode):
        raise TypeError("mode 必须为 TaskExecutionMode")
    locked = dict(config)
    locked["execution_mode"] = mode.value
    return locked
