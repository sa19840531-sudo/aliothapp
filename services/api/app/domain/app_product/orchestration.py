"""纯决策辅助：根据 Profile 给出下一步编排建议（不触达设备）。

只消费当前包的关键落点与时序。关窗分型硬规则（全产品）：

- **开屏／首屏**：语义「进入／跳过」+ 右上相对区（``vision_skip``）
- **弹窗广告**：Watcher→A 文案→B 几何安全 X→C 蒙层→D「<」连点两下
- **官方公告**：语义「我知道了」（``vision_tap``／``notice_ack``）
"""

from __future__ import annotations

from typing import Any

from services.api.app.domain.app_product.popup_close import (
    normalize_popup_type,
    resolve_popup_close_plan,
)
from services.api.app.domain.app_product.profiles import AppProductProfile, load_profile


def _tap_decision(
    profile: AppProductProfile,
    *,
    page_hypothesis: str,
    tap_key: str,
    reason: str,
) -> dict[str, Any]:
    tap = profile.key_taps.get(tap_key)
    clamped = profile.clamp_tap_y(*tap) if tap else None
    return {
        "ok": True,
        "package_name": profile.package_name,
        "page_hypothesis": page_hypothesis,
        "next_action": "tap",
        "tool": "app_tap_from_profile_tool",
        "tap_key": tap_key,
        "tap": list(clamped) if clamped else None,
        "reason": reason,
    }


def suggest_popup_close_action(
    *,
    package_name: str,
    popup_type: str | None,
    page_hypothesis: str | None = None,
) -> dict[str, Any]:
    """按弹窗类型给出关窗动作（弹窗默认 watcher cascade；D 档 cloud_back_twice）。"""
    from services.api.app.domain.app_product.popup_close import (
        DEFAULT_POPUP_CLOSE_POLICY,
    )

    profile = load_profile(package_name)
    raw_type = popup_type or page_hypothesis
    plan = resolve_popup_close_plan(
        raw_type,
        profile_overrides=(profile.popup_close_by_type if profile else None),
    )
    action = str(plan.get("action") or "popup_watcher_cascade")
    package = profile.package_name if profile else package_name
    policy = plan.get("popup_close_policy") or (
        profile.popup_close_policy if profile else DEFAULT_POPUP_CLOSE_POLICY
    )
    base_forbid = list(plan.get("forbid") or [])
    if action == "vision_skip":
        # 开屏允许语义 skip；仍禁止脚本绝对点与乱猜圆 X
        extra_forbid = [
            "product_script_popup_close_tap",
            "profile_popup_close_tap",
            "script_entry_tap",
            "guess_absolute_xy_without_vision_bounds",
        ]
    elif action in {
        "popup_watcher_cascade",
        "vision_tap",
        "cloud_back",
        "cloud_back_twice",
    }:
        # 弹窗 cascade 允许几何安全 X／文案；禁脚本绝对点（D 档允许 twice）。
        extra_forbid = [
            "product_script_popup_close_tap",
            "profile_popup_close_tap",
            "guess_absolute_xy_without_vision_bounds",
        ]
    else:
        extra_forbid = [
            "product_script_popup_close_tap",
            "profile_popup_close_tap",
            "guess_absolute_xy_without_vision_bounds",
        ]
    decision: dict[str, Any] = {
        "ok": True,
        "package_name": package,
        "page_hypothesis": page_hypothesis or popup_type,
        "popup_type": plan.get("popup_type"),
        "normalized_popup_type": normalize_popup_type(raw_type),
        "next_action": action,
        "popup_close_policy": policy,
        "target_hint": plan.get("target_hint"),
        "forbid": base_forbid + extra_forbid,
        "reason": (
            f"按弹窗类型 {plan.get('popup_type')} → {action}；"
            f"{plan.get('target_hint') or 'Watcher→文案→几何X→蒙层→连点两下<'}"
        ),
    }
    if action == "popup_watcher_cascade":
        decision["tool"] = plan.get("tool")
        decision["cascade"] = list(
            plan.get("cascade")
            or ("text_ocr", "geometry_safe_x", "scrim", "cloud_back_twice")
        )
        decision["fallback_action"] = (
            plan.get("fallback_action") or "cloud_back_twice"
        )
        decision["fallback_tool"] = (
            plan.get("fallback_tool") or "app_cloud_back_twice_tool"
        )
        decision["forbid_between"] = ["sleep", "screenshot", "capture_screen"]
        if profile is not None:
            decision["cloud_back_tap"] = list(profile.cloud_back_tap)
    elif action == "cloud_back_twice":
        decision["tool"] = plan.get("tool") or "app_cloud_back_twice_tool"
        decision["forbid_between"] = list(
            plan.get("forbid_between") or ["sleep", "screenshot", "capture_screen"]
        )
        if profile is not None:
            decision["cloud_back_tap"] = list(profile.cloud_back_tap)
    elif action == "cloud_back":
        decision["tool"] = plan.get("tool") or "app_cloud_back_tool"
        if profile is not None:
            decision["cloud_back_tap"] = list(profile.cloud_back_tap)
    elif action == "vision_skip":
        decision["tool"] = plan.get("tool")
        decision["control_role"] = plan.get("control_role") or "skip"
        decision["expected_state_hint"] = plan.get("expected_state_hint") or "tap_skip"
        decision["semantic_markers"] = list(
            plan.get("semantic_markers")
            or ["进入", "跳过", "skip", "entry", "enter"]
        )
        decision["relative_region"] = dict(
            plan.get("relative_region")
            or {"left": 0.65, "top": 0.0, "right": 1.0, "bottom": 0.22}
        )
        decision["default_normalized_bounds"] = dict(
            plan.get("default_normalized_bounds")
            or {"left": 0.78, "top": 0.02, "right": 0.98, "bottom": 0.10}
        )
        decision["startup_ad_skip_contract"] = plan.get("startup_ad_skip_contract") or (
            "app-startup-ad-skip.v1"
        )
    elif action == "vision_tap":
        decision["tool"] = plan.get("tool")
        decision["control_role"] = plan.get("control_role") or "dismiss"
        decision["expected_state_hint"] = plan.get("expected_state_hint") or "notice_ack"
        decision["semantic_markers"] = list(
            plan.get("semantic_markers") or ["我知道了", "知道了"]
        )
    elif action == "wait_observe":
        decision["tool"] = plan.get("tool") or "app_screenshot_tool"
    else:
        decision["tool"] = None
    if profile is not None:
        decision["popup_notes"] = profile.popup_notes
    return decision


