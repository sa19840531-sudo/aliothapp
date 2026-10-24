"""ADB 前台包名／Activity 只读事实解析合同。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import services.api.app.infrastructure.device.adb_ops as adb_ops_module
from services.api.app.infrastructure.device.adb_ops import (
    AdbDeviceOps,
    _require_shell_success,
    is_clearable_leftover_qc_package,
    open_adb_device_ops,
    parse_android_runtime_identity,
    screencap_exec_out,
)
from services.api.app.infrastructure.device.backend import BACKEND_CLOUD_PHONE


def _ops(shell_output):
    return AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=lambda _args, _timeout: None,
        _shell_output_fn=shell_output,
    )


def test_dump_ui_hierarchy_does_not_read_stale_fixed_file() -> None:
    shell_calls: list[tuple[str, ...]] = []
    output_calls: list[tuple[str, ...]] = []
    stale_xml = "<hierarchy><node text=\"stale\" /></hierarchy>"

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        output_calls.append(tuple(args))
        if args == ["cat", "/sdcard/alioth_ui_dump.xml"]:
            return stale_xml
        if args[:2] == ["uiautomator", "dump"]:
            return "UI hierchary dumped to: " + args[2]
        return ""

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    with pytest.raises(RuntimeError, match="未返回有效 hierarchy XML"):
        ops.dump_ui_hierarchy()

    dump_paths = [
        call[2]
        for call in output_calls
        if call[:2] == ("uiautomator", "dump") and call[2] != "/dev/tty"
    ]
    cleanup_paths = [
        call[2] for call in shell_calls if call[:2] == ("rm", "-f")
    ]
    assert len(dump_paths) == 1
    assert dump_paths[0] != "/sdcard/alioth_ui_dump.xml"
    assert cleanup_paths == dump_paths
    assert ("cat", "/sdcard/alioth_ui_dump.xml") not in output_calls
    assert ("cat", dump_paths[0]) in output_calls


def test_dump_ui_hierarchy_uses_a_new_path_and_cleans_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_calls: list[tuple[str, ...]] = []
    output_calls: list[tuple[str, ...]] = []
    fallback_calls: list[tuple[object, object, object]] = []

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        output_calls.append(tuple(args))
        if args[:2] == ["uiautomator", "dump"]:
            return "UI hierchary dumped to: " + args[2]
        return "<hierarchy />" if args[:1] == ["cat"] else ""

    def fallback(shell_output_fn, shell_fn, log_fn):  # noqa: ANN001
        fallback_calls.append((shell_output_fn, shell_fn, log_fn))
        raise AssertionError("normal dump must not call the idle fallback")

    monkeypatch.setattr(adb_ops_module, "dump_current_ui_hierarchy", fallback)

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    assert ops.dump_ui_hierarchy(remote_path="/data/local/tmp/ui.xml") == "<hierarchy />"
    assert ops.dump_ui_hierarchy(remote_path="/data/local/tmp/ui.xml") == "<hierarchy />"

    dump_paths = [
        call[2]
        for call in output_calls
        if call[:2] == ("uiautomator", "dump")
    ]
    cleanup_paths = [
        call[2] for call in shell_calls if call[:2] == ("rm", "-f")
    ]
    cat_paths = [call[1] for call in output_calls if call[:1] == ("cat",)]
    assert len(dump_paths) == 2
    assert len(set(dump_paths)) == 2
    assert all(path.startswith("/data/local/tmp/ui.") for path in dump_paths)
    assert all(path.endswith(".xml") for path in dump_paths)
    assert cat_paths == dump_paths
    assert cleanup_paths == dump_paths
    assert fallback_calls == []


def test_dump_ui_hierarchy_cleans_new_path_when_dump_fails_without_cat() -> None:
    shell_calls: list[tuple[str, ...]] = []
    output_calls: list[tuple[str, ...]] = []

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        output_calls.append(tuple(args))
        if args[:2] == ["uiautomator", "dump"]:
            raise RuntimeError("dump failed")
        return ""

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    with pytest.raises(RuntimeError, match="dump failed"):
        ops.dump_ui_hierarchy()

    dump_paths = [
        call[2]
        for call in output_calls
        if call[:2] == ("uiautomator", "dump")
    ]
    cleanup_paths = [
        call[2] for call in shell_calls if call[:2] == ("rm", "-f")
    ]
    assert len(dump_paths) == 1
    assert cleanup_paths == dump_paths
    assert len(output_calls) == 1
    assert output_calls[0][:2] == ("uiautomator", "dump")


def test_dump_ui_hierarchy_cleanup_failure_does_not_replace_success() -> None:
    logs: list[str] = []

    def shell(args: list[str], _timeout: float) -> None:
        if args[:2] == ["rm", "-f"]:
            raise RuntimeError("cleanup failed")

    def shell_output(args: list[str], _timeout: float) -> str:
        if args[:2] == ["uiautomator", "dump"]:
            return "UI hierchary dumped to: " + args[2]
        return "<hierarchy />" if args[:1] == ["cat"] else ""

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=logs.append,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    assert ops.dump_ui_hierarchy() == "<hierarchy />"
    assert logs == [
        "ui_tree dump bytes=13",
        "ui_tree cleanup skipped: RuntimeError",
    ]


def test_dump_ui_hierarchy_cleanup_failure_does_not_replace_dump_error() -> None:
    def shell(args: list[str], _timeout: float) -> None:
        if args[:2] == ["rm", "-f"]:
            raise RuntimeError("cleanup failed")

    def shell_output(args: list[str], _timeout: float) -> str:
        if args[:2] == ["uiautomator", "dump"]:
            raise ValueError("dump failed")
        return ""

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    with pytest.raises(ValueError, match="dump failed"):
        ops.dump_ui_hierarchy()


def test_dump_ui_hierarchy_uses_current_root_fallback_after_idle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_calls: list[tuple[str, ...]] = []
    output_calls: list[tuple[str, ...]] = []
    fallback_calls: list[tuple[object, object, object]] = []
    logs: list[str] = []
    fallback_xml = "<hierarchy><node text=\"current\" /></hierarchy>"

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        output_calls.append(tuple(args))
        if args[:2] == ["uiautomator", "dump"]:
            return "ERROR: could not get idle state."
        raise AssertionError("idle fallback must not cat the primary dump path")

    def fallback(shell_output_fn, shell_fn, log_fn):  # noqa: ANN001
        fallback_calls.append((shell_output_fn, shell_fn, log_fn))
        return fallback_xml

    monkeypatch.setattr(adb_ops_module, "dump_current_ui_hierarchy", fallback)
    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=logs.append,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    assert ops.dump_ui_hierarchy() == fallback_xml

    dump_paths = [
        call[2]
        for call in output_calls
        if call[:2] == ("uiautomator", "dump")
    ]
    cleanup_paths = [
        call[2] for call in shell_calls if call[:2] == ("rm", "-f")
    ]
    assert len(dump_paths) == 1
    assert cleanup_paths == dump_paths
    assert output_calls == [output_calls[0]]
    assert output_calls[0][:2] == ("uiautomator", "dump")
    assert fallback_calls == [(shell_output, shell, logs.append)]


def test_dump_ui_hierarchy_preserves_idle_fallback_error_and_cleans_primary_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_calls: list[tuple[str, ...]] = []
    output_calls: list[tuple[str, ...]] = []
    fallback_calls: list[tuple[object, object, object]] = []

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        output_calls.append(tuple(args))
        if args[:2] == ["uiautomator", "dump"]:
            return "ERROR: could not get idle state."
        raise AssertionError("idle fallback must not cat the primary dump path")

    def fallback(shell_output_fn, shell_fn, log_fn):  # noqa: ANN001
        fallback_calls.append((shell_output_fn, shell_fn, log_fn))
        raise RuntimeError("fallback failed")

    monkeypatch.setattr(adb_ops_module, "dump_current_ui_hierarchy", fallback)
    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    with pytest.raises(RuntimeError, match="fallback failed"):
        ops.dump_ui_hierarchy()

    dump_paths = [
        call[2]
        for call in output_calls
        if call[:2] == ("uiautomator", "dump")
    ]
    cleanup_paths = [
        call[2] for call in shell_calls if call[:2] == ("rm", "-f")
    ]
    assert len(dump_paths) == 1
    assert cleanup_paths == dump_paths
    assert output_calls == [output_calls[0]]
    assert fallback_calls == [(shell_output, shell, ops._log)]


def test_dump_ui_hierarchy_does_not_fallback_for_non_idle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_calls: list[tuple[str, ...]] = []
    output_calls: list[tuple[str, ...]] = []
    fallback_calls: list[tuple[object, object, object]] = []

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        output_calls.append(tuple(args))
        if args[:2] == ["uiautomator", "dump"]:
            return "ERROR: could not get window root."
        raise AssertionError("non-idle errors must not cat the primary dump path")

    def fallback(shell_output_fn, shell_fn, log_fn):  # noqa: ANN001
        fallback_calls.append((shell_output_fn, shell_fn, log_fn))
        raise AssertionError("non-idle errors must not call the idle fallback")

    monkeypatch.setattr(adb_ops_module, "dump_current_ui_hierarchy", fallback)
    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    with pytest.raises(RuntimeError, match="设备采集失败"):
        ops.dump_ui_hierarchy()

    dump_paths = [
        call[2]
        for call in output_calls
        if call[:2] == ("uiautomator", "dump")
    ]
    cleanup_paths = [
        call[2] for call in shell_calls if call[:2] == ("rm", "-f")
    ]
    assert len(dump_paths) == 1
    assert cleanup_paths == dump_paths
    assert output_calls == [output_calls[0]]
    assert fallback_calls == []


def test_shell_nonzero_exit_cannot_be_reported_as_success() -> None:
    response = subprocess.CompletedProcess(
        args=["adb", "shell", "input", "tap", "1", "2"],
        returncode=1,
        stdout="",
        stderr="device offline",
    )

    with pytest.raises(RuntimeError, match="returncode=1"):
        _require_shell_success(response)


def test_screencap_exec_out_timeout_falls_back_to_remote_pull(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    timeouts: list[float] = []
    png = adb_ops_module.PNG_MAGIC + b"fallback-evidence" * 10

    def _run(args, **kwargs):  # noqa: ANN001
        command = tuple(str(item) for item in args)
        calls.append(command)
        timeouts.append(float(kwargs["timeout"]))
        if "exec-out" in command:
            raise subprocess.TimeoutExpired(
                command,
                adb_ops_module._SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS,
            )
        if "pull" in command:
            Path(command[-1]).write_bytes(png)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(adb_ops_module.subprocess, "run", _run)
    destination = tmp_path / "capture.png"

    screencap_exec_out(Path("/sdk/adb"), "cloud:5555", destination)

    assert destination.read_bytes() == png
    assert any("screencap" in call and "shell" in call for call in calls)
    assert any("pull" in call for call in calls)
    assert any(
        call[-3:] == ("rm", "-f", "/sdcard/alioth_agent_cap.png")
        for call in calls
    )
    assert timeouts == [
        adb_ops_module._SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS,
        adb_ops_module._SCREENSHOT_REMOTE_TIMEOUT_SECONDS,
        adb_ops_module._SCREENSHOT_PULL_TIMEOUT_SECONDS,
        adb_ops_module._SCREENSHOT_CLEANUP_TIMEOUT_SECONDS,
    ]
    assert sum(timeouts) <= adb_ops_module._SCREENSHOT_FALLBACK_MAX_SECONDS
    assert adb_ops_module._SCREENSHOT_FALLBACK_MAX_SECONDS < 60.0
    assert (
        adb_ops_module._SCREENSHOT_PULL_TIMEOUT_SECONDS
        > adb_ops_module._SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS
    )
    assert adb_ops_module._SCREENSHOT_PULL_TIMEOUT_SECONDS >= 35.0


def test_screencap_exec_out_fallback_timeout_is_bounded_per_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []
    png = adb_ops_module.PNG_MAGIC + b"fallback-evidence" * 10

    def _run(args, **kwargs):  # noqa: ANN001
        command = tuple(str(item) for item in args)
        timeout = float(kwargs["timeout"])
        calls.append((command, timeout))
        if "exec-out" in command:
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"")
        if "pull" in command:
            Path(command[-1]).write_bytes(png)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(adb_ops_module.subprocess, "run", _run)
    destination = tmp_path / "capture.png"

    screencap_exec_out(Path("/sdk/adb"), "cloud:5555", destination)

    expected = {
        "exec-out": adb_ops_module._SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS,
        "remote": adb_ops_module._SCREENSHOT_REMOTE_TIMEOUT_SECONDS,
        "pull": adb_ops_module._SCREENSHOT_PULL_TIMEOUT_SECONDS,
        "cleanup": adb_ops_module._SCREENSHOT_CLEANUP_TIMEOUT_SECONDS,
    }
    assert len(calls) == 4
    assert calls[0][1] == expected["exec-out"]
    assert calls[1][0][3:6] == ("shell", "screencap", "-p")
    assert calls[1][1] == expected["remote"]
    assert calls[2][0][3] == "pull"
    assert calls[2][1] == expected["pull"]
    assert calls[3][0][3:6] == ("shell", "rm", "-f")
    assert calls[3][1] == expected["cleanup"]
    assert sum(timeout for _command, timeout in calls) <= 60.0


def test_screencap_remote_pull_preference_skips_exec_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    png = adb_ops_module.PNG_MAGIC + b"remote-first-evidence" * 10

    def _run(args, **kwargs):  # noqa: ANN001
        command = tuple(str(item) for item in args)
        calls.append(command)
        if "pull" in command:
            Path(command[-1]).write_bytes(png)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(adb_ops_module.subprocess, "run", _run)
    destination = tmp_path / "capture.png"

    screencap_exec_out(
        Path("/sdk/adb"),
        "cloud:5555",
        destination,
        prefer_remote_pull=True,
    )

    assert destination.read_bytes() == png
    assert not any("exec-out" in call for call in calls)
    assert calls[0][3:6] == ("shell", "screencap", "-p")
    assert calls[1][3] == "pull"
    assert calls[2][-3:] == ("rm", "-f", "/sdcard/alioth_agent_cap.png")


def test_cloud_phone_ops_use_remote_pull_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    preferences: list[tuple[bool, float | None]] = []

    class _Controller:
        _serial = "cloud:5555"
        adb_path = Path("/sdk/adb")

        def _adb(self, args, *, timeout_seconds):  # noqa: ANN001
            del args, timeout_seconds
            return subprocess.CompletedProcess(
                ["adb"],
                0,
                stdout="Physical size: 1080x1920",
                stderr="",
            )

    def _capture(  # noqa: ANN001
        _adb_path,
        _serial,
        destination,
        *,
        prefer_remote_pull=False,
        remote_pull_timeout_seconds=None,
    ):
        preferences.append(
            (bool(prefer_remote_pull), remote_pull_timeout_seconds)
        )
        destination.write_bytes(adb_ops_module.PNG_MAGIC + b"cloud-evidence" * 10)

    monkeypatch.setattr(adb_ops_module, "screencap_exec_out", _capture)
    ops = open_adb_device_ops(
        BACKEND_CLOUD_PHONE,
        cloud_phone_controller=_Controller(),
    )

    ops.screencap(tmp_path / "cloud.png")

    assert preferences == [
        (True, adb_ops_module._SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS)
    ]
    assert adb_ops_module._SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS == 90.0
    assert (
        adb_ops_module._SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS
        > adb_ops_module._SCREENSHOT_PULL_TIMEOUT_SECONDS
    )
    assert adb_ops_module._SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS == 45.0


def test_runtime_identity_prefers_resumed_activity_over_window_focus() -> None:
    text = """
    mCurrentFocus=Window{9 u0 com.example.browser/.BrowserActivity}
    mResumedActivity: ActivityRecord{3 u0 com.example.target/.MainActivity}
    """

    assert parse_android_runtime_identity(text) == (
        "com.example.target",
        "com.example.target.MainActivity",
    )


def test_runtime_identity_falls_back_to_window_focus() -> None:
    text = "mCurrentFocus=Window{9 u0 com.example.browser/.BrowserActivity}"

    assert parse_android_runtime_identity(text) == (
        "com.example.browser",
        "com.example.browser.BrowserActivity",
    )


def test_clearable_leftover_qc_package_skips_system_keeps_third_party() -> None:
    assert is_clearable_leftover_qc_package("com.edxuf.euloop") is True
    assert is_clearable_leftover_qc_package("com.android.launcher3") is False
    assert is_clearable_leftover_qc_package("android") is False
    assert is_clearable_leftover_qc_package("") is False


def test_clear_leftover_qc_app_force_stops_explicit_and_foreground_then_home() -> None:
    shell_calls: list[tuple[str, ...]] = []

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        if args == ["dumpsys", "activity", "activities"]:
            return (
                "mResumedActivity: ActivityRecord{1 u0 "
                "com.leftover.app/.MainActivity t1}"
            )
        return ""

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    logs = ops.clear_leftover_qc_app(package_name="com.first.apk")

    assert "force_stop explicit=com.first.apk" in logs
    assert "force_stop foreground=com.leftover.app" in logs
    assert "press HOME" in logs
    assert ("am", "force-stop", "com.first.apk") in shell_calls
    assert ("am", "force-stop", "com.leftover.app") in shell_calls
    assert ("input", "keyevent", "3") in shell_calls


def test_clear_leftover_qc_app_does_not_force_stop_system_launcher() -> None:
    shell_calls: list[tuple[str, ...]] = []

    def shell(args: list[str], _timeout: float) -> None:
        shell_calls.append(tuple(args))

    def shell_output(args: list[str], _timeout: float) -> str:
        if args == ["dumpsys", "activity", "activities"]:
            return (
                "mResumedActivity: ActivityRecord{1 u0 "
                "com.android.launcher3/.Launcher t1}"
            )
        return ""

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=shell_output,
    )

    logs = ops.clear_leftover_qc_app()

    assert all("force_stop foreground" not in line for line in logs)
    assert ("am", "force-stop", "com.android.launcher3") not in shell_calls
    assert ("input", "keyevent", "3") in shell_calls


def test_runtime_identity_unknown_or_unreadable_returns_none() -> None:
    assert parse_android_runtime_identity("mResumedActivity: none") is None

    calls: list[tuple[str, ...]] = []

    def shell_output(args: list[str], _timeout: float) -> str:
        calls.append(tuple(args))
        if args == ["dumpsys", "activity", "activities"]:
            raise RuntimeError("dumpsys unavailable")
        return "no focused window"

    ops = _ops(shell_output)

    assert ops.current_runtime_identity() is None
    assert calls == [
        ("dumpsys", "activity", "activities"),
        ("dumpsys", "window", "windows"),
    ]


def test_runtime_identity_accepts_android_system_package() -> None:
    text = "mResumedActivity: ActivityRecord{3 u0 android/.PermissionController}"

    assert parse_android_runtime_identity(text) == (
        "android",
        "android.PermissionController",
    )


def test_runtime_identity_reads_resumed_then_window_from_device() -> None:
    calls: list[tuple[str, ...]] = []

    def shell_output(args: list[str], _timeout: float) -> str:
        calls.append(tuple(args))
        return (
            "mCurrentFocus=Window{9 u0 com.example.browser/.BrowserActivity}"
            if args == ["dumpsys", "window", "windows"]
            else "no resumed activity"
        )

    ops = _ops(shell_output)

    assert ops.current_runtime_identity() == (
        "com.example.browser",
        "com.example.browser.BrowserActivity",
    )
    assert calls == [
        ("dumpsys", "activity", "activities"),
        ("dumpsys", "window", "windows"),
    ]


def test_fault_signals_uses_compact_dropbox_index_and_targeted_body() -> None:
    calls: list[tuple[tuple[str, ...], float]] = []

    def shell_output(args: list[str], timeout: float) -> str:
        calls.append((tuple(args), timeout))
        if args == ["dumpsys", "dropbox"]:
            return "Drop box contents: data_app_anr\n"
        if args == ["dumpsys", "dropbox", "--print", "data_app_anr"]:
            return "ANR in com.example.target: input dispatching timed out\n"
        if args == ["ls", "-lt", "/data/anr"]:
            return "total 0\n"
        if args == ["ls", "-lt", "/data/tombstones"]:
            return "total 0\n"
        raise AssertionError(args)

    text = _ops(shell_output).dump_fault_signals(package_name="com.example.target")

    assert "ANR in com.example.target" in text
    assert ("dumpsys", "dropbox", "--print") not in {
        args for args, _timeout in calls
    }
    assert ("dumpsys", "dropbox", "--print", "data_app_anr") in {
        args for args, _timeout in calls
    }
    assert all(timeout <= 3.0 for _args, timeout in calls)


def test_fault_signals_enforces_shared_budget_and_keeps_unavailable_truthful(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(adb_ops_module.time, "monotonic", lambda: clock[0])
    calls: list[tuple[tuple[str, ...], float]] = []

    def shell_output(args: list[str], timeout: float) -> str:
        calls.append((tuple(args), timeout))
        clock[0] += timeout
        if args == ["dumpsys", "dropbox"]:
            return "\n".join(adb_ops_module._FAULT_DROPBOX_TAGS)
        raise TimeoutError("adb timeout")

    text = _ops(shell_output).dump_fault_signals(package_name="com.example.target")

    assert sum(timeout for _args, timeout in calls) == 12.0
    assert calls[-1][1] == 0.5
    assert not any(args[:2] == ("ls", "-lt") for args, _timeout in calls)
    assert "fault_aux_unavailable:TimeoutError" in text
    assert "fault_aux_skipped:budget_exhausted" in text
    assert "tombstone" not in text


def test_screencap_exec_out_records_five_stage_compatible_spans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    png = adb_ops_module.PNG_MAGIC + b"span-evidence" * 10
    clock_ms = {"value": 10_000}

    def _mono() -> float:
        return clock_ms["value"] / 1000.0

    def _run(args, **kwargs):  # noqa: ANN001
        command = tuple(str(item) for item in args)
        if "screencap" in command and "shell" in command:
            clock_ms["value"] += 120
        elif "pull" in command:
            clock_ms["value"] += 340
            Path(command[-1]).write_bytes(png)
        elif "rm" in command:
            clock_ms["value"] += 50
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(adb_ops_module.time, "perf_counter", _mono)
    monkeypatch.setattr(adb_ops_module.subprocess, "run", _run)
    destination = tmp_path / "span.png"

    trace = screencap_exec_out(
        Path("/sdk/adb"),
        "cloud:5555",
        destination,
        prefer_remote_pull=True,
    )

    assert trace.path_mode == "remote_pull"
    assert trace.remote_screencap_ms == pytest.approx(120, abs=1)
    assert trace.adb_pull_ms == pytest.approx(340, abs=1)
    assert trace.cleanup_ms == pytest.approx(50, abs=1)
    assert trace.exec_out_ms is None
    assert trace.png_bytes == len(png)
    assert destination.read_bytes() == png


def test_query_wm_size_uses_shell_when_available() -> None:
    calls: list[tuple[str, ...]] = []

    def shell_output(args: list[str], timeout: float) -> str:
        del timeout
        calls.append(tuple(args))
        if list(args[:2]) == ["wm", "size"]:
            return "Physical size: 1080x2400"
        return "mCurrentOrientation=0"

    ops = _ops(shell_output)
    (width, height), source = ops.query_wm_size()

    assert ("wm", "size") in calls
    assert (width, height) == (1080, 2400)
    assert source == "shell"
    assert ops.width == 1080
    assert ops.height == 2400


def test_parse_wm_size_prefers_override_over_physical() -> None:
    from services.api.app.infrastructure.device.adb_ops import parse_wm_size

    assert parse_wm_size(
        "Physical size: 1440x3040\nOverride size: 3040x1440\n"
    ) == (3040, 1440)
    assert parse_wm_size("Physical size: 1440x3040\n") == (1440, 3040)


def test_resolve_wm_display_size_swaps_physical_portrait_when_rotated() -> None:
    from services.api.app.infrastructure.device.adb_ops import resolve_wm_display_size

    assert resolve_wm_display_size(
        "Physical size: 1440x3040\n",
        display_rotation=90,
    ) == (3040, 1440)
    # Override 已是当前方向时不得再交换
    assert resolve_wm_display_size(
        "Physical size: 1440x3040\nOverride size: 3040x1440\n",
        display_rotation=90,
    ) == (3040, 1440)


def test_cloud_phone_tap_keeps_coordinates_and_shows_live_dot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.infrastructure.device.adb_ops import (
        LIVE_TOUCH_INDICATOR_DWELL_MS,
        tap_shell_args,
    )

    monkeypatch.delenv("ALIOTH_CLOUD_PHONE_SHOW_TOUCHES", raising=False)
    assert tap_shell_args(120, 340, live_visible=False) == ["input", "tap", "120", "340"]
    assert tap_shell_args(120, 340, live_visible=True) == [
        "input",
        "swipe",
        "120",
        "340",
        "120",
        "340",
        str(LIVE_TOUCH_INDICATOR_DWELL_MS),
    ]

    calls: list[tuple[str, ...]] = []

    def shell(args: list[str], _timeout: float) -> None:
        calls.append(tuple(args))

    ops = AdbDeviceOps(
        backend=BACKEND_CLOUD_PHONE,
        width=1440,
        height=3040,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=lambda _args, _timeout: "",
    )
    ops.tap(880, 210)
    assert ("settings", "put", "system", "show_touches", "1") in calls
    assert ("settings", "put", "system", "pointer_location", "0") in calls
    assert (
        "input",
        "swipe",
        "880",
        "210",
        "880",
        "210",
        str(LIVE_TOUCH_INDICATOR_DWELL_MS),
    ) in calls
    assert ("input", "tap", "880", "210") not in calls


def test_non_cloud_phone_tap_stays_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_SHOW_TOUCHES", "1")

    def shell(args: list[str], _timeout: float) -> None:
        calls.append(tuple(args))

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=shell,
        _shell_output_fn=lambda _args, _timeout: "",
    )
    ops.tap(10, 20)
    assert calls == [("input", "tap", "10", "20")]
