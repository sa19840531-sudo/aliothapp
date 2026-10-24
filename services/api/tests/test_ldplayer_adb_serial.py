"""多機時 ADB serial 不可回退到 adb devices 第一台。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.api.app.infrastructure.device.ldplayer import LdPlayerController, LdPlayerError


def test_adb_serial_rejects_devices_list_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "LDPlayer9"
    (home / "vms" / "config").mkdir(parents=True)
    (home / "ldconsole.exe").write_text("", encoding="utf-8")
    (home / "vms" / "config" / "leidian1.config").write_text("{}", encoding="utf-8")
    c = LdPlayerController(home=home, index=1)

    def fake_adb(command: str, *, timeout_seconds: float = 120.0):
        r = MagicMock()
        if command.startswith("get-serialno"):
            r.stdout = "unknown\n"
            r.stderr = ""
            return r
        if command.startswith("devices"):
            r.stdout = "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n"
            r.stderr = ""
            return r
        r.stdout = ""
        r.stderr = ""
        return r

    monkeypatch.setattr(c, "adb", fake_adb)
    with pytest.raises(LdPlayerError, match="index=1"):
        c.adb_serial()


def test_airtest_device_uri_pins_serial(tmp_path: Path) -> None:
    home = tmp_path / "LDPlayer9"
    (home / "vms" / "config").mkdir(parents=True)
    (home / "ldconsole.exe").write_text("", encoding="utf-8")
    (home / "vms" / "config" / "leidian0.config").write_text("{}", encoding="utf-8")
    c = LdPlayerController(home=home, index=0)
    assert c.airtest_device_uri("emulator-5556") == "Android:///emulator-5556"
