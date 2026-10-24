"""App 包边界判定：探索、Frontier 恢复与动作执行共用同一套 location 规则。"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from urllib.parse import urlsplit

from services.api.app.domain.exploration import ExplorationWork
from services.api.app.domain.interaction import SurfaceKind

# 连续回源失败／外跳循环达到阈值后给出明确终态（见 AppExplorationTerminalEvaluator）。
EXTERNAL_RETURN_FAILURE_LIMIT = 2
EXTERNAL_ACTIVITY_BOUNCE_LIMIT = 3


class ExternalActivityKind(StrEnum):
    """目标包之外的前台 Activity 粗分类，用于限制操作与审计文案。"""

    BROWSER = "browser"
    STORE = "store"
    PAYMENT = "payment"
    LAUNCHER = "launcher"
    SETTINGS = "settings"
    OTHER = "other"


_ANDROID_LAUNCHER_PACKAGES = frozenset(
    {
        "com.android.launcher3",
        "com.google.android.apps.nexuslauncher",
        "com.sec.android.app.launcher",
        "com.miui.home",
        "com.huawei.android.launcher",
        "com.hihonor.android.launcher",
        "com.oppo.launcher",
        "com.android.launcher",
    }
)

_BROWSER_PACKAGES = frozenset(
    {
        "com.android.chrome",
        "com.chrome.beta",
        "com.chrome.dev",
        "com.chrome.canary",
        "com.google.android.apps.chrome",
        "com.android.browser",
        "com.sec.android.app.sbrowser",
        "com.huawei.browser",
        "com.mi.globalbrowser",
        "com.opera.browser",
        "com.opera.mini.native",
        "com.microsoft.emmx",
        "org.mozilla.firefox",
        "org.mozilla.firefox_beta",
        "com.brave.browser",
        "com.UCMobile",
        "com.uc.browser.en",
        "com.tencent.mtt",
        "mark.via",
    }
)

_STORE_PACKAGES = frozenset(
    {
        "com.android.vending",
        "com.google.android.finsky",
        "com.sec.android.app.samsungapps",
        "com.huawei.appmarket",
        "com.hihonor.appmarket",
        "com.xiaomi.market",
        "com.miui.supermarket",
        "com.oppo.market",
        "com.heytap.market",
        "com.bbk.appstore",
        "com.vivo.appstore",
        "com.tencent.android.qqdownloader",
        "com.qihoo.appstore",
        "com.baidu.appsearch",
    }
)

_PAYMENT_PACKAGES = frozenset(
    {
        "com.eg.android.AlipayGphone",
        "com.eg.android.AlipayGphoneRC",
        "com.tencent.mm",
        "com.tencent.mobileqq",
        "com.unionpay",
        "com.chinamworld.main",
        "cmb.pb",
        "com.icbc",
        "com.android.bankabc",
        "com.chinamworld.bocmbci",
        "com.paypal.android.p2pmobile",
        "com.google.android.apps.walletnfcrel",
        "com.samsung.android.spay",
        "com.huawei.wallet",
        "com.miui.hybrid",
    }
)

_SETTINGS_PACKAGES = frozenset(
    {
        "com.android.settings",
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
        "com.android.packageinstaller",
        "com.google.android.packageinstaller",
        "com.samsung.android.packageinstaller",
        "com.miui.securitycenter",
        "com.huawei.systemmanager",
    }
)

_BROWSER_MARKERS = ("browser", "chrome", "firefox", "webview", "ucmobile", "opera")
_STORE_MARKERS = ("market", "appstore", "vending", "play.store", "appmarket")
_PAYMENT_MARKERS = ("alipay", "wallet", "pay", "bank", "unionpay")
_SETTINGS_MARKERS = ("settings", "permission", "packageinstaller", "securitycenter")


def app_location_within_package_boundary(
    *,
    location: str,
    surface_kind: SurfaceKind,
    allowed_hosts: Sequence[str],
) -> bool:
    """Android／iOS 的 app://package/... 必须落在 allowed_hosts 内。"""

    if surface_kind is SurfaceKind.WEB:
        return True
    allowed = {item.casefold() for item in allowed_hosts if item and item.strip()}
    if not allowed:
        return False
    parsed = urlsplit(location)
    package_name = (parsed.hostname or "").casefold()
    return bool(package_name) and package_name in allowed


def work_within_app_package_boundary(work: ExplorationWork) -> bool:
    """当前 Work 观察 location 是否仍在目标 App 包边界内。"""

    return app_location_within_package_boundary(
        location=work.frontier.observation.location,
        surface_kind=work.surface_kind,
        allowed_hosts=work.boundaries.allowed_hosts,
    )


def package_name_from_app_location(location: str) -> str:
    """从 app://package/... location 取出包名；无法解析时返回空串。"""

    parsed = urlsplit(location)
    return (parsed.hostname or "").strip().casefold()


def is_android_launcher_package(*, package_name: str | None, activity: str | None = None) -> bool:
    """系统桌面／Launcher 识别（用于 BACK 无效时改 warm_restore）。"""

    package = str(package_name or "").strip().casefold()
    activity_name = str(activity or "").strip().casefold()
    if not package:
        return False
    if package in _ANDROID_LAUNCHER_PACKAGES:
        return True
    if "launcher" in package and (
        "launcher" in activity_name
        or activity_name.endswith(".homeactivity")
        or not activity_name
    ):
        return True
    return package.startswith("com.android.") and "launcher" in package


def classify_external_activity(
    *,
    package_name: str | None,
    activity: str | None = None,
) -> ExternalActivityKind:
    """对目标包外的前台包做粗分类；未知包归 other，不得当作目标业务页。"""

    package = str(package_name or "").strip().casefold()
    activity_name = str(activity or "").strip().casefold()
    if not package:
        return ExternalActivityKind.OTHER
    if is_android_launcher_package(package_name=package, activity=activity_name):
        return ExternalActivityKind.LAUNCHER
    if package in _BROWSER_PACKAGES or _marker_hit(package, activity_name, _BROWSER_MARKERS):
        return ExternalActivityKind.BROWSER
    if package in _STORE_PACKAGES or _marker_hit(package, activity_name, _STORE_MARKERS):
        return ExternalActivityKind.STORE
    if package in _PAYMENT_PACKAGES or _marker_hit(package, activity_name, _PAYMENT_MARKERS):
        return ExternalActivityKind.PAYMENT
    if package in _SETTINGS_PACKAGES or _marker_hit(package, activity_name, _SETTINGS_MARKERS):
        return ExternalActivityKind.SETTINGS
    return ExternalActivityKind.OTHER


def external_activity_allows_only_recovery(kind: ExternalActivityKind) -> bool:
    """外部 Activity 一律只允许 BACK／WARM_RESTORE 恢复，禁止业务点击。"""

    del kind
    return True


def _marker_hit(package: str, activity: str, markers: Sequence[str]) -> bool:
    haystack = f"{package} {activity}"
    return any(marker in haystack for marker in markers)
