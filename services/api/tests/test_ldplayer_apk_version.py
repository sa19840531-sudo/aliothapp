"""LDPlayer APK 版本比較單元測試。"""

import subprocess
from pathlib import Path

import pytest

from services.api.app.infrastructure.device.ldplayer import (
    AppVersionInfo,
    LdPlayerError,
    _run_aapt_dump_badging,
    compare_app_versions,
    compare_version_names,
    evaluate_apk_install_plan,
    labels_match,
    parse_application_label_from_aapt_badging,
    parse_version_from_aapt_badging,
    parse_version_from_dumpsys_package,
    should_install_apk_update,
)


def test_parse_version_from_aapt_badging() -> None:
    badging = "package: name='com.example.app' versionCode='134' versionName='1.3.4'"
    info = parse_version_from_aapt_badging(badging)
    assert info.version_code == 134
    assert info.version_name == "1.3.4"


def test_parse_version_from_dumpsys_package() -> None:
    output = """
    versionCode=134 minSdk=21 targetSdk=33
    versionName=1.3.4
    """
    info = parse_version_from_dumpsys_package(output)
    assert info.version_code == 134
    assert info.version_name == "1.3.4"


def test_compare_version_names() -> None:
    assert compare_version_names("1.3.10", "1.3.4") == 1
    assert compare_version_names("1.3.4", "1.3.4") == 0
    assert compare_version_names("1.2.9", "1.3.0") == -1


def test_should_install_apk_update_by_version_code() -> None:
    uploaded = AppVersionInfo(version_code=200, version_name="2.0.0")
    installed = AppVersionInfo(version_code=134, version_name="1.3.4")
    need, reason = should_install_apk_update(uploaded, installed)
    assert need is True
    assert "較新" in reason

    uploaded_same = AppVersionInfo(version_code=134, version_name="1.3.4")
    need_same, _ = should_install_apk_update(uploaded_same, installed)
    assert need_same is False

    uploaded_old = AppVersionInfo(version_code=100, version_name="1.0.0")
    need_old, reason_old = should_install_apk_update(uploaded_old, installed)
    assert need_old is False
    assert "較舊" in reason_old


def test_compare_app_versions_fallback_to_name() -> None:
    uploaded = AppVersionInfo(version_name="1.4.0")
    installed = AppVersionInfo(version_name="1.3.4")
    assert compare_app_versions(uploaded, installed) == 1


def test_normalize_adb_serial_rejects_error_text() -> None:
    from services.api.app.infrastructure.device.ldplayer import normalize_adb_serial

    assert normalize_adb_serial("emulator-5554") == "emulator-5554"
    assert normalize_adb_serial("error: device 'emulator-5554' not found") is None
    assert normalize_adb_serial("127.0.0.1:5555") == "127.0.0.1:5555"
    # Docker 服務名（ReDroid compose）
    assert normalize_adb_serial("alioth-redroid:5555") == "alioth-redroid:5555"


def test_parse_application_label_from_aapt_badging() -> None:
    badging = "application-label:'測試App'\napplication-label-zh-CN:'測試App'"
    assert parse_application_label_from_aapt_badging(badging) == "測試App"


def _stub_nonzero_aapt(monkeypatch, stdout: str) -> None:
    """讓 aapt 候選固定為可攔截的命令，模擬退出碼非零。"""

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.ldplayer._resolve_aapt_candidates",
        lambda **_kwargs: ["aapt"],
    )

    def run_aapt(*args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args[0] if args else [],
            1,
            stdout=stdout,
            stderr="aapt: missing icon resource",
        )

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.ldplayer.subprocess.run",
        run_aapt,
    )


def test_run_aapt_dump_badging_allows_label_only_nonzero_for_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """元数据缺少 icon 时，允许轻量路径保留已解析的应用名。"""

    badging = "package: name='com.google.android.apps.docs' application-label:'Drive'"
    _stub_nonzero_aapt(monkeypatch, badging)

    result = _run_aapt_dump_badging(
        tmp_path / "metadata.apk",
        allow_label_only_result=True,
    )

    assert result == badging
    assert parse_application_label_from_aapt_badging(result or "") == "Drive"


