"""有界 InteractionPlan：仅 scroll → 新 Capture／重绑 → click。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

APP_SCROLL_REBIND_CLICK_PLAN_VERSION = "app-interaction-plan.scroll-rebind-click.v1"
_MAX_SCROLL_STEPS = 3

PlanStatus = Literal[
    "planned",
    "scroll_pending",
    "rebind_pending",
    "click_pending",
    "completed",
    "failed_rebind",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class AppScrollRebindClickPlan:
    """平台侧最小合成计划；不是通用复合脚本引擎。"""

    click_candidate_id: str
    scroll_steps: int = 1
    direction: Literal["down", "up"] = "down"
    status: PlanStatus = "planned"
    current_step: int = 0
    source_capture_id: str | None = None
    after_scroll_capture_id: str | None = None
    failure_reason: str | None = None
    protocol_version: str = APP_SCROLL_REBIND_CLICK_PLAN_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.click_candidate_id, str) or not self.click_candidate_id.strip():
            raise ValueError("click_candidate_id 不能为空")
        if self.scroll_steps < 1 or self.scroll_steps > _MAX_SCROLL_STEPS:
            raise ValueError(f"scroll_steps 必须在 1..{_MAX_SCROLL_STEPS}")
        if self.protocol_version != APP_SCROLL_REBIND_CLICK_PLAN_VERSION:
            raise ValueError("InteractionPlan 协议版本不受支持")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "click_candidate_id": self.click_candidate_id,
            "scroll_steps": self.scroll_steps,
            "direction": self.direction,
            "status": self.status,
            "current_step": self.current_step,
            "source_capture_id": self.source_capture_id,
            "after_scroll_capture_id": self.after_scroll_capture_id,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, raw: object) -> AppScrollRebindClickPlan:
        if not isinstance(raw, dict):
            raise TypeError("InteractionPlan 必须为对象")
        return cls(
            click_candidate_id=str(raw.get("click_candidate_id") or ""),
            scroll_steps=int(raw.get("scroll_steps") or 1),
            direction="up" if raw.get("direction") == "up" else "down",
            status=str(raw.get("status") or "planned"),  # type: ignore[arg-type]
            current_step=int(raw.get("current_step") or 0),
            source_capture_id=(
                str(raw["source_capture_id"])
                if isinstance(raw.get("source_capture_id"), str)
                else None
            ),
            after_scroll_capture_id=(
                str(raw["after_scroll_capture_id"])
                if isinstance(raw.get("after_scroll_capture_id"), str)
                else None
            ),
            failure_reason=(
                str(raw["failure_reason"])
                if isinstance(raw.get("failure_reason"), str)
                else None
            ),
            protocol_version=str(
                raw.get("protocol_version") or APP_SCROLL_REBIND_CLICK_PLAN_VERSION
            ),
        )


def build_scroll_rebind_click_plan(
    *,
    click_candidate_id: str,
    source_capture_id: str | None = None,
    scroll_steps: int = 1,
    direction: Literal["down", "up"] = "down",
) -> AppScrollRebindClickPlan:
    """创建有界计划；执行方必须先 scroll 再强制新观察后 click。"""
    return AppScrollRebindClickPlan(
        click_candidate_id=click_candidate_id.strip(),
        scroll_steps=scroll_steps,
        direction=direction,
        status="scroll_pending",
        source_capture_id=source_capture_id,
    )


def mark_plan_after_scroll(
    plan: AppScrollRebindClickPlan,
    *,
    after_scroll_capture_id: str,
) -> AppScrollRebindClickPlan:
    """滚动完成：进入重绑等待；click 不得再用滚动前 Capture。"""
    if not after_scroll_capture_id.strip():
        raise ValueError("after_scroll_capture_id 不能为空")
    step = min(plan.current_step + 1, plan.scroll_steps)
    status: PlanStatus = "rebind_pending" if step >= plan.scroll_steps else "scroll_pending"
    return AppScrollRebindClickPlan(
        click_candidate_id=plan.click_candidate_id,
        scroll_steps=plan.scroll_steps,
        direction=plan.direction,
        status=status,
        current_step=step,
        source_capture_id=plan.source_capture_id,
        after_scroll_capture_id=after_scroll_capture_id.strip(),
        failure_reason=None,
    )


def mark_plan_rebind_failed(
    plan: AppScrollRebindClickPlan,
    *,
    reason: str = "POST_SCROLL_REBIND_REQUIRED",
) -> AppScrollRebindClickPlan:
    """重绑失败：禁止盲点旧框，计划失败待重规划。"""
    return AppScrollRebindClickPlan(
        click_candidate_id=plan.click_candidate_id,
        scroll_steps=plan.scroll_steps,
        direction=plan.direction,
        status="failed_rebind",
        current_step=plan.current_step,
        source_capture_id=plan.source_capture_id,
        after_scroll_capture_id=plan.after_scroll_capture_id,
        failure_reason=reason,
    )


def mark_plan_click_ready(plan: AppScrollRebindClickPlan) -> AppScrollRebindClickPlan:
    if plan.status not in {"rebind_pending", "click_pending"}:
        raise ValueError("只有重绑成功后的计划可以进入 click")
    if not plan.after_scroll_capture_id:
        raise ValueError("缺少滚动后 Capture，不能 click")
    return AppScrollRebindClickPlan(
        click_candidate_id=plan.click_candidate_id,
        scroll_steps=plan.scroll_steps,
        direction=plan.direction,
        status="click_pending",
        current_step=plan.current_step,
        source_capture_id=plan.source_capture_id,
        after_scroll_capture_id=plan.after_scroll_capture_id,
        failure_reason=None,
    )


def mark_plan_completed(plan: AppScrollRebindClickPlan) -> AppScrollRebindClickPlan:
    return AppScrollRebindClickPlan(
        click_candidate_id=plan.click_candidate_id,
        scroll_steps=plan.scroll_steps,
        direction=plan.direction,
        status="completed",
        current_step=plan.current_step,
        source_capture_id=plan.source_capture_id,
        after_scroll_capture_id=plan.after_scroll_capture_id,
        failure_reason=None,
    )


def plan_requires_fresh_capture_for_click(plan: AppScrollRebindClickPlan) -> bool:
    """click 意图的 source 必须是滚动后 Capture。"""
    return plan.status in {"rebind_pending", "click_pending", "failed_rebind"}
