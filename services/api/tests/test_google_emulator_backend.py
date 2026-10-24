"""裝置後端與 Google Emulator 解析單元測試。"""

import subprocess
from pathlib import Path

from services.api.app.infrastructure.device.backend import (
    BACKEND_CLOUD_PHONE,
    BACKEND_GOOGLE_EMULATOR,
    BACKEND_LDPLAYER,
    BACKEND_REDROID,
    demo_backend_options,
    is_remote_adb_backend,
    resolve_device_backend,
    set_device_backend,
)
from services.api.app.infrastructure.device.google_emulator import (
    GoogleEmulatorController,
    resolve_adb_executable,
    resolve_android_home,
    resolve_avd_name,
)
from services.api.app.infrastructure.device.ldplayer import AppVersionInfo


def _label_controller(tmp_path: Path, monkeypatch) -> GoogleEmulatorController:
    """建立不連接真實裝置的 Google Emulator controller。"""

    sdk = tmp_path / "sdk"
    platform_tools = sdk / "platform-tools"
    platform_tools.mkdir(parents=True)
    (platform_tools / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.delenv("ALIOTH_ADB_EXECUTABLE", raising=False)
    emu = GoogleEmulatorController()
    emu._serial = "emulator-5554"
    monkeypatch.setattr(
        emu,
        "_resolve_installed_apk_path",
        lambda _package_name, **_kwargs: "/data/app/com.example/base.apk",
    )
    return emu


def test_resolve_device_backend_explicit(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_BACKEND", "google_emulator")
    assert resolve_device_backend() == BACKEND_GOOGLE_EMULATOR
    monkeypatch.setenv("ALIOTH_DEVICE_BACKEND", "ldplayer")
    assert resolve_device_backend() == BACKEND_LDPLAYER
    monkeypatch.setenv("ALIOTH_DEVICE_BACKEND", "avd")
    assert resolve_device_backend() == BACKEND_GOOGLE_EMULATOR
    monkeypatch.setenv("ALIOTH_DEVICE_BACKEND", "redroid")
    assert resolve_device_backend() == BACKEND_REDROID
    monkeypatch.setenv("ALIOTH_DEVICE_BACKEND", "cloud_phone")
    assert resolve_device_backend() == BACKEND_CLOUD_PHONE
    monkeypatch.setenv("ALIOTH_DEVICE_BACKEND", "cloud-phone")
    assert resolve_device_backend() == BACKEND_CLOUD_PHONE
    assert is_remote_adb_backend(BACKEND_CLOUD_PHONE)
    assert is_remote_adb_backend(BACKEND_REDROID)
    assert not is_remote_adb_backend(BACKEND_LDPLAYER)


def test_resolve_device_backend_default_linux(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_DEVICE_BACKEND", raising=False)
    monkeypatch.setattr("services.api.app.infrastructure.device.backend.os.name", "posix")
    assert resolve_device_backend() == BACKEND_GOOGLE_EMULATOR


def test_set_device_backend_demo_modes(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_EMULATOR_BACKEND", "redroid")
    assert set_device_backend("cloud_phone") == BACKEND_CLOUD_PHONE
    assert resolve_device_backend() == BACKEND_CLOUD_PHONE
    assert set_device_backend("emulator") == BACKEND_REDROID
    assert resolve_device_backend() == BACKEND_REDROID
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_EMULATOR_BACKEND", "google_emulator")
    assert set_device_backend("模擬器") == BACKEND_GOOGLE_EMULATOR


def test_demo_backend_options(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_EMULATOR_BACKEND", "redroid")
    opts = demo_backend_options()
    assert opts[0]["mode"] == "cloud_phone"
    assert opts[1]["mode"] == "emulator"
    assert opts[1]["value"] == BACKEND_REDROID


def test_resolve_avd_name(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_AVD_NAME", raising=False)
    assert resolve_avd_name() == "Alioth_API30"
    monkeypatch.setenv("ALIOTH_AVD_NAME", "MyAVD")
    assert resolve_avd_name() == "MyAVD"


def test_resolve_adb_from_android_home(tmp_path: Path, monkeypatch) -> None:
    sdk = tmp_path / "sdk"
    pt = sdk / "platform-tools"
    pt.mkdir(parents=True)
    adb = pt / "adb"
    adb.write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.delenv("ALIOTH_ADB_EXECUTABLE", raising=False)
    assert resolve_adb_executable() == adb.resolve()


def test_resolve_android_home(tmp_path: Path, monkeypatch) -> None:
    sdk = tmp_path / "android-sdk"
    sdk.mkdir()
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    assert resolve_android_home() == sdk.resolve()


def test_airtest_uri(monkeypatch, tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    pt = sdk / "platform-tools"
    pt.mkdir(parents=True)
    (pt / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.delenv("ALIOTH_MOBILE_DEMO_AIRTEST_DEVICE", raising=False)
    emu = GoogleEmulatorController()
    emu._serial = "emulator-5554"
    assert emu.airtest_device_uri() == "Android:///emulator-5554"


def test_pick_online_serial_skips_ldplayer_spoof(tmp_path: Path, monkeypatch) -> None:
    sdk = tmp_path / "sdk"
    pt = sdk / "platform-tools"
    pt.mkdir(parents=True)
    (pt / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.delenv("ALIOTH_ADB_SERIAL", raising=False)
    emu = GoogleEmulatorController()
    monkeypatch.setattr(
        emu,
        "list_adb_devices",
        lambda **_k: [("emulator-5554", "device"), ("emulator-5556", "device")],
    )

    def _prop(serial: str, prop: str, **_k) -> str:
        if serial == "emulator-5554":
            return {
                "ro.product.model": "ASUS AI2201_B",
                "ro.product.manufacturer": "ROG",
                "ro.product.name": "AI2201_B",
                "ro.build.fingerprint": "Android/aosp_marlin/marlin:9",
                "ro.kernel.qemu": "1",
                "ro.hardware": "marlin",
            }.get(prop, "")
        return {
            "ro.product.model": "sdk_gphone64_x86_64",
            "ro.product.manufacturer": "Google",
            "ro.product.name": "sdk_gphone64_x86_64",
            "ro.build.fingerprint": "google/sdk_gphone64_x86_64",
            "ro.kernel.qemu": "1",
            "ro.hardware": "ranchu",
        }.get(prop, "")

    monkeypatch.setattr(emu, "_device_prop", _prop)
    assert emu._pick_online_serial() == "emulator-5556"


def test_pick_online_serial_none_when_only_ldplayer(tmp_path: Path, monkeypatch) -> None:
    sdk = tmp_path / "sdk"
    pt = sdk / "platform-tools"
    pt.mkdir(parents=True)
    (pt / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.delenv("ALIOTH_ADB_SERIAL", raising=False)
    emu = GoogleEmulatorController()
    monkeypatch.setattr(emu, "list_adb_devices", lambda **_k: [("emulator-5554", "device")])
    monkeypatch.setattr(
        emu,
        "_device_prop",
        lambda serial, prop, **_k: {
            "ro.product.model": "ASUS AI2201_B",
            "ro.product.manufacturer": "ROG",
            "ro.product.name": "AI2201",
            "ro.build.fingerprint": "ldmnq",
            "ro.kernel.qemu": "1",
            "ro.hardware": "marlin",
        }.get(prop, ""),
    )
    assert emu._pick_online_serial() is None


def test_emulator_serials_online_first(tmp_path: Path, monkeypatch) -> None:
    sdk = tmp_path / "sdk"
    pt = sdk / "platform-tools"
    pt.mkdir(parents=True)
    (pt / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    emu = GoogleEmulatorController()
    ordered = emu._emulator_serials_ordered(
        [
            ("emulator-5556", "offline"),
            ("emulator-5554", "device"),
            ("RZ8N", "device"),
        ]
    )
    assert ordered == ["emulator-5554", "emulator-5556"]


def test_ensure_apk_installed_recovers_real_package_when_manifest_guess_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """安装后若 Manifest 兜底包名不存在，应返回设备上的真实新增包名。"""

    sdk = tmp_path / "sdk"
    platform_tools = sdk / "platform-tools"
    platform_tools.mkdir(parents=True)
    (platform_tools / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"apk")
    emu = GoogleEmulatorController()
    emu._serial = "127.0.0.1:5555"
    installed = False
    actual_package = "com.flutter324.cblmh.dq04ke"

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.google_emulator.try_extract_package_name_from_aapt",
        lambda *_args, **_kwargs: "com.qiku.id",
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.google_emulator.try_extract_apk_version_from_aapt",
        lambda *_args, **_kwargs: AppVersionInfo(),
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.google_emulator.try_extract_application_label_from_aapt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        emu,
        "is_package_installed",
        lambda package_name, **_kwargs: installed and package_name == actual_package,
    )
    monkeypatch.setattr(emu, "get_installed_app_version", lambda *_args, **_kwargs: AppVersionInfo())
    monkeypatch.setattr(
        emu,
        "list_user_installed_packages",
        lambda **_kwargs: ["com.example.existing", actual_package]
        if installed
        else ["com.example.existing"],
    )

    def install_apk(*_args, **_kwargs) -> str:
        nonlocal installed
        installed = True
        return "Success"

    monkeypatch.setattr(emu, "install_apk", install_apk)

    logs, package_name, installed_now = emu.ensure_apk_installed(apk)

    assert installed_now is True
    assert package_name == actual_package
    assert "com.qiku.id" in logs
    assert actual_package in logs


def test_ensure_apk_installed_recovers_reinstalled_package_by_apk_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """错误候选包不存在且真实包已预装时，应以 APK 大小恢复真实包名。"""

    sdk = tmp_path / "sdk"
    platform_tools = sdk / "platform-tools"
    platform_tools.mkdir(parents=True)
    (platform_tools / "adb").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"apk")
    emu = GoogleEmulatorController()
    emu._serial = "127.0.0.1:5555"
    actual_package = "com.flutter324.cblmh.dq04ke"

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.google_emulator.try_extract_package_name_from_aapt",
        lambda *_args, **_kwargs: "com.qiku.id",
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.google_emulator.try_extract_apk_version_from_aapt",
        lambda *_args, **_kwargs: AppVersionInfo(),
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.google_emulator.try_extract_application_label_from_aapt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(emu, "is_package_installed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(emu, "get_installed_app_version", lambda *_args, **_kwargs: AppVersionInfo())
    monkeypatch.setattr(
        emu,
        "list_user_installed_packages",
        lambda **_kwargs: ["com.example.existing", actual_package],
    )
    monkeypatch.setattr(emu, "install_apk", lambda *_args, **_kwargs: "Success")
    monkeypatch.setattr(emu, "_package_last_update_time", lambda *_args, **_kwargs: "unchanged")
    monkeypatch.setattr(
        emu,
        "_package_apk_size_bytes",
        lambda package_name, **_kwargs: 3 if package_name == actual_package else 100,
    )

    _logs, package_name, installed_now = emu.ensure_apk_installed(apk)

    assert installed_now is True
    assert package_name == actual_package


def test_google_emulator_application_label_uses_metadata_without_full_apk_pull(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """轻量元数据能解析应用名时，控制器不应再拉取完整 APK。"""

    emu = _label_controller(tmp_path, monkeypatch)
    metadata_calls: list[tuple[str, Path]] = []
    adb_calls: list[list[str]] = []
    aapt_inputs: list[tuple[bytes, bool]] = []

    def download_metadata(*, remote_apk_path: str, local_apk_path: Path, run_adb) -> bool:
        metadata_calls.append((remote_apk_path, local_apk_path))
        local_apk_path.write_bytes(b"metadata-apk")
        return True

    def run_aapt(
        path: Path,
        *,
        ldplayer_home: Path | None = None,
        allow_label_only_result: bool = False,
    ) -> str:
        assert ldplayer_home is None
        assert allow_label_only_result is True
        aapt_inputs.append((path.read_bytes(), allow_label_only_result))
        return "package: name='com.example' application-label:'Metadata Label'"

    def run_adb(args: list[str], **_kwargs):
        adb_calls.append(args)
        raise AssertionError("輕量成功時不應執行完整 APK pull")

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.installed_apk_metadata.download_installed_apk_metadata",
        download_metadata,
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.ldplayer._run_aapt_dump_badging",
        run_aapt,
    )
    monkeypatch.setattr(emu, "_run_adb", run_adb)

    assert emu.get_package_application_label("com.example") == "Metadata Label"
    assert len(metadata_calls) == 1
    assert metadata_calls[0][0] == "/data/app/com.example/base.apk"
    assert aapt_inputs == [(b"metadata-apk", True)]
    assert adb_calls == []


def test_google_emulator_application_label_falls_back_to_full_apk_when_metadata_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """轻量元数据下载失败时，应回退完整 APK 并返回真实应用名。"""

    emu = _label_controller(tmp_path, monkeypatch)
    metadata_calls = 0
    pull_calls: list[list[str]] = []
    aapt_inputs: list[tuple[bytes, bool]] = []

    def download_metadata(*, remote_apk_path: str, local_apk_path: Path, run_adb) -> bool:
        nonlocal metadata_calls
        metadata_calls += 1
        return False

    def run_aapt(
        path: Path,
        *,
        ldplayer_home: Path | None = None,
        allow_label_only_result: bool = False,
    ) -> str:
        assert ldplayer_home is None
        assert allow_label_only_result is False
        aapt_inputs.append((path.read_bytes(), allow_label_only_result))
        return "package: name='com.example' application-label:'Full APK Label'"

    def run_adb(args: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        pull_calls.append(args)
        assert args[0] == "pull"
        Path(args[-1]).write_bytes(b"full-apk")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.installed_apk_metadata.download_installed_apk_metadata",
        download_metadata,
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.ldplayer._run_aapt_dump_badging",
        run_aapt,
    )
    monkeypatch.setattr(emu, "_run_adb", run_adb)

    assert emu.get_package_application_label("com.example") == "Full APK Label"
    assert metadata_calls == 1
    assert len(pull_calls) == 1
    assert pull_calls[0][:2] == ["pull", "/data/app/com.example/base.apk"]
    assert aapt_inputs == [(b"full-apk", False)]


def test_google_emulator_application_label_falls_back_when_metadata_has_no_label(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """轻量 APK 没有可解析标签时，应回退并返回完整 APK 的标签。"""

    emu = _label_controller(tmp_path, monkeypatch)
    pull_calls: list[list[str]] = []
    aapt_inputs: list[tuple[bytes, bool]] = []

    def download_metadata(*, remote_apk_path: str, local_apk_path: Path, run_adb) -> bool:
        local_apk_path.write_bytes(b"metadata-without-label")
        return True

    def run_aapt(
        path: Path,
        *,
        ldplayer_home: Path | None = None,
        allow_label_only_result: bool = False,
    ) -> str:
        assert ldplayer_home is None
        content = path.read_bytes()
        aapt_inputs.append((content, allow_label_only_result))
        if content == b"metadata-without-label":
            assert allow_label_only_result is True
            return "package: name='com.example'"
        assert allow_label_only_result is False
        return "package: name='com.example' application-label:'Full APK Label'"

    def run_adb(args: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        pull_calls.append(args)
        assert args[0] == "pull"
        Path(args[-1]).write_bytes(b"full-apk")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.installed_apk_metadata.download_installed_apk_metadata",
        download_metadata,
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.ldplayer._run_aapt_dump_badging",
        run_aapt,
    )
    monkeypatch.setattr(emu, "_run_adb", run_adb)

    assert emu.get_package_application_label("com.example") == "Full APK Label"
    assert len(pull_calls) == 1
    assert aapt_inputs == [(b"metadata-without-label", True), (b"full-apk", False)]


def test_foreground_evidence_prefers_resumed_over_stale_focused_app() -> None:
    from services.api.app.infrastructure.device.google_emulator import (
        package_in_foreground_evidence,
        parse_foreground_evidence_lines,
    )

    window = """
  mCurrentFocus=null
  mFocusedApp=AppWindowToken{7ebe2b0 token=Token{6cfa5f3 ActivityRecord{553062 u0 dark.xBHhi.rZKwQ/com.xvproject.MainActivity t7}}}
    mFocusedApp=Token{403d028 ActivityRecord{ef57e4b u0 me.mfanv.npymci/com.jycg.icg.MainActivity t12}}
"""
    activity = """
  ResumedActivity: ActivityRecord{ef57e4b u0 me.mfanv.npymci/com.jycg.icg.MainActivity t12}
"""
    evidence = parse_foreground_evidence_lines(window_dump=window, activity_dump=activity)
    assert evidence[0].startswith("ResumedActivity:")
    assert package_in_foreground_evidence("me.mfanv.npymci", evidence)
    # 舊邏輯只取 window 第一條 mFocusedApp 會誤判成 dark.xBHhi
    stale_first = next(
        ln.strip()
        for ln in window.splitlines()
        if "mFocusedApp=" in ln
    )
    assert "dark.xBHhi.rZKwQ" in stale_first
    assert "me.mfanv.npymci" not in stale_first