def test_run_aapt_dump_badging_keeps_strict_exit_code_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """默认全包解析仍要求 aapt 正常退出。"""

    _stub_nonzero_aapt(
        monkeypatch,
        "package: name='com.google.android.apps.docs' application-label:'Drive'",
    )

    assert _run_aapt_dump_badging(tmp_path / "full.apk") is None


def test_run_aapt_dump_badging_rejects_nonzero_without_application_label(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """非零退出且没有应用名时，即使轻量模式也必须返回 None。"""

    _stub_nonzero_aapt(
        monkeypatch,
        "package: name='com.google.android.apps.docs'\napplication-icon-160:'res/mipmap/icon.png'",
    )

    assert (
        _run_aapt_dump_badging(
            tmp_path / "metadata.apk",
            allow_label_only_result=True,
        )
        is None
    )


def test_evaluate_apk_install_plan_rejects_downgrade_same_package() -> None:
    uploaded = AppVersionInfo(version_code=134, version_name="1.3.4")
    installed = AppVersionInfo(version_code=135, version_name="1.3.5")

    with pytest.raises(LdPlayerError, match="拒絕安裝"):
        evaluate_apk_install_plan(
            apk_pkg="com.example.app",
            apk_version=uploaded,
            apk_label=None,
            is_package_installed=lambda pkg: pkg == "com.example.app",
            get_installed_version=lambda pkg: installed,
            list_user_packages=lambda: [],
            get_package_label=lambda pkg: None,
        )


def test_evaluate_apk_install_plan_rejects_downgrade_same_label_different_package() -> None:
    uploaded = AppVersionInfo(version_code=134, version_name="1.3.4")
    installed = AppVersionInfo(version_code=135, version_name="1.3.5")

    with pytest.raises(LdPlayerError, match="拒絕安裝"):
        evaluate_apk_install_plan(
            apk_pkg="us.nojcm.tkftab",
            apk_version=uploaded,
            apk_label="91測試",
            is_package_installed=lambda pkg: False,
            get_installed_version=lambda pkg: installed,
            list_user_packages=lambda: ["me.mfanv.npymci"],
            get_package_label=lambda pkg: "91測試" if pkg == "me.mfanv.npymci" else None,
        )


def test_evaluate_apk_install_plan_skips_same_version() -> None:
    version = AppVersionInfo(version_code=134, version_name="1.3.4")
    should_install, logs = evaluate_apk_install_plan(
        apk_pkg="com.example.app",
        apk_version=version,
        apk_label=None,
        is_package_installed=lambda pkg: True,
        get_installed_version=lambda pkg: version,
        list_user_packages=lambda: [],
        get_package_label=lambda pkg: None,
    )
    assert should_install is False
    assert any("跳過安裝" in line for line in logs)


def test_evaluate_apk_install_plan_same_package_skips_before_label_scan() -> None:
    version = AppVersionInfo(version_code=134, version_name="1.3.4")

    def _unexpected_label_scan() -> list[str]:
        raise AssertionError("同包同版本不应扫描同 label 包")

    should_install, logs = evaluate_apk_install_plan(
        apk_pkg="com.example.app",
        apk_version=version,
        apk_label="测试 App",
        is_package_installed=lambda pkg: pkg == "com.example.app",
        get_installed_version=lambda pkg: version,
        list_user_packages=_unexpected_label_scan,
        get_package_label=lambda pkg: (_ for _ in ()).throw(
            AssertionError("同包同版本不应读取其他包 label")
        ),
    )

    assert should_install is False
    assert any("跳過安裝" in line for line in logs)


def test_labels_match() -> None:
    assert labels_match(" App ", "App")
    assert not labels_match("A", "")
