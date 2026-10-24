"""通用成人内容五页分母（91PORN／萝莉岛等未单独拆栏的包）。"""

from __future__ import annotations

GENERIC5_REQUIRED_PAGE_TYPES: tuple[str, ...] = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
)

GENERIC5_MINIMUM_BUSINESS_PAGES = 5

GENERIC5_EXCLUDED_FROM_DENOMINATOR: tuple[str, ...] = (
    "profile",
    "settings",
    "payment",
    "post_publish",
    "post_list",
    "activity",
)

# 已登记走 generic5 的包名
GENERIC5_PACKAGE_NAMES: frozenset[str] = frozenset(
    {
        "app.paf4c0.pede1e12de8c2f952a9aa",  # 91PORN
        "app.p8b938.p1084bf711f98",  # 萝莉岛2
    }
)


def generic5_exploration_target() -> dict[str, object]:
    return {
        "required_page_types": list(GENERIC5_REQUIRED_PAGE_TYPES),
        "minimum_business_pages": GENERIC5_MINIMUM_BUSINESS_PAGES,
        "device_capability_template": "android_visual_readonly_v1",
    }


def generic5_task_remark(*, profile: str, apk_name: str, label: str = "") -> str:
    excluded = "/".join(GENERIC5_EXCLUDED_FROM_DENOMINATOR)
    tag = f"; label={label}" if label else ""
    return (
        f"MoreLogin {profile}/CP-2; APK={apk_name}; "
        f"coverage_scope=generic5; exclude_denominator={excluded}; "
        f"hard_coverage=5/5; no_test_account=本轮不测登录后／我的／充值{tag}"
    )