def suggest_next_action(
    *,
    package_name: str,
    page_hypothesis: str,
    popup_type: str | None = None,
) -> dict[str, Any]:
    """根据页面假说与 Profile 给出 Agent 下一步动作建议。"""
    profile = load_profile(package_name)
    if profile is None:
        return {
            "ok": False,
            "package_name": package_name,
            "error": "profile_not_found",
            "next_action": "explore_generic",
            "reason": "无产品 Profile，使用通用受控探索并准备新建 Profile，勿写产品脚本",
            "popup_close_policy": "cloud_back",
        }
    hypothesis = (page_hypothesis or "").strip().lower()
    typed = normalize_popup_type(popup_type) or normalize_popup_type(page_hypothesis)

    popup_tokens = (
        "popup",
        "ad_popup",
        "overlay",
        "弹窗",
        "遮罩",
        "关闭弹层",
        "close_x",
        "dismiss",
        "startup_ad",
        "startup_skip",
        "scrim",
        "age_gate",
        "age_confirm",
        "grid_ad_below_x",
        "corner_close_x",
        "paywall_grid",
    )
    if (
        typed is not None
        or hypothesis in popup_tokens
        or any(token in hypothesis for token in popup_tokens)
    ):
        return suggest_popup_close_action(
            package_name=package_name,
            popup_type=popup_type or typed or page_hypothesis,
            page_hypothesis=page_hypothesis,
        )

    membership_hypotheses = {
        "membership_intercept",
        "paywall",
        "会员拦截",
        "试看结束",
        "membership",
    }
    if hypothesis in membership_hypotheses or any(
        token in hypothesis for token in ("会员", "paywall", "membership")
    ):
        # 会员拦截走弹窗 cascade（文案→几何X→蒙层→连点两下<）。
        typed_decision = suggest_popup_close_action(
            package_name=package_name,
            popup_type="membership_intercept",
            page_hypothesis=page_hypothesis,
        )
        if typed_decision.get("next_action") in {
            "popup_watcher_cascade",
            "cloud_back_twice",
            "cloud_back",
        }:
            return typed_decision
        for rule in profile.timing_rules:
            rule_id = str(rule.get("id") or "")
            when = str(rule.get("when") or "").lower()
            action = str(rule.get("action") or "")
            if not (
                "intercept" in rule_id
                or "会员" in when
                or "cloud_back" in action
                or "twice" in action
                or "double" in action
            ):
                continue
            return {
                "ok": True,
                "package_name": profile.package_name,
                "page_hypothesis": page_hypothesis,
                "popup_type": "membership_intercept",
                "profile_rule_id": rule_id,
                "next_action": "popup_watcher_cascade",
                "cascade": [
                    "text_ocr",
                    "geometry_safe_x",
                    "scrim",
                    "cloud_back_twice",
                ],
                "fallback_action": "cloud_back_twice",
                "fallback_tool": "app_cloud_back_twice_tool",
                "forbid_between": ["sleep", "screenshot", "capture_screen"],
                "cloud_back_tap": list(profile.cloud_back_tap),
                "reason": (
                    "Profile 时序：会员拦截走 Watcher cascade，"
                    "D 档连点两下云手机「<」"
                ),
            }
    if hypothesis in {"browser", "payment_browser", "外跳", "支付浏览器", "chrome"}:
        return {
            "ok": True,
            "package_name": profile.package_name,
            "page_hypothesis": page_hypothesis,
            "next_action": "cloud_back",
            "tool": "app_cloud_back_tool",
            "cloud_back_tap": list(profile.cloud_back_tap),
            "reason": "外跳浏览器/支付页：点云手机可见 back，不是 keyevent 4",
        }
    if hypothesis in {"me_tab", "mine", "我", "profile_tab"}:
        return _tap_decision(
            profile,
            page_hypothesis=page_hypothesis,
            tap_key="me_tab",
            reason="底栏「我」使用 Profile.me_tab，勿用专题带坐标",
        )
    if hypothesis in {"detail", "detail_page", "详情", "drama_detail"}:
        return _tap_decision(
            profile,
            page_hypothesis=page_hypothesis,
            tap_key="detail_page_back",
            reason="详情左上角返回使用实拍落点，勿用偏右旧点",
        )
    if hypothesis in {"coin_300", "金币300", "充值300", "wallet_300"}:
        return _tap_decision(
            profile,
            page_hypothesis=page_hypothesis,
            tap_key="coin_300",
            reason="金币钱包「300/¥30」左上格，勿点 300|500 缝隙",
        )
    if hypothesis in {"wallet_back", "wallet_page_back", "金币钱包返回", "钱包返回"}:
        return _tap_decision(
            profile,
            page_hypothesis=page_hypothesis,
            tap_key="wallet_page_back",
            reason="钱包页左上角返回用箭头尖端，勿点标题空白",
        )
    if hypothesis in {"wallet", "金币钱包", "wallet_entry"}:
        return _tap_decision(
            profile,
            page_hypothesis=page_hypothesis,
            tap_key="wallet_entry",
            reason="进入金币钱包相关入口（Profile.wallet_entry）",
        )
    if hypothesis in {"first_drama", "first_left_video", "最新上架", "首部剧"}:
        return _tap_decision(
            profile,
            page_hypothesis=page_hypothesis,
            tap_key="first_left_video",
            reason="最新上架左边第一部（Profile.first_left_video）",
        )
    summary = profile.to_agent_summary()
    return {
        "ok": True,
        "package_name": profile.package_name,
        "page_hypothesis": page_hypothesis,
        "next_action": "screenshot_then_detect",
        "tool": "app_screenshot_tool",
        "reason": (
            "未命中专用时序，先截图再认页与 popup_type；"
            "弹窗在则 Watcher→文案→几何X→蒙层→连点两下「<」"
        ),
        "key_taps": summary["key_taps"],
        "popup_close_policy": summary["popup_close_policy"],
        "popup_close_by_type": summary["popup_close_by_type"],
        "exploration_curriculum": summary["exploration_curriculum"],
    }


def membership_double_back_plan(profile: AppProductProfile) -> dict[str, Any]:
    """MaMa 试点用：明确双 back 计划（供测试断言）。"""
    return {
        "steps": [
            {"op": "cloud_back", "xy": list(profile.cloud_back_tap)},
            {"op": "cloud_back", "xy": list(profile.cloud_back_tap)},
            {"op": "screenshot", "name": "after_double_back"},
        ],
        "forbid_between_backs": ["sleep", "screenshot", "capture_screen"],
        "package_name": profile.package_name,
    }


def mama_agent_curriculum(profile: AppProductProfile | None = None) -> list[dict[str, Any]]:
    """返回 MaMa 探索课纲（提示词用），不是可执行固定脚本。"""
    loaded = profile or load_profile("com.dzu4.app")
    if loaded is None:
        return []
    raw = loaded.data.get("exploration_curriculum") or []
    return [dict(item) for item in raw if isinstance(item, dict)]
