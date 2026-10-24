"""全产品关窗分型（开屏 ≠ 弹窗，禁止混用）。

**开屏 / 首屏广告**（``startup_skip``）
  - 语义图标／文案 + 右上相对位置
  - 语义标记仅限：进入／跳过／skip／entry／enter
  - 禁止：cloud_back、keyevent 4、弹层下方／右上 X、脚本 ENTRY_TAP

**常规弹窗广告**（``watcher_cascade``）
  - Watcher → **A 文案**（跳过／关闭／我知道了等）
    → **B 几何安全 X**（跟像素检出落点；相对弹层面板右上／下沿／紧贴下方；
       **禁止写死屏幕绝对 Y**；无面板时才弱回落屏幕分区；禁中部假 X）
    → **C 蒙层空白**
    → **D 底栏「<」连点两下**（``cloud_back_twice``／``app_cloud_back_twice_tool``；中间禁止截图）
  - 禁止：脚本 ``POPUP_CLOSE_*``、keyevent 4、开屏进入混用、只点一下底栏「<」当 D 档

**官方公告／确认文案弹窗**（``notice_ack``）
  - 点语义「我知道了／知道了」等确认钮（有 UIA／视觉框）
  - 禁止：当开屏进入混用；无确认钮时再走 cascade（含 D 档连点「<」）
"""

from __future__ import annotations

from typing import Any

# 公告／确认弹窗：点文案钮（cascade 的 A 档也认这些标记）。
NOTICE_ACK_MARKERS: tuple[str, ...] = (
    "我知道了",
    "知道了",
    "got it",
    "gotit",
)

# 弹窗 A 档：OCR／语义关钮（不含裸「×」图形，那是 B 档）。
POPUP_TEXT_CLOSE_MARKERS: tuple[str, ...] = (
    *NOTICE_ACK_MARKERS,
    "跳过",
    "跳過",
    "略过",
    "略過",
    "关闭",
    "關閉",
    "关掉",
    "關掉",
    "skip",
    "close",
    "dismiss",
)

_CASCADE_PLAN: dict[str, Any] = {
    "action": "popup_watcher_cascade",
    "tool": None,
    "cascade": (
        "text_ocr",
        "geometry_safe_x",
        "scrim",
        "cloud_back_twice",
    ),
    "fallback_action": "cloud_back_twice",
    "fallback_tool": "app_cloud_back_twice_tool",
    "target_hint": (
        "弹窗广告：Watcher 后按 A 文案关钮 → B 几何安全圆 X（贴面板右上／下沿）"
        "→ C 蒙层空白 → 仍挡路再连点两下底栏「<」（cloud_back_twice；中间不截图；"
        "禁止 keyevent 4／脚本 POPUP_CLOSE／当开屏点进入）"
    ),
    "forbid": [
        "script_xy",
        "profile_popup_close_tap",
        "page_back",
        "keyevent_4",
        "startup_enter_skip",
        "product_script_popup_close_tap",
        "cloud_back_once_as_d_fallback",
    ],
    "popup_close_policy": "watcher_cascade",
}

# 开屏：语义图标/文案 + 右上相对位置（对齐 app_startup_ad_skip 合同）。
# 标记刻意不含「关闭」——避免与弹窗关层语义混用。
_STARTUP_SEMANTIC_MARKERS: list[str] = [
    "进入",
    "跳过",
    "skip",
    "entry",
    "enter",
]

_STARTUP_SEMANTIC_PLAN: dict[str, Any] = {
    "action": "vision_skip",
    "tool": None,
    "control_role": "skip",
    "expected_state_hint": "tap_skip",
    "semantic_markers": list(_STARTUP_SEMANTIC_MARKERS),
    "relative_region": {
        "left": 0.65,
        "top": 0.0,
        "right": 1.0,
        "bottom": 0.22,
    },
    "default_normalized_bounds": {
        "left": 0.78,
        "top": 0.02,
        "right": 0.98,
        "bottom": 0.10,
    },
    "startup_ad_skip_contract": "app-startup-ad-skip.v1",
    "target_hint": (
        "开屏/首屏广告：点语义「进入/跳过」控件，落在右上相对区"
        "（默认归一化框 0.78–0.98 × 0.02–0.10）；"
        "禁止脚本 ENTRY_TAP、云手机 back（会退出 App）、弹层下方/右上 X、点广告主体"
    ),
    "forbid": [
        "script_entry_tap",
        "ENTRY_TAP",
        "cloud_back",
        "cloud_back_twice",
        "keyevent_4",
        "ad_body",
        "popup_center_round_x",
        "popup_close_corner",
        "popup_close_bottom_x",
        "vision_find_x",
        "absolute_xy_without_semantic",
    ],
    "popup_close_policy": "startup_semantic_relative",
}

