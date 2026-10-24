"""海角乱伦（hjll）业务侧可锁定的探索覆盖范围。

对应 docs/app_qc_operator_package_scopes.md；提交 /api/v2/tasks 时写入
target.exploration_target，避免用目录全量当分母。

页型分母不含 profile／设置／支付；底栏硬覆盖仍含「我的」只读截图。
"""

from __future__ import annotations

# 历史包名（3.1.x）；3.2.x／3.3.x 马甲包见 HJLL_PACKAGE_NAMES
HJLL_PACKAGE_NAME = "app.p6228f.pa3"

HJLL_PACKAGE_NAMES: tuple[str, ...] = (
    "app.p6228f.pa3",
    "app.p12b04.p51",
    # hjll3.3.0.apk 实测安装包（云手机 20660）
    "dark.wwcskrpojs.ebjhyebjbl",
)

# 本包真实存在且本轮允许探索的业务页（分母不含我的／设置／支付／发布）
HJLL_REQUIRED_PAGE_TYPES: tuple[str, ...] = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "post_list",
)

HJLL_MINIMUM_BUSINESS_PAGES = 6

HJLL_EXCLUDED_FROM_DENOMINATOR: tuple[str, ...] = (
    "profile",
    "settings",
    "payment",
    "post_publish",
    "activity",
)


def hjll_exploration_target() -> dict[str, object]:
    """供 V2 Task payload 的 target.exploration_target 使用。"""

    return {
        "required_page_types": list(HJLL_REQUIRED_PAGE_TYPES),
        "minimum_business_pages": HJLL_MINIMUM_BUSINESS_PAGES,
        "device_capability_template": "android_visual_readonly_v1",
    }


def hjll_task_remark(*, profile: str, apk_name: str) -> str:
    """统一任务备注口径，便于报告解读。"""

    excluded = "/".join(HJLL_EXCLUDED_FROM_DENOMINATOR)
    return (
        f"MoreLogin {profile}/CP-2; APK={apk_name}; "
        f"coverage_scope=hjll_6_types; exclude_denominator={excluded}; "
        f"hard_coverage=6/6; bottom_tabs_include_profile_readonly=我的只读截图"
    )
