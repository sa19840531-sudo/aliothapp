"""App 探索阶段计数纯函数：从巨型探索模块拆出的稳定边界。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.domain.interaction import InteractionOperation


def next_popup_phase_counters(
    *,
    prior_metadata: Mapping[str, object],
    prior_overlay_signature: str | None,
    next_overlay_signature: str | None,
    action_succeeded: bool,
    prior_obstruction_identity: str | None = None,
    next_obstruction_identity: str | None = None,
    action_attempted: bool | None = None,
) -> dict[str, int]:
    """按弹窗结构变化累计真实关闭层数和同层空点次数。"""

    def counter(name: str) -> int:
        value = prior_metadata.get(f"surface_phase_{name}")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    closed = counter("closed_popup_count")
    reobserved = counter("reobserve_count")
    same_attempts = counter("same_popup_attempt_count")
    if action_attempted is None:
        action_attempted = action_succeeded
    if not action_attempted:
        return {
            "surface_phase_closed_popup_count": closed,
            "surface_phase_reobserve_count": reobserved,
            "surface_phase_same_popup_attempt_count": same_attempts,
        }
    same_obstruction = (
        prior_obstruction_identity == next_obstruction_identity
        if prior_obstruction_identity is not None
        else bool(
            prior_overlay_signature
            and prior_overlay_signature == next_overlay_signature
        )
    )
    if same_obstruction:
        reobserved += 1
        same_attempts += 1
    else:
        if prior_obstruction_identity is not None or prior_overlay_signature:
            closed += 1
        # reobserve_count 约束的是同一弹窗层等待关闭控件的连续次数。
        # 弹窗结构已经变化说明上一层取得了真实进展，新一层必须重新计数；
        # 否则多个带倒计时的广告会把历史等待次数累加成假性无进展。
        reobserved = 0
        same_attempts = 0
    return {
        "surface_phase_closed_popup_count": closed,
        "surface_phase_reobserve_count": reobserved,
        "surface_phase_same_popup_attempt_count": same_attempts,
    }


def next_startup_ad_phase_counters(
    *,
    prior_metadata: Mapping[str, object],
    action_type: InteractionOperation,
    action_succeeded: bool,
    action_attempted: bool | None = None,
) -> dict[str, int]:
    """把成功的启动广告点击或系统返回记入可恢复的阶段上下文。"""

    value = prior_metadata.get("surface_phase_close_attempt_count")
    close_attempt_count = (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    )
    if action_attempted is None:
        action_attempted = action_succeeded
    if action_attempted and action_type in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.BACK,
    }:
        close_attempt_count += 1
    return {"surface_phase_close_attempt_count": close_attempt_count}


def next_bounded_dismiss_counters(
    *,
    prior_metadata: Mapping[str, object],
    phase: str | None,
    step: int | None,
) -> dict[str, int]:
    """限步清障工具写回 splash／popup 已用步数，供下一帧 Fail-safe。"""

    def counter(name: str) -> int:
        value = prior_metadata.get(name)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )

    splash = counter("bounded_dismiss_splash_steps")
    popup = counter("bounded_dismiss_popup_steps")
    if phase == "splash" and isinstance(step, int) and not isinstance(step, bool):
        splash = max(splash, step)
    elif phase == "popup" and isinstance(step, int) and not isinstance(step, bool):
        popup = max(popup, step)
    return {
        "bounded_dismiss_splash_steps": splash,
        "bounded_dismiss_popup_steps": popup,
    }


def next_surface_phase_observation_counters(
    *,
    phase_key: str | None,
    prior_metadata: Mapping[str, object],
) -> dict[str, int]:
    """重新观察也必须推进阶段计数，避免无触控弹层无限重采。"""

    def counter(name: str) -> int:
        value = prior_metadata.get(f"surface_phase_{name}")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    if phase_key == "startup_ad_countdown":
        return {
            "surface_phase_countdown_observation_count": (
                counter("countdown_observation_count") + 1
            ),
            "surface_phase_close_attempt_count": counter("close_attempt_count"),
        }
    if phase_key == "popup_chain":
        return {
            "surface_phase_closed_popup_count": counter("closed_popup_count"),
            "surface_phase_reobserve_count": counter("reobserve_count") + 1,
            "surface_phase_same_popup_attempt_count": counter("same_popup_attempt_count"),
        }
    if phase_key == "auth_gate":
        return {
            "surface_phase_dismiss_attempt_count": counter("dismiss_attempt_count"),
            "surface_phase_reobserve_count": counter("reobserve_count") + 1,
        }
    if phase_key == "system_dialog":
        return {
            "surface_phase_handled_dialog_count": counter("handled_dialog_count"),
            "surface_phase_reobserve_count": counter("reobserve_count") + 1,
        }
    return {}
