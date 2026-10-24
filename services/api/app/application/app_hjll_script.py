"""HJLL 產品編排：直接沿用 hjll.py 的順序、落點與等待。

通用 Agent 只負責啟動 App、把每一步前後截圖記入證據並做驗證。
這裡不讀視覺框、不改點、關層被判無效也不提前收口。
來源腳本：AirtestIDE untitled.air/hjll.air/hjll.py。
App 已由質檢啟動，故不在此 force-stop。上方欄不在每一幀重數字形，
國產點中後欄位會左滑，動漫、漫畫用滑動後的實測點。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import (
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

HJLL_SCRIPT_INDEX_KEY = "hjll_script_index"
HJLL_AGE_GATE_DONE_KEY = "hjll_age_gate_done"
HJLL_VIP_CARD_EXTRA_BACK_INDEX_KEY = "hjll_vip_card_extra_back_index"
HJLL_SCRIPT_REASON_CODE = "hjll_script_completed"

_AGE_GATE_MARKERS = (
    "我已满18岁",
    "我年满18岁",
    "我已满18岁-进入",
    "我年满18岁-进入",
    "我已满18岁进入",
    "确认已满18",
    "确认年满18",
    "未满18岁禁止进入",
    "内有成人内容",
    "未成年禁止",
    "海角影视",
)
# 开屏／线路失败后的卫星锅空态；产品脚本必须先点重试，不能继续点关闭／圆 X。
_LOAD_FAIL_MARKERS = (
    "加载失败，点击重试",
    "加载失败点击重试",
    "加载失败",
    "点击重试",
    "重试重新加载",
    "重新加载",
)
# 试看结束页上的尊享卡。一次页内返回只关掉这张卡，人还停在详情页。
_VIP_CARD_MARKERS = ("尊享卡",)


@dataclass(frozen=True)
class HjllScriptStep:
    tag: str
    action: InteractionOperation
    semantic: str
    purpose: str
    settle_ms: int
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
) -> HjllScriptStep:
    return HjllScriptStep(
        tag=tag,
        action=InteractionOperation.CLICK,
        semantic=semantic,
        purpose=purpose,
        settle_ms=settle_ms,
        x=x,
        y=y,
    )


def _back(
    tag: str,
    semantic: str,
    purpose: str,
    *,
    settle_ms: int,
) -> HjllScriptStep:
    return HjllScriptStep(
        tag=tag,
        action=InteractionOperation.BACK,
        semantic=semantic,
        purpose=purpose,
        settle_ms=settle_ms,
    )


def _shot(tag: str, semantic: str, *, settle_ms: int) -> HjllScriptStep:
    return HjllScriptStep(
        tag=tag,
        action=InteractionOperation.OBSERVE,
        semantic=semantic,
        purpose=f"按 hjll.py 留證截圖 {tag}；本步不選點。",
        settle_ms=settle_ms,
    )


def hjll_script_steps() -> tuple[HjllScriptStep, ...]:
    """與 hjll.py 主流程同序：開屏一張、彈窗 03 與 05–11、上方五欄、短劇四張、暗網支付、底欄。"""

    steps: list[HjllScriptStep] = [
        # 开屏只留两张证据：本步 OBSERVE=开屏本体；下一步 close 的 after=关闭后。
        _shot("01", "开屏本体", settle_ms=300),
        _tap(
            "close",
            "关闭开屏广告",
            "按 hjll.py 点右上「关闭」(0.866, 0.100)，只点一次。",
            0.866,
            0.100,
            settle_ms=1_000,
        ),
    ]
    # 关掉后的首页圆 X 只留 03，不再留 04。随后 05..11。
    # 每步点之前的 before 截图即该弹窗留证，不再多插一轮只观察。
    for number in (3, *range(5, 12)):
        tag = f"{number:02d}"
        steps.append(
            _tap(
                f"x{tag}",
                "关闭广告弹窗",
                "按 hjll.py 点下方圆 X (0.50, 0.80)；不搜索白点，不因未生效改点。",
                0.50,
                0.80,
                settle_ms=3_000,
            )
        )
    # 2026-10-08 17:07 qa-b 1440x3040。未滑动：最新字心 0.070、精选短剧 0.250、
    # 推荐 0.431、国产 0.572。0.050 和 0.089 都偏在「最新」字边，选不中。
    # 点中国产后栏目左滑：乱伦 0.640、动漫 0.780、漫画 0.919。
    # 动漫用滑后的 0.780。动漫居中后漫画约在 0.640。不点精选短剧、乱伦。
    # 试看页「<」字心 (0.067, 0.076)。(0.043, 0.096) 在箭头左下方空处。
    top_y = 0.131
    player_back = (0.067, 0.076)
    top_tabs = (
        ("最新", 0.070, 0.25, 0.30, *player_back),
        ("推荐", 0.431, 0.25, 0.62, *player_back),
        ("国产", 0.572, 0.25, 0.72, *player_back),
        ("动漫", 0.780, 0.18, 0.78, *player_back),
        ("漫画", 0.640, 0.18, 0.78, *player_back),
    )
    for label, tab_x, item_x, item_y, back_x, back_y in top_tabs:
        steps.append(
            _tap(
                f"top-{label}",
                f"上方分类「{label}」",
                f"按 hjll.py 点上方栏目「{label}」({tab_x:.3f}, {top_y:.3f})。",
                tab_x,
                top_y,
                settle_ms=1_500,
            )
        )
        steps.append(
            _tap(
                f"top-{label}-item",
                f"上方分类「{label}」第一项",
                f"按 hjll.py 点「{label}」第一项 ({item_x:.2f}, {item_y:.2f})。",
                item_x,
                item_y,
                settle_ms=2_000,
            )
        )
        steps.append(
            _tap(
                f"top-{label}-back",
                f"「{label}」返回",
                f"按 hjll.py 点「{label}」页内返回 ({back_x:.3f}, {back_y:.3f})，不用系统 BACK。",
                back_x,
                back_y,
                settle_ms=1_200,
            )
        )
    steps.append(
        _tap(
            "bottom-短剧",
            "底栏「短剧」",
            "按 hjll.py 点底栏「短剧」(0.267, 0.900)。",
            0.267,
            0.900,
            settle_ms=1_500,
        )
    )
    for shot in range(2, 5):
        steps.append(
            _shot(
                f"短剧{shot}",
                f"短剧截图{shot}/4",
                settle_ms=30_000 if shot < 4 else 1_200,
            )
        )
    # 四张截完仍在播放页。不先按左上 <，底栏会点到「观看完整剧集」。
    # 2026-10-08 17:29 播放页箭头 (0.043, 0.097)。不用系统 BACK，也不点金币解锁。
    steps.append(
        _tap(
            "短剧-back",
            "短剧返回",
            "短剧播放页左上角「<」在 (0.043, 0.097)。不用系统 BACK。",
            0.043,
            0.097,
            settle_ms=1_200,
        )
    )
    for label, x in (
        ("暗网", 0.421),
    ):
        steps.append(
            _tap(
                f"bottom-{label}",
                f"底栏「{label}」",
                f"按 hjll.py 点底栏「{label}」({x:.3f}, 0.900)。",
                x,
                0.900,
                settle_ms=1_500,
            )
        )
    # 暗网页支付链。点完各按钮后留一张证。外跳浏览器先关下载框再返回，
    # 不要点 Download。仍在浏览器时由 override 继续 BACK，不往下点社区。
    # 会员中心蓝色「¥300/立即支付」中心是 (0.499, 0.868)，0.535 偏在按钮右侧。
    for tag, semantic, x, y, settle_ms in (
        ("pay-unlock", "立即解锁", 0.501, 0.790, 1_500),
        ("pay-now", "立即支付", 0.499, 0.868, 1_500),
        ("pay-now-2", "立即支付", 0.494, 0.856, 1_500),
        ("pay-known", "我知道了", 0.490, 0.742, 1_500),
    ):
        steps.append(
            _tap(
                tag,
                semantic,
                f"按 hjll.py 点暗网「{semantic}」({x:.3f}, {y:.3f})。",
                x,
                y,
                settle_ms=settle_ms,
            )
        )
        steps.append(_shot(f"{tag}-shot", f"{semantic}后", settle_ms=400))
    steps.append(_shot("pay-browser", "外跳浏览器", settle_ms=2_500))
    steps.append(
        _tap(
            "pay-cancel",
            "取消下载",
            "下载询问框左侧 Cancel (0.272, 0.893)。不要点右侧 Download。",
            0.272,
            0.893,
            settle_ms=1_000,
        )
    )
    steps.append(
        _back(
            "pay-browser-back",
            "离开浏览器",
            "云手机返回。一次返回往往只关掉下载框，人还在浏览器里。",
            settle_ms=1_200,
        )
    )
    steps.append(_shot("pay-return", "回到app", settle_ms=400))
    steps.append(
        _tap(
            "pay-success",
            "支付成功",
            "回到 App 后点「支付成功」(0.487, 0.658)。",
            0.487,
            0.658,
            settle_ms=1_500,
        )
    )
    steps.append(
        _tap(
            "pay-back",
            "返回暗网",
            "支付页左上角「<」(0.059, 0.080)，回到暗网页。",
            0.059,
            0.080,
            settle_ms=1_200,
        )
    )
    for label, x in (
        ("AI", 0.577),
        ("社区", 0.735),
        ("我的", 0.890),
    ):
        steps.append(
            _tap(
                f"bottom-{label}",
                f"底栏「{label}」",
                f"按 hjll.py 点底栏「{label}」({x:.3f}, 0.900)。",
                x,
                0.900,
                settle_ms=1_500,
            )
        )
    # 我的页会弹出账号凭证。不点「保存凭证」和「我已保存」，脚本到此结束收口。
    return tuple(steps)


def _metadata(work: ExplorationWork) -> Mapping[str, object]:
    metadata = work.frontier.observation.metadata
    return metadata if isinstance(metadata, Mapping) else {}


def _is_hjll_work(work: ExplorationWork) -> bool:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    from services.api.app.application.app_ui_tree_features import (
        resolve_product_orchestrator_id,
    )

    package_name = str(_metadata(work).get("package_name") or "").strip()
    if resolve_product_orchestrator_id(package_name=package_name) == "hjll":
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(
        resolve_product_orchestrator_id(package_name=str(item)) == "hjll" for item in allowed
    )


def _expected_package(work: ExplorationWork) -> str:
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if package_name == "com.hj.ttthree":
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    for item in allowed:
        if str(item).strip() == "com.hj.ttthree":
            return "com.hj.ttthree"
    return package_name or "com.hj.ttthree"


def _script_index(metadata: Mapping[str, object]) -> int:
    raw = metadata.get(HJLL_SCRIPT_INDEX_KEY)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _age_gate_visible(work: ExplorationWork, metadata: Mapping[str, object]) -> bool:
    parts = [
        str(metadata.get("visual_summary") or ""),
        str(metadata.get("ocr_text") or ""),
        str(metadata.get("page_type") or ""),
    ]
    elements = metadata.get("key_elements")
    if isinstance(elements, (list, tuple)):
        parts.extend(str(item) for item in elements)
    # 15:55 的「我已满18岁」在画面元素上，不在 visual_summary 里。
    parts.extend(str(item) for item in work.frontier.observation.key_elements)
    blob = " ".join(parts)
    return any(marker in blob for marker in _AGE_GATE_MARKERS)


def _foreground_needs_cloud_phone_back(metadata: Mapping[str, object]) -> bool:
    """外跳浏览器或 Play 商店时，只按云手机返回。只看前景，不看页面文案。"""

    blob = " ".join(
        str(metadata.get(key) or "")
        for key in (
            "package_name",
            "current_focus",
            "focused_package",
            "foreground_package",
        )
    ).casefold()
    return any(token in blob for token in ("sbrowser", "chrome", "browser", "vending"))


def _screen_text(metadata: Mapping[str, object]) -> str:
    parts = [
        str(metadata.get("visual_summary") or ""),
        str(metadata.get("ocr_text") or ""),
        str(metadata.get("page_type") or ""),
        str(metadata.get("page_state") or ""),
    ]
    elements = metadata.get("key_elements")
    if isinstance(elements, (list, tuple)):
        parts.extend(str(item) for item in elements)
    return " ".join(parts)


def _vip_card_visible(metadata: Mapping[str, object]) -> bool:
    blob = _screen_text(metadata)
    return any(marker in blob for marker in _VIP_CARD_MARKERS)


def _vip_extra_back_index(metadata: Mapping[str, object]) -> int | None:
    raw = metadata.get(HJLL_VIP_CARD_EXTRA_BACK_INDEX_KEY)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


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
    step: HjllScriptStep,
    index_token: str,
) -> ActionIntent:
    capture_id = str(work.source_capture_id or "unknown")
    candidate_id = f"hjll-script:{index_token}:{step.tag}"
    digest = hashlib.sha256(
        json.dumps(
            [work.work_id, capture_id, candidate_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate_id,
        "hjll_script": True,
        "hjll_script_tag": step.tag,
        "hjll_orchestrated": True,
        "hjll_expected_package": _expected_package(work),
        "hjll_settle_after_action_ms": step.settle_ms,
        "require_screenshot_evidence": True,
    }
    # 开屏：OBSERVE 只验本体；关闭点击复用前帧视觉，只验 after。
    if step.tag == "01" and step.action is InteractionOperation.OBSERVE:
        parameters["splash_body_evidence"] = True
    if step.tag == "close":
        parameters["splash_skip_after_only"] = True
    if step.x is not None and step.y is not None:
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
        risk_level=(
            InteractionRiskLevel.MEDIUM
            if step.tag in {"pay-now", "pay-now-2", "pay-success", "pay-back"}
            else InteractionRiskLevel.LOW
        ),
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            step.action,
        ),
        deadline=work.deadline,
    )


def _completion(work: ExplorationWork) -> ExplorationTerminal:
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason=(
            "HJLL 录制步骤已全部走完。最后一步是底栏「我的」，"
            "不点「保存凭证」和「我已保存」。"
        ),
        outcome="exploration_completed",
        reason_code=HJLL_SCRIPT_REASON_CODE,
        coverage_summary={
            "hjll_script_completed": True,
            "hjll_script_steps": len(hjll_script_steps()),
            # 點到「我的」只寫封口標記。規則或質檢工作項近 3 分鐘還在更新時不封，
            # 停更後才按這個手動封口放行。標記寫入後不能再改。
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
    )


def hjll_script_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """HJLL 的下一步只來自腳本。非 HJLL 返回 None，交給通用 Agent。"""

    if not isinstance(work, ExplorationWork) or not _is_hjll_work(work):
        return None
    metadata = _metadata(work)
    index = _script_index(metadata)
    if (
        index == 0
        and metadata.get(HJLL_AGE_GATE_DONE_KEY) is not True
        and _age_gate_visible(work, metadata)
    ):
        return _intent_for_step(
            work=work,
            index_token="age",
            step=_tap(
                "age_gate",
                "我已满18岁",
                "首次安装的蓝色「我已满18岁」在开屏之前。"
                "按钮中心 (0.465, 0.592)。没有该层则跳过。禁止点未满18。",
                0.465,
                0.592,
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
    steps = hjll_script_steps()
    if index >= len(steps):
        return _completion(work)
    # 我知道了会外跳浏览器。还在浏览器里就发云手机返回，不要先去观察或点取消。
    # 已经回到 App 就跳过下载框和返回，避免再按一次把人送回桌面。
    pay_known_at = next(
        (position for position, step in enumerate(steps) if step.tag == "pay-known"),
        None,
    )
    pay_return_at = next(
        (position for position, step in enumerate(steps) if step.tag == "pay-return"),
        None,
    )
    outside_app = _foreground_needs_cloud_phone_back(metadata)
    if (
        pay_known_at is not None
        and pay_return_at is not None
        and pay_known_at < index < pay_return_at
    ):
        if outside_app:
            return _intent_for_step(
                work=work,
                index_token="browser_back",
                step=_back(
                    "browser_back",
                    "离开外部页面",
                    "仍在浏览器或 Play 商店，按云手机返回回到 App，不用 monkey 拉到桌面。",
                    settle_ms=1_200,
                ),
            )
        if steps[index].tag in {"pay-browser", "pay-cancel", "pay-browser-back"}:
            return _intent_for_step(
                work=work,
                step=steps[pay_return_at],
                index_token=str(pay_return_at),
            )
    # 支付外跳后，一次返回往往只关掉下载框。人还在浏览器时不要继续点社区。
    cancel_at = next(
        (position for position, step in enumerate(steps) if step.tag == "pay-cancel"),
        None,
    )
    if (
        cancel_at is not None
        and index > cancel_at
        and outside_app
    ):
        return _intent_for_step(
            work=work,
            index_token="browser_back",
            step=_back(
                "browser_back",
                "离开外部页面",
                "仍在浏览器或 Play 商店，再按一次云手机返回，回到 App 后才继续后面的步骤。",
                settle_ms=1_200,
            ),
        )
    step = steps[index]
    # 尊享卡出现在试看页时，脚本里的那一次返回只关掉卡片。先多按一次页内返回。
    # 同一格只多按一次，避免卡片还在时连按。支付页和底栏不走这里。
    if (
        step.tag.startswith("top-")
        and step.tag.endswith("-back")
        and _vip_card_visible(metadata)
        and _vip_extra_back_index(metadata) != index
    ):
        return _intent_for_step(
            work=work,
            index_token="vip_card_back",
            step=_tap(
                "vip_card_back",
                "关闭尊享卡",
                "出现尊享卡时，先按页内返回 (0.067, 0.076) 多按一次，"
                "再继续脚本里的返回。不用系统 BACK。",
                0.067,
                0.076,
                settle_ms=1_200,
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


def apply_hjll_script_progress(
    result: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: object,
    action_succeeded: bool | None,
) -> None:
    """腳本步成功後才前進；失敗留在同一步重試。無關動作不改計數。"""

    result[HJLL_SCRIPT_INDEX_KEY] = _script_index(prior)
    result[HJLL_AGE_GATE_DONE_KEY] = prior.get(HJLL_AGE_GATE_DONE_KEY) is True
    vip_extra_at = _vip_extra_back_index(prior)
    if vip_extra_at is not None:
        result[HJLL_VIP_CARD_EXTRA_BACK_INDEX_KEY] = vip_extra_at
    if completed_action is None or action_succeeded is False:
        return
    candidate_id = _action_candidate_id(completed_action)
    if not candidate_id.startswith("hjll-script:"):
        return
    parts = candidate_id.split(":")
    if len(parts) < 3:
        return
    if parts[1] == "age":
        result[HJLL_AGE_GATE_DONE_KEY] = True
        return
    if parts[1] == "vip_card_back":
        result[HJLL_VIP_CARD_EXTRA_BACK_INDEX_KEY] = _script_index(prior)
        return
    try:
        finished = int(parts[1])
    except ValueError:
        return
    if finished < _script_index(prior):
        return
    steps = hjll_script_steps()
    tag = parts[2]
    # 進行中的舊步驟若已被新步驟佔用同一編號（例如短劇第 4 張改成左上角返回），
    # 留在這格，不要跳去下一欄。
    if finished < len(steps) and steps[finished].tag != tag:
        result[HJLL_SCRIPT_INDEX_KEY] = finished
        return
    result[HJLL_SCRIPT_INDEX_KEY] = finished + 1
