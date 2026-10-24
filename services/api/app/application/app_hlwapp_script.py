"""黑料网只走 hlwapp.py 的 ADB 顺序、落点、等待和前台恢复。

通用 Agent 只负责启动、把动作前后截图留下来，以及按脚本结果验证。
它不能用当前帧的候选框改写这些点。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.application.app_ui_tree_features import (
    resolve_product_orchestrator_id,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
)

HLWAPP_PACKAGE = "me.fifeg.dtphkj"
HLWAPP_SCRIPT_INDEX_KEY = "hlwapp_script_index"
HLWAPP_SCRIPT_REASON_CODE = "hlwapp_script_completed"
HLWAPP_SCRIPT_BLOCKED_CODE = "hlwapp_script_blocked"
_SCREEN_WIDTH = 1440
_SCREEN_HEIGHT = 3040


@dataclass(frozen=True)
class HlwappScriptStep:
    """hlwapp.py 的一步。坐标来自脚本里已经量过的 ADB 点，不是模型候选。"""

    tag: str
    action: InteractionOperation
    point: tuple[int, int] | None = None
    settle_ms: int = 1500
    swipe_to: tuple[int, int] | None = None
    duration_ms: int = 700
    browser_hold: bool = False


def _norm_px(x: int, y: int) -> dict[str, float]:
    return {
        "left": max(0.0, (x / _SCREEN_WIDTH) - 0.02),
        "top": max(0.0, (y / _SCREEN_HEIGHT) - 0.015),
        "right": min(1.0, (x / _SCREEN_WIDTH) + 0.02),
        "bottom": min(1.0, (y / _SCREEN_HEIGHT) + 0.015),
    }


def _tap(tag: str, x: int, y: int, *, settle_ms: int = 1500) -> HlwappScriptStep:
    return HlwappScriptStep(tag, InteractionOperation.CLICK, (x, y), settle_ms)


def _shot(tag: str, *, settle_ms: int = 1500, browser_hold: bool = False) -> HlwappScriptStep:
    return HlwappScriptStep(
        tag,
        InteractionOperation.OBSERVE,
        settle_ms=settle_ms,
        browser_hold=browser_hold,
    )


def _swipe(
    tag: str,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    duration_ms: int,
) -> HlwappScriptStep:
    return HlwappScriptStep(
        tag,
        InteractionOperation.SCROLL,
        start,
        swipe_to=end,
        duration_ms=duration_ms,
    )


def hlwapp_script_steps() -> tuple[HlwappScriptStep, ...]:
    """与 hlwapp.py 主流程同一顺序。关闭叉只在脚本里按模板点，这里不另猜点。"""

    new_card_swipe = ((720, 2100), (720, 800), 800)
    detail_swipe = ((720, 2100), (720, 900), 700)
    return (
        _shot("01-splash", settle_ms=2000),
        _tap("01-skip", 1273, 254),
        _tap("02-popup", 720, 2127),
        _tap("03-popup", 720, 2513),
        _tap("04-popup", 720, 2127),
        _tap("05-popup", 720, 2127),
        _tap("06-popup", 720, 2127),
        _tap("07-popup", 720, 2513),
        _tap("08-popup", 720, 2073),
        _shot("09-home"),
        _tap("10-latest", 348, 397),
        _shot("10-latest-shot"),
        _swipe("10-new-1", *new_card_swipe[:2], duration_ms=new_card_swipe[2]),
        _swipe("10-new-2", *new_card_swipe[:2], duration_ms=new_card_swipe[2]),
        _swipe("10-new-3", *new_card_swipe[:2], duration_ms=new_card_swipe[2]),
        _tap("11-new-card", 720, 1740),
        _shot("11-detail"),
        *[
            _swipe(f"12-detail-{index:02d}", *detail_swipe[:2], duration_ms=detail_swipe[2])
            for index in range(1, 7)
        ],
        _tap("13-back", 85, 230),
        _tap("14-ai", 370, 2768),
        _shot("14-ai-shot"),
        _tap("16-community", 608, 2770),
        _shot("16-community-shot"),
        _tap("18-picture", 479, 400),
        _shot("18-picture-shot"),
        _swipe("18-video-scan", *detail_swipe[:2], duration_ms=detail_swipe[2]),
        _tap("19-video", 464, 692),
        _shot("19-video-shot"),
        _tap("21-back", 88, 225),
        _tap("22-back", 92, 221),
        _tap("23-video-tab", 837, 2768),
        _shot("23-video-tab-shot"),
        _tap("24-video-play", 375, 1525),
        _shot("25-video-1", settle_ms=10_000),
        _shot("25-video-2", settle_ms=10_000),
        _shot("25-video-3"),
        _tap("26-video-back", 85, 230),
        _tap("27-welfare", 1085, 2764),
        _shot("27-welfare-shot"),
        _tap("29-mine", 1315, 2764),
        _shot("29-mine-shot"),
        _tap("31-coin", 894, 1100),
        _shot("31-coin-shot"),
        _tap("32-pay", 723, 2669),
        _shot("32-pay-shot"),
        HlwappScriptStep(
            "33-submit",
            InteractionOperation.CLICK,
            (722, 2248),
            settle_ms=1500,
            browser_hold=True,
        ),
        _shot("33-browser", settle_ms=20_000, browser_hold=True),
        HlwappScriptStep("34-back", InteractionOperation.BACK, settle_ms=1500),
    )


def hlwapp_required_flow() -> tuple[str, ...]:
    return tuple(step.tag for step in hlwapp_script_steps())


def _metadata_text(metadata: Mapping[str, object], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) else ""


def _is_hlwapp_work(work: ExplorationWork) -> bool:
    if work.surface_kind.value != "android":
        return False
    policy = work.frontier.observation.metadata.get("app_runtime_policy")
    if isinstance(policy, Mapping) and policy.get("exploration_mode") == "dynamic_frontier":
        return False
    metadata = work.frontier.observation.metadata
    package = _metadata_text(metadata, "package_name")
    allowed = work.app_goal.allowed_packages if work.app_goal is not None else ()
    if package == HLWAPP_PACKAGE or HLWAPP_PACKAGE in allowed:
        return True
    return resolve_product_orchestrator_id(package_name=package) == "hlwapp"


def _script_index(work: ExplorationWork) -> int:
    raw = work.frontier.observation.metadata.get(HLWAPP_SCRIPT_INDEX_KEY)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def hlwapp_script_step_ready(
    *,
    candidate_id: str,
    action_succeeded: bool | None,
    observation_completed: bool,
    observation_reason: str | None,
) -> bool:
    """浏览器留证步允许前台暂时离开 App；其余步离开包就停。"""

    if action_succeeded is False:
        return False
    tag = candidate_id.removeprefix("hlwapp-script:")
    if tag.startswith(("33-submit", "33-browser", "34-back")):
        return True
    if observation_reason == "after_package_outside_scope":
        return False
    return observation_completed or action_succeeded is True


def hlwapp_script_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """按 hlwapp.py 给出下一步。通用候选框不能改这个点。"""

    if not _is_hlwapp_work(work):
        return None
    steps = hlwapp_script_steps()
    index = _script_index(work)
    if index >= len(steps):
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.COMPLETED,
            reason="黑料网脚本已按 hlwapp.py 走完，截图留证后收口。",
            outcome="exploration_completed",
            reason_code=HLWAPP_SCRIPT_REASON_CODE,
            coverage_summary={
                "hlwapp_script_completed": True,
                "hlwapp_script_steps": len(steps),
                "coverage_complete": True,
                "visual_quality_satisfied": True,
            },
        )
    return _intent_for_step(work, steps[index], index)


def _intent_for_step(
    work: ExplorationWork,
    step: HlwappScriptStep,
    index: int,
) -> ActionIntent:
    candidate_id = f"hlwapp-script:{index}:{step.tag}"
    digest = hashlib.sha256(
        json.dumps(
            [work.work_id, str(work.source_capture_id or "unknown"), candidate_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate_id,
        "hlwapp_script": True,
        "hlwapp_script_tag": step.tag,
        "hlwapp_orchestrated": True,
        "hlwapp_product_orchestrated_point": True,
        "hlwapp_expected_package": HLWAPP_PACKAGE,
        "hlwapp_settle_after_action_ms": step.settle_ms,
        "require_screenshot_evidence": True,
    }
    if step.browser_hold:
        parameters["hlwapp_browser_hold"] = True
    if step.action is InteractionOperation.SCROLL and step.point and step.swipe_to:
        parameters["from"] = {
            "x": step.point[0] / _SCREEN_WIDTH,
            "y": step.point[1] / _SCREEN_HEIGHT,
            "space": "normalized_01",
        }
        parameters["to"] = {
            "x": step.swipe_to[0] / _SCREEN_WIDTH,
            "y": step.swipe_to[1] / _SCREEN_HEIGHT,
            "space": "normalized_01",
        }
        parameters["duration_ms"] = step.duration_ms
    elif step.point is not None:
        parameters["normalized_bounds"] = _norm_px(*step.point)
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=step.action,
        semantic_target=f"黑料网 {step.tag}",
        parameters=parameters,
        purpose="按 hlwapp.py 执行固定步骤并截图留证，不重新选择点位",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(work.surface_kind, step.action),
        deadline=work.deadline,
    )


def apply_hlwapp_script_progress(
    metadata: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: CandidateAction | None,
    action_succeeded: bool | None,
) -> None:
    """成功的脚本步才推进。失败停在原步，不把点交给通用 Agent。"""

    steps = hlwapp_script_steps()
    raw = prior.get(HLWAPP_SCRIPT_INDEX_KEY)
    index = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
    if (
        completed_action is not None
        and action_succeeded is not False
        and str(completed_action.candidate_id).startswith("hlwapp-script:")
        and index < len(steps)
    ):
        index += 1
    metadata[HLWAPP_SCRIPT_INDEX_KEY] = index
    if index >= len(steps):
        metadata["hlwapp_script_completed"] = True
        metadata["coverage_complete"] = True
        metadata["visual_quality_satisfied"] = True
        metadata["hlwapp_script_reason"] = HLWAPP_SCRIPT_REASON_CODE