# 规范类型 → 关闭动作。产品 Profile 可用 popup_close_by_type 覆盖同名字段。
DEFAULT_POPUP_CLOSE_BY_TYPE: dict[str, dict[str, Any]] = {
    "grid_ad_below_x": dict(_CASCADE_PLAN),
    "portrait_ad_below_x": dict(_CASCADE_PLAN),
    "corner_close_x": dict(_CASCADE_PLAN),
    "startup_skip": dict(_STARTUP_SEMANTIC_PLAN),
    "scrim_outside": dict(_CASCADE_PLAN),
    "age_confirm": dict(_CASCADE_PLAN),
    "paywall_grid_overlay": dict(_CASCADE_PLAN),
    "credential_prompt": dict(_CASCADE_PLAN),
    "no_close_auto": dict(_CASCADE_PLAN),
    "membership_intercept": dict(_CASCADE_PLAN),
    "notice_ack": {
        "action": "vision_tap",
        "tool": None,
        "control_role": "dismiss",
        "expected_state_hint": "notice_ack",
        "semantic_markers": list(NOTICE_ACK_MARKERS),
        "target_hint": (
            "官方公告／确认弹窗：点语义「我知道了／知道了」确认钮；"
            "禁止当开屏进入；无确认钮时再走 watcher cascade（含单次 cloud_back）"
        ),
        "forbid": [
            "startup_enter_skip",
            "script_xy",
            "keyevent_4",
            "cloud_back_twice",
        ],
        "popup_close_policy": "notice_ack_semantic",
    },
}

_TYPE_ALIASES: dict[str, str] = {
    "ad_popup": "grid_ad_below_x",
    "popup": "grid_ad_below_x",
    "overlay": "grid_ad_below_x",
    "弹窗": "grid_ad_below_x",
    "弹窗广告": "grid_ad_below_x",
    "遮罩": "grid_ad_below_x",
    "关闭弹层": "grid_ad_below_x",
    "close_x": "grid_ad_below_x",
    "dismiss": "grid_ad_below_x",
    "below_x": "grid_ad_below_x",
    "startup_ad": "startup_skip",
    "skip": "startup_skip",
    "splash": "startup_skip",
    "开屏": "startup_skip",
    "開屏": "startup_skip",
    "首屏": "startup_skip",
    "首屏广告": "startup_skip",
    "开屏广告": "startup_skip",
    "进入": "startup_skip",
    "entry": "startup_skip",
    "age_gate": "age_confirm",
    "age_verification": "age_confirm",
    "会员拦截": "membership_intercept",
    "paywall": "membership_intercept",
    "试看结束": "membership_intercept",
    "scrim": "scrim_outside",
    "蒙层": "scrim_outside",
    "官方公告": "notice_ack",
    "系统公告": "notice_ack",
    "我知道了": "notice_ack",
    "知道了": "notice_ack",
    "system_notice": "notice_ack",
    "notice_ack": "notice_ack",
}

DEFAULT_POPUP_CLOSE_POLICY = "watcher_cascade"

# 导出给探索／课纲：开屏语义标记白名单。
STARTUP_SEMANTIC_MARKERS: tuple[str, ...] = tuple(_STARTUP_SEMANTIC_MARKERS)


