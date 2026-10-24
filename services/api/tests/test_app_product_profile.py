"""APP 产品 Profile 与固定脚本切流（不触达真实设备）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.api.app.domain.app_product import (
    list_profile_packages,
    load_profile,
    membership_double_back_plan,
    suggest_next_action,
)


def test_mama_profile_loads() -> None:
    profile = load_profile("com.dzu4.app")
    assert profile is not None
    assert profile.package_name == "com.dzu4.app"
    assert profile.cloud_back_tap == (1120, 2955)
    assert profile.taps["me_tab"] == (1260, 2794)
    assert profile.taps["detail_page_back"] == (99, 221)
    assert "会员拦截" in profile.markdown or "cloud_back" in profile.markdown.lower()
    assert "com.dzu4.app" in list_profile_packages()


def test_membership_suggests_watcher_cascade() -> None:
    decision = suggest_next_action(
        package_name="com.dzu4.app",
        page_hypothesis="membership_intercept",
    )
    assert decision["ok"] is True
    assert decision["next_action"] == "popup_watcher_cascade"
    assert decision["popup_close_policy"] == "watcher_cascade"
    assert decision["fallback_action"] == "cloud_back_twice"
    assert decision["fallback_tool"] == "app_cloud_back_twice_tool"
    assert "cloud_back_twice" in decision["cascade"]
    assert "text_ocr" in decision["cascade"]
    assert "geometry_safe_x" in decision["cascade"]


def test_me_tab_and_detail_back_from_profile() -> None:
    me = suggest_next_action(package_name="com.dzu4.app", page_hypothesis="me_tab")
    assert me["tap_key"] == "me_tab"
    assert me["tap"] == [1260, 2794]

    detail = suggest_next_action(package_name="com.dzu4.app", page_hypothesis="detail")
    assert detail["tap_key"] == "detail_page_back"
    assert detail["tap"] == [99, 221]


def test_mama_wallet_and_coin_from_profile() -> None:
    coin = suggest_next_action(package_name="com.dzu4.app", page_hypothesis="coin_300")
    assert coin["tap_key"] == "coin_300"
    assert coin["tap"] == [263, 1430]

    back = suggest_next_action(
        package_name="com.dzu4.app", page_hypothesis="wallet_page_back"
    )
    assert back["tap_key"] == "wallet_page_back"
    assert back["tap"] == [79, 216]


def test_mama_curriculum_retained_as_history_only() -> None:
    from services.api.app.domain.app_product import load_profile, mama_agent_curriculum

    profile = load_profile("com.dzu4.app")
    assert profile is not None
    assert profile.data.get("execution_policy") == "agent_profile_only"
    assert "mama.py" in str(profile.data.get("product_script") or "")
    curriculum = mama_agent_curriculum(profile)
    # 课纲仍可作对照知识；主路径使用通用 Agent。
    assert any(step.get("tap_key") == "coin_300" for step in curriculum)
    assert profile.popup_close_policy == "watcher_cascade"


def test_double_back_plan_has_no_ops_between_backs() -> None:
    profile = load_profile("com.dzu4.app")
    assert profile is not None
    plan = membership_double_back_plan(profile)
    assert plan["steps"][0]["op"] == "cloud_back"
    assert plan["steps"][1]["op"] == "cloud_back"
    assert plan["steps"][2]["op"] == "screenshot"
    assert "screenshot" in plan["forbid_between_backs"]


def test_missing_profile() -> None:
    decision = suggest_next_action(
        package_name="com.example.missing.app",
        page_hypothesis="membership_intercept",
    )
    assert decision["ok"] is False
    assert decision["error"] == "profile_not_found"


def test_product_orchestrator_preferred_unless_agent_forced(monkeypatch) -> None:
    from services.api.app.application.app_product_orchestration_policy import (
        product_fixed_script_enabled,
        profile_allows_product_orchestrator,
        try_product_script_override,
    )

    monkeypatch.delenv("ALIOTH_APP_EXECUTION_MODE", raising=False)
    assert product_fixed_script_enabled() is False  # 2.0：固定脚本禁用
    # 无包名单例：各 override 自过滤后仍为 None
    assert try_product_script_override(MagicMock()) is None
    assert profile_allows_product_orchestrator("dark.nvycy.ambpu") is False
    assert profile_allows_product_orchestrator("com.dzu4.app") is False
    assert profile_allows_product_orchestrator("com.alz7xm2.drama") is False
    assert profile_allows_product_orchestrator("com.example.unknown.app") is False

    monkeypatch.setenv("ALIOTH_APP_EXECUTION_MODE", "agent")
    assert product_fixed_script_enabled() is False

    monkeypatch.setenv("ALIOTH_APP_EXECUTION_MODE", "legacy_script")
    assert product_fixed_script_enabled() is False


def test_cloud_back_twice_single_adb_shell_no_screenshot_between(monkeypatch) -> None:
    from services.api.app.agents.tools import app_device_tools as tools

    monkeypatch.setenv("ALIOTH_ADB_SERIAL", "emulator-5554")
    calls: list[list[str]] = []

    def fake_adb(args: list[str], *, timeout: float = 60.0):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(tools, "_adb", side_effect=fake_adb):
        result = tools.app_cloud_back_twice_impl(package_name="com.dzu4.app")

    assert result["ok"] is True
    assert result["action"] == "cloud_back_twice"
    assert "screenshot" in result["forbid_between"]
    assert len(calls) == 1
    assert calls[0][0] == "shell"
    shell_cmd = calls[0][1]
    assert shell_cmd.count("input tap") == 2
    assert "1120" in shell_cmd and "2955" in shell_cmd


def test_app_device_tools_registered() -> None:
    from services.api.app.agents.tools import app_device_tools  # noqa: F401
    from services.api.app.agents.tools.registry import default_registry

    names = {t.name for t in default_registry.list_tools()}
    assert "app_screenshot_tool" in names
    assert "app_cloud_back_twice_tool" in names
    assert "load_app_product_profile_tool" in names


def test_profile_load_case_insensitive_and_key_taps_exclude_popup() -> None:
    from services.api.app.domain.app_product import is_vision_only_tap_key

    profile = load_profile("dark.NVYCy.AmBPu")
    assert profile is not None
    assert profile.package_name == "dark.nvycy.ambpu"
    assert profile.popup_close_policy == "watcher_cascade"
    assert profile.key_taps["me_tab"] == (1297, 2734)
    # 手势带抬高：脚本 2833 → Profile clamp 2794
    assert profile.clamp_tap_y(659, 2833) == (659, 2794)
    assert is_vision_only_tap_key("popup_close_1") is True
    summary = profile.to_agent_summary()
    assert summary["popup_close_policy"] == "watcher_cascade"
    assert summary["grants_absolute_coordinates"] is False
    assert "key_taps" not in summary
    assert "taps" not in summary
    assert "cloud_back_tap" not in summary


def test_popup_hypothesis_uses_watcher_cascade() -> None:
    decision = suggest_next_action(
        package_name="com.xhwj.jmtt",
        page_hypothesis="ad_popup",
    )
    assert decision["ok"] is True
    assert decision["next_action"] == "popup_watcher_cascade"
    assert decision["popup_type"] == "grid_ad_below_x"
    assert decision["popup_close_policy"] == "watcher_cascade"
    assert "product_script_popup_close_tap" in decision["forbid"]
    assert "cloud_back_twice" in decision["cascade"]
    assert "geometry_safe_x" in decision["cascade"]


def test_popup_close_by_type_routes_actions() -> None:
    from services.api.app.domain.app_product import suggest_popup_close_action

    below = suggest_popup_close_action(
        package_name="com.dzu4.app",
        popup_type="grid_ad_below_x",
    )
    assert below["next_action"] == "popup_watcher_cascade"
    assert below["fallback_action"] == "cloud_back_twice"
    hint = below.get("target_hint") or ""
    assert "几何" in hint or "文案" in hint or "Watcher" in hint or "连点" in hint

    skip = suggest_popup_close_action(
        package_name="com.dzu4.app",
        popup_type="startup_skip",
    )
    assert skip["next_action"] == "vision_skip"
    assert skip["control_role"] == "skip"
    assert skip["popup_close_policy"] == "startup_semantic_relative"
    assert "进入" in skip["semantic_markers"]
    assert "跳过" in skip["semantic_markers"]
    assert "关闭" not in skip["semantic_markers"]
    assert skip["relative_region"]["left"] >= 0.65
    assert "cloud_back" in skip["forbid"]
    assert "script_entry_tap" in skip["forbid"]
    assert "popup_close_bottom_x" in skip["forbid"]
    assert "vision_find_x" in skip["forbid"]

    entry = suggest_next_action(
        package_name="dark.nvycy.ambpu",
        page_hypothesis="startup_ad",
    )
    assert entry["next_action"] == "vision_skip"
    assert entry["popup_type"] == "startup_skip"

    membership = suggest_popup_close_action(
        package_name="com.dzu4.app",
        popup_type="membership_intercept",
    )
    assert membership["next_action"] == "popup_watcher_cascade"
    assert membership["fallback_action"] == "cloud_back_twice"
    assert "cloud_back_twice" in membership["cascade"]

    age = suggest_next_action(
        package_name="com.dzu4.app",
        page_hypothesis="home",
        popup_type="age_confirm",
    )
    assert age["next_action"] == "popup_watcher_cascade"
    assert age["fallback_action"] == "cloud_back_twice"

    notice = suggest_popup_close_action(
        package_name="dark.nvycy.ambpu",
        popup_type="官方公告",
    )
    assert notice["popup_type"] == "notice_ack"
    assert notice["next_action"] == "vision_tap"
    assert "我知道了" in notice["semantic_markers"]
    assert notice["popup_close_policy"] == "notice_ack_semantic"


def test_load_profile_tool_summary_excludes_script_dump(monkeypatch) -> None:
    from services.api.app.agents.tools import app_device_tools as tools

    summary = tools.load_app_product_profile_impl("com.dzu4.app")
    assert summary["ok"] is True
    assert summary["popup_close_policy"] == "watcher_cascade"
    assert summary["grants_absolute_coordinates"] is False
    assert "key_taps" not in summary
    assert "taps" not in summary
    assert "agent_loading_rule" in summary

    blocked = tools.app_tap_from_profile_impl("com.dzu4.app", "popup_close_below")
    assert blocked["ok"] is False
    assert blocked["error"] == "popup_close_requires_vision"


def test_orchestrator_packages_have_profiles() -> None:
    packages = set(list_profile_packages())
    for package in (
        "com.dzu4.app",
        "com.hj.ttthree",
        "dark.nvycy.ambpu",
        "com.xhwj.jmtt",
        "top.qkjzg.zfiufm",
        "me.fifeg.dtphkj",
    ):
        assert package in packages
        assert load_profile(package) is not None


def test_popup_cloud_back_not_blocked_by_forbid_startup_phrase() -> None:
    """弹窗 purpose 含「禁止…开屏进入…」时仍须解析为 cloud_back 坐标，不得 keyevent。"""

    from services.api.app.infrastructure.device.android_surface_driver import (
        _resolve_product_cloud_back_tap,
    )

    # 旧文案（曾误伤）：裸「开屏」命中「开屏进入」→ None → keyevent 4
    legacy_purpose = (
        "弹窗广告仍在：点底部系统导航栏最右侧「<」关窗"
        "（cloud_back_tap；禁止找 ×／开屏进入／keyevent 4）"
    )
    assert _resolve_product_cloud_back_tap(
        semantic_target="底部导航栏「<」关闭弹窗",
        purpose=legacy_purpose,
        package_name="dark.NVYCy.AmBPu",
    ) == (1120, 2955)

    # 新文案
    new_purpose = (
        "弹窗广告仍在：点底部系统导航栏最右侧「<」关窗"
        "（cloud_back_tap；禁止找×／禁止当开屏点进入／禁止keyevent4）"
    )
    assert _resolve_product_cloud_back_tap(
        semantic_target="底部导航栏「<」关闭弹窗",
        purpose=new_purpose,
        package_name="dark.NVYCy.AmBPu",
    ) == (1120, 2955)

    # 真开屏仍禁止 cloud_back
    assert (
        _resolve_product_cloud_back_tap(
            semantic_target="开屏进入：进入/跳过",
            purpose="点开屏进入/跳过并采集进入后页面",
            package_name="dark.NVYCy.AmBPu",
        )
        is None
    )

    # 回归：Explorer 误写「外跳浏览器」purpose 时，底栏「<」仍须点坐标，禁止退桌面
    assert _resolve_product_cloud_back_tap(
        semantic_target="底部导航栏「<」关闭弹窗",
        purpose=(
            "关层曾外跳浏览器／外链；确定性系统 BACK 关闭遮挡，"
            "禁止再点广告关闭 X 或落地页「点击进入」。"
        ),
        package_name="dark.NVYCy.AmBPu",
    ) == (1120, 2955)

    # 遮挡弹层文案（无「弹窗」字）+ 底栏 semantic 仍须 cloud_back
    assert _resolve_product_cloud_back_tap(
        semantic_target="底部导航栏「<」关闭弹窗",
        purpose="当前业务页仍有遮挡弹层；先关闭弹层并截图，再巡检底栏与覆盖缺口。",
        package_name="dark.NVYCy.AmBPu",
    ) == (1120, 2955)


def test_tap_cloud_back_twice_prefers_ops_tap() -> None:
    """cascade D 档连点两下「<」走 ops.tap×2。"""

    from services.api.app.infrastructure.device.android_surface_driver import (
        _tap_cloud_back_twice_on_ops,
    )

    taps: list[tuple[int, int]] = []

    class _Ops:
        def tap(self, x: int, y: int) -> None:
            taps.append((x, y))

    _tap_cloud_back_twice_on_ops(_Ops(), 1120, 2955)
    assert taps == [(1120, 2955), (1120, 2955)]


def test_all_profiles_hard_split_startup_vs_popup() -> None:
    """全部 APP 产品：开屏=进入/跳过；弹窗=watcher cascade（D=twice）。"""
    from services.api.app.domain.app_product import (
        STARTUP_SEMANTIC_MARKERS,
        suggest_popup_close_action,
    )

    for package in list_profile_packages():
        profile = load_profile(package)
        assert profile is not None
        assert profile.popup_close_policy == "watcher_cascade"
        popup = suggest_popup_close_action(
            package_name=package,
            popup_type="ad_popup",
        )
        assert popup["next_action"] == "popup_watcher_cascade"
        assert popup["fallback_action"] == "cloud_back_twice"
        assert "cloud_back_twice" in popup["cascade"]
        startup = suggest_popup_close_action(
            package_name=package,
            popup_type="startup_ad",
        )
        assert startup["next_action"] == "vision_skip"
        assert startup["popup_close_policy"] == "startup_semantic_relative"
        assert set(STARTUP_SEMANTIC_MARKERS).issubset(set(startup["semantic_markers"]))
        assert "关闭" not in startup["semantic_markers"]
        assert "cloud_back" in startup["forbid"]
        assert "popup_close_bottom_x" in startup["forbid"]
