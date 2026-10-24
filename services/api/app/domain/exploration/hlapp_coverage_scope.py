"""黑料不打烊（hlapp）业务侧可锁定的探索覆盖范围。

对应 docs/app_qc_operator_package_scopes.md；提交 /api/v2/tasks 时写入
target.exploration_target，避免用目录全量 11 类当分母。
"""

from __future__ import annotations

# F:\apptest\黑料不打烊app\hlapp_1.8.9_260929_1.apk
HLAPP_PACKAGE_NAME = "top.qkjzg.zfiufm"

# 旧马甲仍走同一课纲；当前安装包以 HLAPP_PACKAGE_NAME 为准。
HLAPP_PACKAGE_NAMES: tuple[str, ...] = (
    HLAPP_PACKAGE_NAME,
    "com.edxuf.euloop",
    "uk.frtju.nnxsaf",
)

# 本包真实存在且本轮允许探索的业务页（不含我的／设置／支付／发布）
HLAPP_REQUIRED_PAGE_TYPES: tuple[str, ...] = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "post_list",
    "activity",
)

HLAPP_MINIMUM_BUSINESS_PAGES = 7

HLAPP_EXCLUDED_FROM_DENOMINATOR: tuple[str, ...] = (
    "profile",
    "settings",
    "payment",
    "post_publish",
)


def hlapp_exploration_target() -> dict[str, object]:
    """供 V2 Task payload 的 target.exploration_target 使用。"""

    return {
        "required_page_types": list(HLAPP_REQUIRED_PAGE_TYPES),
        "minimum_business_pages": HLAPP_MINIMUM_BUSINESS_PAGES,
        "device_capability_template": "android_visual_readonly_v1",
        # 与 hlapp playbook 对齐：给足详情返回后重巡底栏的回合。
        "max_actions": 240,
    }


def hlapp_task_remark(*, profile: str, apk_name: str, script_path: str) -> str:
    """统一任务备注口径，便于报告解读。"""

    excluded = "/".join(HLAPP_EXCLUDED_FROM_DENOMINATOR)
    return (
        f"MoreLogin {profile}/CP-2; APK={apk_name}; "
        f"script_hint=untitled1.py; age_confirm=我年满18岁-进入; "
        f"coverage_scope=hlapp_7_types; exclude_denominator={excluded}; "
        f"hard_coverage=7/7; no_test_account=本轮不测登录后; path={script_path}"
    )