def normalize_popup_type(raw: str | None) -> str | None:
    """归一化弹窗类型；无法识别返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    folded = text.casefold()
    if folded in {k.casefold() for k in DEFAULT_POPUP_CLOSE_BY_TYPE}:
        for key in DEFAULT_POPUP_CLOSE_BY_TYPE:
            if key.casefold() == folded:
                return key
    for alias, canonical in _TYPE_ALIASES.items():
        if alias.casefold() == folded or alias in text:
            return canonical
    return None


def is_startup_close_type(popup_type: str | None) -> bool:
    """是否开屏／首屏关层类型（与弹窗 cascade 硬拆分）。"""

    return normalize_popup_type(popup_type) == "startup_skip"


def is_notice_ack_semantic_target(text: str | None) -> bool:
    """是否官方公告／确认文案关层目标（点「我知道了」等）。"""

    blob = str(text or "").strip().casefold()
    if not blob:
        return False
    return any(marker.casefold() in blob for marker in NOTICE_ACK_MARKERS)


def is_popup_text_close_target(text: str | None) -> bool:
    """弹窗 A 档：语义／OCR 关钮文案（跳过／关闭／我知道了等）。"""

    blob = str(text or "").strip().casefold()
    if not blob:
        return False
    return any(marker.casefold() in blob for marker in POPUP_TEXT_CLOSE_MARKERS)


def resolve_popup_close_plan(
    popup_type: str | None,
    *,
    profile_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按类型生成关窗计划；弹窗默认 watcher cascade，开屏仍语义进入／跳过。"""
    overrides = profile_overrides if isinstance(profile_overrides, dict) else {}
    canonical = normalize_popup_type(popup_type) or (
        popup_type.strip() if isinstance(popup_type, str) and popup_type.strip() else None
    )
    if canonical and canonical in DEFAULT_POPUP_CLOSE_BY_TYPE:
        base = dict(DEFAULT_POPUP_CLOSE_BY_TYPE[canonical])
        extra = overrides.get(canonical)
        if isinstance(extra, dict):
            # 开屏类型禁止被产品覆盖成 cloud_back／乱点圆 X。
            if canonical == "startup_skip":
                blocked = {
                    "action",
                    "tool",
                    "popup_close_policy",
                    "control_role",
                    "expected_state_hint",
                }
                extra = {k: v for k, v in extra.items() if k not in blocked}
            # 公告确认禁止被覆盖成开屏进入。
            elif canonical == "notice_ack":
                blocked = {
                    "action",
                    "tool",
                    "popup_close_policy",
                    "control_role",
                    "expected_state_hint",
                }
                extra = {k: v for k, v in extra.items() if k not in blocked}
            # 弹窗类型禁止被覆盖成开屏进入（D 档 twice 允许）。
            elif str(extra.get("action") or "") == "vision_skip":
                extra = {
                    k: v
                    for k, v in extra.items()
                    if k not in {"action", "tool", "popup_close_policy"}
                }
            base.update(extra)
        base["popup_type"] = canonical
        base.setdefault(
            "popup_close_policy",
            (
                "startup_semantic_relative"
                if canonical == "startup_skip"
                else (
                    "notice_ack_semantic"
                    if canonical == "notice_ack"
                    else DEFAULT_POPUP_CLOSE_POLICY
                )
            ),
        )
        if canonical == "startup_skip":
            base["semantic_markers"] = list(_STARTUP_SEMANTIC_MARKERS)
        if canonical == "notice_ack":
            base["semantic_markers"] = list(NOTICE_ACK_MARKERS)
        return base
    if canonical and canonical in overrides and isinstance(overrides[canonical], dict):
        plan = dict(overrides[canonical])
        plan.setdefault("action", "popup_watcher_cascade")
        plan.setdefault("fallback_action", "cloud_back_twice")
        plan.setdefault("fallback_tool", "app_cloud_back_twice_tool")
        plan["popup_type"] = canonical
        plan.setdefault("popup_close_policy", DEFAULT_POPUP_CLOSE_POLICY)
        return plan
    fallback = dict(_CASCADE_PLAN)
    fallback.update(
        {
            "popup_type": canonical or "unknown",
            "popup_close_policy": DEFAULT_POPUP_CLOSE_POLICY,
            "target_hint": (
                "未识别具体类型：弹窗仍在则 Watcher→A 文案→B 几何安全 X→C 蒙层"
                "→D 连点两下底栏「<」；禁止开屏进入／keyevent 4"
            ),
        }
    )
    return fallback
