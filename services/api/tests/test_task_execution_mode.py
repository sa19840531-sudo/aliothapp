"""任务执行模式领域合同测试。"""

import pytest

from services.api.app.domain.task import (
    TaskExecutionMode,
    lock_task_execution_mode,
    task_execution_mode_from_config,
)


def test_missing_execution_mode_is_backward_compatible_full_inspection() -> None:
    """既有任务未锁定新字段时仍按完整质检执行。"""

    assert task_execution_mode_from_config({}) is TaskExecutionMode.FULL_INSPECTION


def test_unknown_execution_mode_fails_closed() -> None:
    """未知模式不能静默回退到完整质检。"""

    with pytest.raises(ValueError, match="execution_mode"):
        task_execution_mode_from_config({"execution_mode": "future"})


def test_lock_execution_mode_does_not_mutate_source() -> None:
    """锁定任务模式必须生成新快照，不能修改调用方字典。"""

    source = {"workflow_version": "v2"}

    locked = lock_task_execution_mode(source, TaskExecutionMode.EXPLORATION_ONLY)

    assert source == {"workflow_version": "v2"}
    assert locked == {
        "workflow_version": "v2",
        "execution_mode": "exploration_only",
    }


def test_lock_execution_mode_requires_enum() -> None:
    """写入端只接受领域枚举，阻止任意字符串污染持久化快照。"""

    with pytest.raises(TypeError, match="TaskExecutionMode"):
        lock_task_execution_mode({}, "exploration_only")  # type: ignore[arg-type]
