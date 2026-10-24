"""外部 Activity 包分类与包边界共用规则。"""

from services.api.app.application.app_package_boundary import (
    ExternalActivityKind,
    classify_external_activity,
    is_android_launcher_package,
)


def test_classify_browser_store_payment_launcher_and_other() -> None:
    assert (
        classify_external_activity(package_name="com.android.chrome").value
        == ExternalActivityKind.BROWSER
    )
    assert (
        classify_external_activity(package_name="com.android.vending").value
        == ExternalActivityKind.STORE
    )
    assert (
        classify_external_activity(package_name="com.eg.android.AlipayGphone").value
        == ExternalActivityKind.PAYMENT
    )
    assert (
        classify_external_activity(package_name="com.tencent.mm").value
        == ExternalActivityKind.PAYMENT
    )
    assert (
        classify_external_activity(package_name="com.android.settings").value
        == ExternalActivityKind.SETTINGS
    )
    assert (
        classify_external_activity(package_name="com.android.launcher3").value
        == ExternalActivityKind.LAUNCHER
    )
    assert (
        classify_external_activity(package_name="com.unknown.vendor.app").value
        == ExternalActivityKind.OTHER
    )


def test_launcher_heuristic_covers_oem_home() -> None:
    assert is_android_launcher_package(
        package_name="com.sec.android.app.launcher",
        activity=".HomeActivity",
    )
    assert is_android_launcher_package(
        package_name="com.custom.launcher",
        activity=".LauncherActivity",
    )
    assert not is_android_launcher_package(
        package_name="com.android.chrome",
        activity="com.google.android.apps.chrome.Main",
    )


def test_marker_based_classification_for_unknown_vendor_browser() -> None:
    assert (
        classify_external_activity(
            package_name="com.vendor.fastbrowser",
            activity=".MainActivity",
        ).value
        == ExternalActivityKind.BROWSER
    )
