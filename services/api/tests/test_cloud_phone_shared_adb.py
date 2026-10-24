"""多台真实云手机共享 SDK ADB daemon 的 readiness 合同测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.api.app.infrastructure.device.cloud_phone import CloudPhoneController, CloudPhoneError
from services.api.app.infrastructure.device.google_emulator import GoogleEmulatorError


def test_fresh_adb_refresh_controller_preserves_morelogin_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("fake adb", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "0")
    controller = CloudPhoneController(
        morelogin_env_id="env-qa-a",
        allow_global_morelogin_fallback=False,
        apply_morelogin_to_process_env=False,
    )

    fresh = controller.fresh_for_adb_refresh()

    assert fresh is not controller
    assert fresh._morelogin_env_id == "env-qa-a"
    assert fresh._allow_global_morelogin_fallback is False
    assert fresh._apply_morelogin_to_process_env is False


def _make_controller(
    monkeypatch: pytest.MonkeyPatch,
    sdk: Path,
    endpoint: str,
) -> CloudPhoneController:
    """构造不访问 MoreLogin 和真实设备的云手机控制器夹具。"""
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB", endpoint)
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "0")
    controller = CloudPhoneController()
    monkeypatch.setattr(controller, "_refresh_endpoint_from_morelogin", lambda _logs: None)
    monkeypatch.setattr(controller, "_shell_probe_ok", lambda _serial: True)
    monkeypatch.setattr(
        controller._emu,
        "list_adb_devices",
        lambda: [(endpoint, "device")],
    )
    monkeypatch.setattr(
        controller._emu,
        "_run_adb",
        lambda _args, *, serial, timeout_seconds: SimpleNamespace(
            stdout="1",
            stderr="",
            returncode=0,
        ),
    )

    def _adb(args: list[str], *, timeout_seconds: float = 60.0):
        if args[:1] == ["connect"]:
            return SimpleNamespace(stdout="connected", stderr="", returncode=0)
        raise AssertionError(f"unexpected adb command: {args}")

    monkeypatch.setattr(controller, "_adb", _adb)
    return controller


def test_cloud_phone_readiness_never_kills_shared_adb_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """云手机 readiness 只能确保 daemon 存在，不能杀掉其他任务的连接。"""
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("fake adb", encoding="utf-8")
    commands: list[list[str]] = []

    def _run(command: list[str], **_kwargs):
        commands.append(list(command))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.subprocess.run",
        _run,
    )
    controller = _make_controller(monkeypatch, sdk, "cp-1.example:20268")

    serial, logs = controller.ensure_ready(timeout_seconds=30)

    assert serial == "cp-1.example:20268"
    assert any("start-server" in command for command in commands)
    assert all("kill-server" not in command for command in commands)
    assert any("shared" in line.lower() or "start-server" in line for line in logs)


def test_two_cloud_phone_readinesses_do_not_restart_shared_adb_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两台云手机交错 ready 时不得互相 kill 全局 ADB daemon。"""
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("fake adb", encoding="utf-8")
    commands: list[list[str]] = []

    def _run(command: list[str], **_kwargs):
        commands.append(list(command))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.subprocess.run",
        _run,
    )
    first = _make_controller(monkeypatch, sdk, "cp-1.example:20268")
    second = _make_controller(monkeypatch, sdk, "cp-2.example:20268")

    assert first.ensure_ready(timeout_seconds=30)[0] == "cp-1.example:20268"
    assert second.ensure_ready(timeout_seconds=30)[0] == "cp-2.example:20268"

    server_commands = [
        command for command in commands if "start-server" in command or "kill-server" in command
    ]
    assert len([command for command in server_commands if "start-server" in command]) >= 2
    assert all("kill-server" not in command for command in server_commands)


def test_one_cloud_phone_readiness_failure_does_not_break_another(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """单台云手机连接失败后，其他实例仍可复用共享 daemon。"""
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("fake adb", encoding="utf-8")
    commands: list[list[str]] = []

    def _run(command: list[str], **_kwargs):
        commands.append(list(command))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.subprocess.run",
        _run,
    )
    failed = _make_controller(monkeypatch, sdk, "cp-1.example:20268")
    healthy = _make_controller(monkeypatch, sdk, "cp-2.example:20268")

    def _connect_failure(args: list[str], *, timeout_seconds: float = 60.0):
        raise CloudPhoneError("cp-1 transient connect failure")

    monkeypatch.setattr(failed, "_adb", _connect_failure)
    with pytest.raises(CloudPhoneError, match="cp-1"):
        failed.ensure_ready(timeout_seconds=30)

    assert healthy.ensure_ready(timeout_seconds=30)[0] == "cp-2.example:20268"
    assert all("kill-server" not in command for command in commands)


def test_boot_probe_timeout_reopens_current_endpoint_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """boot probe 传输超时后只重开当前静态端点，再继续 readiness。"""
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("fake adb", encoding="utf-8")
    endpoint = "172.104.172.119:30615"
    controller = _make_controller(monkeypatch, sdk, endpoint)
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.time.sleep",
        lambda _seconds: None,
    )

    adb_calls: list[list[str]] = []

    def _adb(args: list[str], *, timeout_seconds: float = 60.0):
        del timeout_seconds
        adb_calls.append(list(args))
        if args[:1] in (["connect"], ["disconnect"]):
            return SimpleNamespace(stdout="connected", stderr="", returncode=0)
        raise AssertionError(args)

    boot_calls = 0

    def _run_adb(_args, *, serial, timeout_seconds):  # noqa: ANN001
        del serial, timeout_seconds
        nonlocal boot_calls
        boot_calls += 1
        if boot_calls == 1:
            cause = subprocess.TimeoutExpired("adb", 20.0)
            raise GoogleEmulatorError("adb 逾时：boot probe") from cause
        return SimpleNamespace(stdout="1", stderr="", returncode=0)

    monkeypatch.setattr(controller, "_adb", _adb)
    monkeypatch.setattr(controller._emu, "_run_adb", _run_adb)

    serial, _logs = controller.ensure_ready(timeout_seconds=30)

    assert serial == endpoint
    assert boot_calls == 2
    assert [call[0] for call in adb_calls] == [
        "connect",
        "disconnect",
        "connect",
        "connect",
    ]
    assert adb_calls[1][1] == endpoint
    assert adb_calls[2][1] == endpoint


def test_boot_probe_timeout_reopens_at_most_once_then_preserves_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """持续 boot probe 传输超时不能无限重连或吞掉原失败。"""
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("fake adb", encoding="utf-8")
    endpoint = "172.104.172.119:30615"
    controller = _make_controller(monkeypatch, sdk, endpoint)

    adb_calls: list[list[str]] = []

    def _adb(args: list[str], *, timeout_seconds: float = 60.0):
        del timeout_seconds
        adb_calls.append(list(args))
        if args[:1] in (["connect"], ["disconnect"]):
            return SimpleNamespace(stdout="connected", stderr="", returncode=0)
        raise AssertionError(args)

    def _run_adb(_args, *, serial, timeout_seconds):  # noqa: ANN001
        del serial, timeout_seconds
        cause = subprocess.TimeoutExpired("adb", 20.0)
        raise GoogleEmulatorError("adb 逾时：boot probe") from cause

    monkeypatch.setattr(controller, "_adb", _adb)
    monkeypatch.setattr(controller._emu, "_run_adb", _run_adb)

    with pytest.raises(GoogleEmulatorError, match="boot probe"):
        controller.ensure_ready(timeout_seconds=30)

    assert sum(call[0] == "disconnect" for call in adb_calls) == 1
    assert sum(call[0] == "connect" for call in adb_calls) == 3
