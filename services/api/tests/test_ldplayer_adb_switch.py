"""雷電 ADB 開關自動恢復單元測試。"""

import json
from pathlib import Path

from services.api.app.infrastructure.device.ldplayer import (
    ADB_DEBUG_LOCAL,
    ADB_DEBUG_OFF,
    LdPlayerController,
)


def _make_controller(tmp_path: Path, *, adb_debug: int) -> LdPlayerController:
    home = tmp_path / "LDPlayer9"
    config_dir = home / "vms" / "config"
    config_dir.mkdir(parents=True)
    (home / "ldconsole.exe").write_text("", encoding="utf-8")
    config = {
        "basicSettings.adbDebug": adb_debug,
        "basicSettings.width": 720,
    }
    (config_dir / "leidian0.config").write_text(
        json.dumps(config, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return LdPlayerController(home=home, index=0)


def test_read_adb_debug_setting(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, adb_debug=ADB_DEBUG_OFF)
    assert controller.read_adb_debug_setting() == ADB_DEBUG_OFF
    assert controller.adb_switch_is_off() is True


def test_write_adb_debug_setting(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, adb_debug=ADB_DEBUG_OFF)
    controller.write_adb_debug_setting(ADB_DEBUG_LOCAL)
    data = json.loads(controller.instance_config_path().read_text(encoding="utf-8"))
    assert data["basicSettings.adbDebug"] == ADB_DEBUG_LOCAL


def test_ensure_adb_debug_enabled_writes_config(tmp_path: Path, monkeypatch) -> None:
    controller = _make_controller(tmp_path, adb_debug=ADB_DEBUG_OFF)
    monkeypatch.setattr(controller, "is_emulator_running", lambda **_: False)
    # 测试桩 ldconsole.exe 不是真实二进制；只验证配置写入，不真跑 subprocess。
    monkeypatch.setattr(
        controller,
        "_run_ldconsole",
        lambda args, **_: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    changed, logs = controller.ensure_adb_debug_enabled(reboot_if_running=True)
    assert changed is True
    assert controller.read_adb_debug_setting() == ADB_DEBUG_LOCAL
    assert any("enabling local adb" in line for line in logs)


def test_ensure_adb_debug_enabled_noop_when_open_and_no_device(tmp_path: Path, monkeypatch) -> None:
    controller = _make_controller(tmp_path, adb_debug=ADB_DEBUG_LOCAL)
    monkeypatch.setattr(controller, "is_emulator_running", lambda **_: False)
    monkeypatch.setattr(controller, "_has_adb_device", lambda **_: False)

    changed, logs = controller.ensure_adb_debug_enabled(reboot_if_running=True)
    assert changed is False
    assert any("already open in config" in line for line in logs)


def test_ensure_adb_debug_enabled_reboot_when_open_but_no_device(tmp_path: Path, monkeypatch) -> None:
    controller = _make_controller(tmp_path, adb_debug=ADB_DEBUG_LOCAL)
    monkeypatch.setattr(controller, "is_emulator_running", lambda **_: True)
    monkeypatch.setattr(controller, "_has_adb_device", lambda **_: False)
    monkeypatch.setattr(
        controller,
        "_run_ldconsole",
        lambda args, **_: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    changed, logs = controller.ensure_adb_debug_enabled(reboot_if_running=True)
    assert changed is True
    assert any("reboot to restore" in line for line in logs)
