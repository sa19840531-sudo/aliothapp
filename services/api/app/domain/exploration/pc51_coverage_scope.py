"""51品茶（51pc / vip.dibwo.txbazn）业务侧可锁定的探索覆盖范围。

对应 docs/app_qc_operator_package_scopes.md；提交 /api/v2/tasks 时写入
target.exploration_target，避免用目录全量当分母。

依据 APK 文案：首页／茶馆／视频；本轮不测登录后个人中心与支付。
"""

from __future__ import annotations

PC51_PACKAGE_NAME = "vip.dibwo.txbazn"

# 本包真实存在且本轮允许探索的业务页（不含我的／设置／支付／发布／社区）
PC51_REQUIRED_PAGE_TYPES: tuple[str, ...] = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
)

PC51_MINIMUM_BUSINESS_PAGES = 5

PC51_EXCLUDED_FROM_DENOMINATOR: tuple[str, ...] = (
    "profile",
    "settings",
    "payment",
    "post_publish",
    "post_list",
    "activity",
)


def pc51_exploration_target() -> dict[str, object]:
    """供 V2 Task payload 的 target.exploration_target 使用。"""

    return {
        "required_page_types": list(PC51_REQUIRED_PAGE_TYPES),
        "minimum_business_pages": PC51_MINIMUM_BUSINESS_PAGES,
        "device_capability_template": "android_visual_readonly_v1",
    }


def pc51_task_remark(*, profile: str, apk_name: str) -> str:
    """统一任务备注口径，便于报告解读。"""

    excluded = "/".join(PC51_EXCLUDED_FROM_DENOMINATOR)
    return (
        f"MoreLogin {profile}/CP-2; APK={apk_name}; "
        f"coverage_scope=pc51_5_types; exclude_denominator={excluded}; "
        f"hard_coverage=5/5; no_test_account=本轮不测登录后／我的／充值"
    )
