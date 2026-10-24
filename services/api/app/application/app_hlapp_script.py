"""HLAPP 產品編排：直接沿用 hlapp.py 的順序、落點、下滑與等待。

通用 Agent 只負責啟動 App、把每一步前後截圖記入證據並做驗證。
這裡不讀通用候選框來改 hlapp 點位，關層被判無效也不改去別的控件。
來源腳本：AirtestIDE untitled.air/hlapp.air/hlapp.py。
App 已由質檢啟動，故不在此安裝或 force-stop。
年齡門只有畫面上看得到才點。AI 短劇標題與片庫影片仍用腳本那條
字樣規則，從當前截圖候選裡取落點；沒有字樣就只下滑，不猜點。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

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
    SurfaceKind,
)

HLAPP_PACKAGE = "top.qkjzg.zfiufm"
HLAPP_SCRIPT_INDEX_KEY = "hlapp_script_index"
HLAPP_AGE_GATE_DONE_KEY = "hlapp_age_gate_done"
HLAPP_AI_SWIPE_KEY = "hlapp_ai_swipe_count"
HLAPP_SCRIPT_REASON_CODE = "hlapp_script_completed"
HLAPP_SCRIPT_BLOCKED_CODE = "hlapp_script_blocked"

_SCREEN_W = 1440
_SCREEN_H = 3040
# 與 hlapp.py 主流程同序，但每個列表只下滑一次，行程比原本 0.75→0.35 更長。
# AI 短劇不再翻找標題：下滑一次、截圖，接著精選。
# 找不到片庫標題時，只略過「進片再按返回」，下一個必走頁仍要點。
_SWIPE_FROM_Y = 0.84
_SWIPE_TO_Y = 0.16
_SWIPE_MS = 600
HLAPP_REQUIRED_FLOW = (
    "today",
    "big",
    "hot",
    "ai-menu",
    "featured",
    "library",
    "live",
    "ai-tech",
    "circle",
    "mine",
)
_LIBRARY_SKIP = (
    "首页",
    "首頁",
    "精选",
    "精選",
    "片库",
    "片庫",
    "直播",
    "圈子",
    "我的",
    "搜索",
    "搜尋",
    "推荐",
    "推薦",
)
_AGE_GATE_MARKERS = (
    "我年满18岁-进入",
    "我年满18岁",
    "我已满18岁-进入",
    "我已满18岁",
    "未满18岁禁止进入",
)
# 开屏／线路失败后的卫星锅空态；产品脚本必须先点重试，不能继续点跳过／圆 X。
_LOAD_FAIL_MARKERS = (
    "加载失败，点击重试",
    "加载失败点击重试",
    "加载失败",
    "点击重试",
    "重试重新加载",
    "重新加载",
)


def _norm_px(x: int, y: int) -> tuple[float, float]:
    return x / _SCREEN_W, y / _SCREEN_H


@dataclass(frozen=True)
class HlappScriptStep:
    tag: str
    action: InteractionOperation
    semantic: str
    purpose: str
    settle_ms: int
    kind: str = "fixed"
    x: float | None = None
    y: float | None = None


def _tap(
    tag: str,
    semantic: str,
    purpose: str,
    x: float,
    y: float,
    *,
    settle_ms: int,
) -> HlappScriptStep:
    return HlappScriptStep(
        tag=tag,
        action=InteractionOperation.CLICK,
        semantic=semantic,
        purpose=purpose,
        settle_ms=settle_ms,
        x=x,
        y=y,
    )


def _shot(tag: str, semantic: str, *, settle_ms: int) -> HlappScriptStep:
    return HlappScriptStep(
        tag=tag,
        action=InteractionOperation.OBSERVE,
        semantic=semantic,
        purpose=f"按 hlapp.py 留證截圖 {tag}；本步不選點。",
        settle_ms=settle_ms,
    )


def _swipe(tag: str, semantic: str) -> HlappScriptStep:
    return HlappScriptStep(
        tag=tag,
        action=InteractionOperation.SCROLL,
        semantic=semantic,
        purpose=(
            "手指從 (0.50, "
            f"{_SWIPE_FROM_Y:.2f}) 滑到 (0.50, {_SWIPE_TO_Y:.2f})，"
            f"{_SWIPE_MS}ms。本步前的截圖即下滑前留證。"
        ),
        settle_ms=800,
    )


def _locate(tag: str, semantic: str, purpose: str) -> HlappScriptStep:
    return HlappScriptStep(
        tag=tag,
        action=InteractionOperation.CLICK,
        semantic=semantic,
        purpose=purpose,
        settle_ms=1_000,
        kind="locate",
    )


def hlapp_script_steps() -> tuple[HlappScriptStep, ...]:
    """與 hlapp.py 主流程同序。截圖由 Agent 在每步前後收下。"""

    steps: list[HlappScriptStep] = [
        # 开屏只留两张证据：本步 OBSERVE=开屏本体；下一步 skip 的 after=跳过后。
        _shot("01", "开屏本体", settle_ms=2_000),
        _tap(
            "skip",
            "跳过",
            "按 hlapp.py 点开屏右上「跳过」(0.877, 0.082)。",
            0.877,
            0.082,
            settle_ms=500,
        ),
    ]
    for tag, x, y in (
        ("x03", 0.499, 0.701),
        ("x04", 0.497, 0.702),
        ("x05", 0.501, 0.700),
        ("x06", 0.501, 0.702),
        ("x07", 0.499, 0.698),
        ("x08", 0.499, 0.786),
        ("x09", 0.499, 0.270),
        ("x10", 0.501, 0.734),
    ):
        steps.append(
            _tap(
                tag,
                "关闭弹窗",
                f"按 hlapp.py 点圆形 X {tag[1:]} ({x:.3f}, {y:.3f})。",
                x,
                y,
                settle_ms=500,
            )
        )
    featured_x, featured_y = _norm_px(310, 2768)
    library_x, library_y = _norm_px(518, 2767)
    live_x, live_y = _norm_px(716, 2768)
    first_live_x, first_live_y = _norm_px(382, 1354)
    back_video_x, back_video_y = _norm_px(71, 211)
    back_live_x, back_live_y = _norm_px(99, 246)
    ai_tech_x, ai_tech_y = _norm_px(922, 2776)
    circle_x, circle_y = _norm_px(1133, 2771)
    mine_x, mine_y = _norm_px(1336, 2774)
    steps.extend(
        (
            _tap(
                "today",
                "今日看料",
                "按 hlapp.py 点上方栏「今日看料」(0.261, 0.128)。",
                0.261,
                0.128,
                settle_ms=500,
            ),
            _swipe("12", "今日看料下滑"),
            _tap(
                "big",
                "黑料大事",
                "按 hlapp.py 点上方栏「黑料大事」(0.443, 0.129)。",
                0.443,
                0.129,
                settle_ms=500,
            ),
            _swipe("15", "黑料大事下滑"),
            _tap(
                "hot",
                "热门吃瓜",
                "按 hlapp.py 点上方栏「热门吃瓜」(0.609, 0.132)，再等列表出来。",
                0.609,
                0.132,
                settle_ms=12_000,
            ),
            _swipe("18", "热门吃瓜下滑"),
            _tap(
                "menu",
                "右上角菜单",
                "按 hlapp.py 点右上角「三」(0.962, 0.130)。",
                0.962,
                0.130,
                settle_ms=500,
            ),
            _tap(
                "ai-menu",
                "AI短剧",
                "按 hlapp.py 点菜单「AI短剧」(0.790, 0.337)。",
                0.790,
                0.337,
                settle_ms=500,
            ),
            _swipe("21", "AI短剧下滑"),
            _shot("22", "AI短剧下滑后", settle_ms=500),
            _tap(
                "featured",
                "精选",
                "按 hlapp.py 点底栏「精选」。",
                featured_x,
                featured_y,
                settle_ms=500,
            ),
            _swipe("30", "精选下滑"),
            _tap(
                "library",
                "片库",
                "按 hlapp.py 点底栏「片库」，再等列表出来。",
                library_x,
                library_y,
                settle_ms=12_000,
            ),
            _swipe("32", "进片库后下滑"),
            _locate(
                "library-open",
                "片库影片",
                "按 hlapp.py 点片库列表最上面一部片；没有标题就不猜点。",
            ),
            _tap(
                "video-back",
                "影片返回",
                "进入影片并留证后，按 hlapp.py 点左上「<」。不用系统 BACK。",
                back_video_x,
                back_video_y,
                settle_ms=500,
            ),
            _tap(
                "live",
                "直播",
                "按 hlapp.py 点底栏「直播」。",
                live_x,
                live_y,
                settle_ms=1_000,
            ),
            _tap(
                "first-live",
                "第一个直播",
                "按 hlapp.py 点直播列表左上第一张卡片，不点上方广告。",
                first_live_x,
                first_live_y,
                settle_ms=1_000,
            ),
            _tap(
                "live-back",
                "直播返回",
                "进入直播间并留证后，按 hlapp.py 点左上「<」。不用系统 BACK。",
                back_live_x,
                back_live_y,
                settle_ms=500,
            ),
            _tap(
                "ai-tech",
                "AI科技",
                "按 hlapp.py 点底栏「AI科技」。",
                ai_tech_x,
                ai_tech_y,
                settle_ms=1_000,
            ),
            _tap(
                "circle",
                "圈子",
                "按 hlapp.py 点底栏「圈子」。",
                circle_x,
                circle_y,
                settle_ms=1_000,
            ),
            _swipe("42", "圈子下滑"),
            _tap(
                "mine",
                "我的",
                "按 hlapp.py 点底栏「我的」。点到后即收口。",
                mine_x,
                mine_y,
                settle_ms=1_000,
            ),
        )
    )
    return tuple(steps)


def _metadata(work: ExplorationWork) -> Mapping[str, object]:
    metadata = work.frontier.observation.metadata
    return metadata if isinstance(metadata, Mapping) else {}


def _is_hlapp_work(work: ExplorationWork) -> bool:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    policy = _metadata(work).get("app_runtime_policy")
    if isinstance(policy, Mapping) and policy.get("exploration_mode") == "dynamic_frontier":
        # 动态规则探索不能因产品包名被固定坐标流程覆盖，显式课纲流程保持既有行为。
        return False
    from services.api.app.application.app_ui_tree_features import (
        resolve_product_orchestrator_id,
    )

    package_name = str(_metadata(work).get("package_name") or "").strip()
    if resolve_product_orchestrator_id(package_name=package_name) == "hlapp":
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(
        resolve_product_orchestrator_id(package_name=str(item)) == "hlapp" for item in allowed
    )


def _expected_package(work: ExplorationWork) -> str:
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if package_name == HLAPP_PACKAGE:
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    for item in allowed:
        if str(item).strip() == HLAPP_PACKAGE:
            return HLAPP_PACKAGE
    return package_name or HLAPP_PACKAGE


def _script_index(metadata: Mapping[str, object]) -> int:
    raw = metadata.get(HLAPP_SCRIPT_INDEX_KEY)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _ai_swipes(metadata: Mapping[str, object]) -> int:
    raw = metadata.get(HLAPP_AI_SWIPE_KEY)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _age_gate_visible(metadata: Mapping[str, object]) -> bool:
    parts = [
        str(metadata.get("visual_summary") or ""),
        str(metadata.get("ocr_text") or ""),
        str(metadata.get("page_type") or ""),
    ]
    elements = metadata.get("key_elements")
    if isinstance(elements, (list, tuple)):
        parts.extend(str(item) for item in elements)
    blob = " ".join(parts)
    return any(marker in blob for marker in _AGE_GATE_MARKERS)


def _load_fail_visible(metadata: Mapping[str, object]) -> bool:
    """卫星锅「加载失败，点击重试」空态；整页可点，优先文案区中部。"""

    parts = [
        str(metadata.get("visual_summary") or ""),
        str(metadata.get("ocr_text") or ""),
        str(metadata.get("page_type") or ""),
        str(metadata.get("page_state") or ""),
    ]
    elements = metadata.get("key_elements")
    if isinstance(elements, (list, tuple)):
        parts.extend(str(item) for item in elements)
    blob = " ".join(parts)
    return any(marker in blob for marker in _LOAD_FAIL_MARKERS)


def _bounds(x: float, y: float) -> dict[str, float]:
    half = 0.04
    return {
        "left": round(max(0.0, x - half), 4),
        "top": round(max(0.0, y - half), 4),
        "right": round(min(1.0, x + half), 4),
        "bottom": round(min(1.0, y + half), 4),
    }


def _intent_for_step(
    *,
    work: ExplorationWork,
    step: HlappScriptStep,
    index_token: str,
) -> ActionIntent:
    capture_id = str(work.source_capture_id or "unknown")
    candidate_id = f"hlapp-script:{index_token}:{step.tag}"
    digest = hashlib.sha256(
        json.dumps(
            [work.work_id, capture_id, candidate_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate_id,
        "hlapp_script": True,
        "hlapp_script_tag": step.tag,
        "hlapp_orchestrated": True,
        "hlapp_expected_package": _expected_package(work),
        "hlapp_settle_after_action_ms": step.settle_ms,
        "require_screenshot_evidence": True,
    }
    # 开屏：OBSERVE 只验本体；跳过点击复用前帧视觉，只验 after。
    if step.tag == "01" and step.action is InteractionOperation.OBSERVE:
        parameters["splash_body_evidence"] = True
    if step.tag == "skip":
        parameters["splash_skip_after_only"] = True
    if step.action is InteractionOperation.SCROLL:
        parameters["from"] = {"x": 0.50, "y": _SWIPE_FROM_Y, "space": "normalized_01"}
        parameters["to"] = {"x": 0.50, "y": _SWIPE_TO_Y, "space": "normalized_01"}
        parameters["duration_ms"] = _SWIPE_MS
    elif step.x is not None and step.y is not None:
        parameters["normalized_bounds"] = _bounds(step.x, step.y)
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=step.action,
        semantic_target=step.semantic,
        parameters=parameters,
        purpose=step.purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            step.action,
        ),
        deadline=work.deadline,
    )


def _looks_like_library_title(label: str) -> bool:
    compact = re.sub(r"\s+", "", label or "")
    if len(compact) < 4 or compact in _LIBRARY_SKIP:
        return False
    return re.search(r"[\u4e00-\u9fff]", compact) is not None


def _candidate_box(candidate: CandidateAction) -> tuple[float, float, float, float] | None:
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return None
    try:
        left = float(raw["left"])
        top = float(raw["top"])
        right = float(raw["right"])
        bottom = float(raw["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _top_match(
    work: ExplorationWork,
    *,
    predicate,
    y_min: float,
    y_max: float,
) -> tuple[float, float] | None:
    hits: list[tuple[float, float, float, float]] = []
    for candidate in work.frontier.candidates:
        if not predicate(str(candidate.semantic_target or "")):
            continue
        box = _candidate_box(candidate)
        if box is None:
            continue
        left, top, right, bottom = box
        cy = (top + bottom) / 2.0
        if cy < y_min or cy > y_max:
            continue
        hits.append((top, left, (left + right) / 2.0, (top + bottom) / 2.0))
    if not hits:
        return None
    hits.sort()
    return hits[0][2], hits[0][3]


def _blocked(work: ExplorationWork, reason: str) -> ExplorationTerminal:
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason=reason,
        outcome="exploration_bounded",
        reason_code=HLAPP_SCRIPT_BLOCKED_CODE,
        coverage_summary={"hlapp_script_blocked": True},
    )


def _completion(work: ExplorationWork) -> ExplorationTerminal:
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason=(
            "HLAPP 录制步骤已全部走完。最后一步是底栏「我的」，不是中途停。"
        ),
        outcome="exploration_completed",
        reason_code=HLAPP_SCRIPT_REASON_CODE,
        coverage_summary={
            "hlapp_script_completed": True,
            "hlapp_script_steps": len(hlapp_script_steps()),
            # 點到「我的」只寫封口標記。規則或質檢工作項近 3 分鐘還在更新時不封，
            # 停更後才按這個手動封口放行。標記寫入後不能再改。
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
    )


def _intent_for_tag(
    work: ExplorationWork,
    steps: tuple[HlappScriptStep, ...],
    tag: str,
) -> ActionIntent:
    index = next(i for i, step in enumerate(steps) if step.tag == tag)
    return _intent_for_step(work=work, index_token=str(index), step=steps[index])


def _next_required_tag(steps: tuple[HlappScriptStep, ...], current_tag: str) -> str:
    """目前這步若進不去，下一個動作必須是尚未走過的必走頁，不能再往後跳。"""

    order = [step.tag for step in steps]
    here = order.index(current_tag)
    passed = [tag for tag in HLAPP_REQUIRED_FLOW if order.index(tag) <= here]
    nxt = HLAPP_REQUIRED_FLOW.index(passed[-1]) + 1
    if nxt >= len(HLAPP_REQUIRED_FLOW):
        raise RuntimeError(f"hlapp required flow has nothing after {current_tag}")
    return HLAPP_REQUIRED_FLOW[nxt]


def hlapp_script_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """HLAPP 的下一步只來自 hlapp.py。非 HLAPP 返回 None，交給通用 Agent。"""

    if not isinstance(work, ExplorationWork) or not _is_hlapp_work(work):
        return None
    metadata = _metadata(work)
    index = _script_index(metadata)
    if (
        index == 0
        and metadata.get(HLAPP_AGE_GATE_DONE_KEY) is not True
        and _age_gate_visible(metadata)
    ):
        return _intent_for_step(
            work=work,
            index_token="age",
            step=_tap(
                "age_gate",
                "我年满18岁-进入",
                "按 hlapp.py 首次年龄门点进入；没有该层则跳过。禁止点未满18。",
                0.478,
                0.706,
                settle_ms=2_000,
            ),
        )
    if _load_fail_visible(metadata):
        # 不推进脚本游标：恢复后仍从当前步继续。
        return _intent_for_step(
            work=work,
            index_token="load_retry",
            step=_tap(
                "load_retry",
                "点击重试",
                "加载失败空态点「点击重试」中部文案区，等页面恢复后再继续脚本。",
                0.50,
                0.58,
                settle_ms=3_000,
            ),
        )
    steps = hlapp_script_steps()
    if index >= len(steps):
        return _completion(work)
    step = steps[index]
    if step.kind == "locate" and step.tag == "library-open":
        point = _top_match(
            work,
            predicate=_looks_like_library_title,
            y_min=0.22,
            y_max=0.82,
        )
        if point is None:
            # 底欄「片庫」已經點過。沒進影片就不能按返回，接著點下一個必走頁「直播」。
            return _intent_for_tag(work, steps, _next_required_tag(steps, step.tag))
        return _intent_for_step(
            work=work,
            index_token=str(index),
            step=_tap(
                "library-open",
                "片库影片",
                "按 hlapp.py 点片库列表最上面一部片。",
                point[0],
                point[1],
                settle_ms=1_000,
            ),
        )
    return _intent_for_step(work=work, step=step, index_token=str(index))


def _action_candidate_id(action: object) -> str:
    raw = getattr(action, "candidate_id", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    metadata = getattr(action, "metadata", None)
    if isinstance(metadata, Mapping):
        nested = metadata.get("candidate_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def hlapp_script_step_ready(
    *,
    candidate_id: str,
    action_succeeded: bool | None,
    observation_completed: bool,
    observation_reason: str,
) -> bool:
    """腳本點擊成功就照 hlapp.py 走下一步，不因彈窗還在而重試同一點。

    已經跳出 App 則停住，避免在瀏覽器裡接著點。
    """

    if observation_completed:
        return True
    if action_succeeded is not True or not candidate_id.startswith("hlapp-script:"):
        return False
    return observation_reason != "after_package_outside_scope"


def apply_hlapp_script_progress(
    result: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: object,
    action_succeeded: bool | None,
) -> None:
    """腳本步成功後才前進。失敗不改計數。"""

    result[HLAPP_SCRIPT_INDEX_KEY] = _script_index(prior)
    result[HLAPP_AGE_GATE_DONE_KEY] = prior.get(HLAPP_AGE_GATE_DONE_KEY) is True
    result[HLAPP_AI_SWIPE_KEY] = _ai_swipes(prior)
    result["hlapp_script_completed"] = prior.get("hlapp_script_completed") is True
    result["hlapp_script_attempted"] = prior.get("hlapp_script_attempted") is True
    if completed_action is None or action_succeeded is False:
        return
    candidate_id = _action_candidate_id(completed_action)
    if not candidate_id.startswith("hlapp-script:"):
        return
    parts = candidate_id.split(":")
    if len(parts) < 3:
        return
    if parts[1] == "age":
        result[HLAPP_AGE_GATE_DONE_KEY] = True
        return
    try:
        finished = int(parts[1])
    except ValueError:
        return
    if finished < _script_index(prior):
        return
    steps = hlapp_script_steps()
    tag = parts[2]
    if finished < len(steps) and steps[finished].tag != tag:
        result[HLAPP_SCRIPT_INDEX_KEY] = finished
        return
    result[HLAPP_SCRIPT_INDEX_KEY] = finished + 1
