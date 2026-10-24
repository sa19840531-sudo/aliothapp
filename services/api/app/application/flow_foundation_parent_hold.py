"""流程基建：父待办 hold 门禁（无真机可测）。

同一父探索待办最多被一个活动短流程阻断；claim 前须通过本门禁。
"""

from __future__ import annotations

from typing import Any, Protocol

from services.api.app.domain.exploration.flow_foundation import FlowFoundationContractError
from services.api.app.domain.exploration.flow_foundation_control import (
    assert_single_active_parent_hold,
)
from services.api.app.domain.protocol import require_text


class ParentHoldGateError(RuntimeError):
    """父待办已被流程 hold，禁止继续 claim／推进主探索。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "PARENT_WORK_HELD_BY_FLOW",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class _ControlStoreLike(Protocol):
    def get_active_hold_for_parent(
        self, *, parent_work_id: str
    ) -> Any | None: ...


class _ControlServiceLike(Protocol):
    @property
    def store(self) -> _ControlStoreLike: ...


class _RuntimeLike(Protocol):
    control: _ControlServiceLike


def assert_parent_work_not_held(
    *,
    parent_work_id: str,
    active_hold_id: str | None,
) -> None:
    """包装领域 assert_single_active_parent_hold，供 claim 门禁使用。

    已有活动 hold 时一律拒绝新 claimant（用占位 claimant_id 触发冲突语义）。
    """

    require_text(parent_work_id, "parent_work_id")
    if active_hold_id is None:
        return
    try:
        assert_single_active_parent_hold(
            existing_active_hold_id=str(active_hold_id),
            claimant_hold_id=f"claimant:gate:{parent_work_id}",
        )
    except FlowFoundationContractError as exc:
        raise ParentHoldGateError(
            f"父待办已被流程 hold，禁止 claim：{parent_work_id}",
            error_code="PARENT_WORK_HELD_BY_FLOW",
        ) from exc


class ParentHoldGate:
    """基于 FlowFoundationControlService／Runtime 的父待办 claim 门禁。"""

    def __init__(
        self,
        control_or_runtime: _ControlServiceLike | _RuntimeLike | None,
    ) -> None:
        self._control = _resolve_control(control_or_runtime)

    async def ensure_claimable(self, parent_work_id: str) -> None:
        """若控制仓对该父待办有活动 hold，抛出 PARENT_WORK_HELD_BY_FLOW。"""

        require_text(parent_work_id, "parent_work_id")
        if self._control is None:
            return
        get_hold = getattr(self._control, "get_active_hold_for_parent", None)
        if callable(get_hold):
            hold = get_hold(parent_work_id=parent_work_id)
        else:
            store = getattr(self._control, "store", None) or getattr(
                self._control, "_store", None
            )
            if store is None:
                return
            hold = store.get_active_hold_for_parent(parent_work_id=parent_work_id)
        if hold is None:
            return
        hold_id = str(getattr(hold, "hold_id", None) or hold)
        assert_parent_work_not_held(
            parent_work_id=parent_work_id,
            active_hold_id=hold_id,
        )


def _resolve_control(
    control_or_runtime: _ControlServiceLike | _RuntimeLike | None,
) -> _ControlServiceLike | None:
    if control_or_runtime is None:
        return None
    control = getattr(control_or_runtime, "control", None)
    if control is not None:
        return control  # type: ignore[return-value]
    return control_or_runtime  # type: ignore[return-value]
