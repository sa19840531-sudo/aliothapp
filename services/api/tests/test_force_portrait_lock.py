"""全局直屏锁定合同。"""

from __future__ import annotations

import pytest

from services.api.app.infrastructure.device.adb_ops import (
    AdbDeviceOps,
    apply_force_portrait_lock,
    force_portrait_lock_enabled,
)


def test_force_portrait_lock_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_FORCE_PORTRAIT", raising=False)
    assert force_portrait_lock_enabled() is True
    monkeypatch.setenv("ALIOTH_FORCE_PORTRAIT", "0")
    assert force_portrait_lock_enabled() is False


def test_apply_force_portrait_lock_runs_core_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_FORCE_PORTRAIT", "1")
    calls: list[list[str]] = []

    def shell(args: list[str], timeout: float) -> None:
        del timeout
        calls.append(list(args))

    def shell_output(args: list[str], timeout: float) -> str:
        del timeout
        if args == ["wm", "size"]:
            return "Physical size: 1440x3040\nOverride size: 1440x3040\n"
        return ""

    logs = apply_force_portrait_lock(shell=shell, shell_output=shell_output)

    assert any(c[:3] == ["settings", "put", "system"] for c in calls)
    assert ["settings", "put", "system", "user_rotation", "0"] in calls
    assert ["settings", "put", "system", "accelerometer_rotation", "0"] in calls
    assert any("ignore-orientation-request" in " ".join(c) for c in calls)
    assert ["wm", "set-user-rotation", "lock", "0"] in calls
    assert ["wm", "set-fix-to-user-rotation", "enabled"] in calls
    assert "force_portrait_lock applied" in logs
    assert any("portrait size ok=" in line for line in logs)


def test_apply_force_portrait_lock_resets_landscape_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_FORCE_PORTRAIT", "1")
    calls: list[list[str]] = []

    def shell(args: list[str], timeout: float) -> None:
        del timeout
        calls.append(list(args))

    def shell_output(args: list[str], timeout: float) -> str:
        del timeout
        if args == ["wm", "size"]:
            return "Physical size: 1440x3040\nOverride size: 3040x1440\n"
        return ""

    logs = apply_force_portrait_lock(shell=shell, shell_output=shell_output)

    assert ["wm", "size", "reset"] in calls
    assert any("landscape override" in line for line in logs)


def test_apply_force_portrait_lock_can_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_FORCE_PORTRAIT", "off")
    calls: list[list[str]] = []

    logs = apply_force_portrait_lock(
        shell=lambda args, timeout: calls.append(list(args)),
    )

    assert calls == []
    assert logs == ["portrait lock skipped (ALIOTH_FORCE_PORTRAIT off)"]


def test_adb_device_ops_force_portrait_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_FORCE_PORTRAIT", "1")
    shell_calls: list[list[str]] = []

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=lambda args, timeout: shell_calls.append(list(args)),
        _shell_output_fn=lambda args, timeout: "Physical size: 1080x1920\n",
    )

    logs = ops.force_portrait_lock()

    assert "force_portrait_lock applied" in logs
    assert any("user_rotation" in " ".join(c) for c in shell_calls)
