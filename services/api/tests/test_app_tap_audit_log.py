"""App 探索点击坐标审计日志。"""

from __future__ import annotations

import logging

from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
)
from services.api.app.infrastructure.device.app_tap_audit_log import log_app_tap_audit


def test_log_app_tap_audit_emits_stable_prefix(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="alioth.app.tap_audit"):
        log_app_tap_audit(
            "adb_tap",
            device_x=100,
            device_y=200,
            shell="input tap 100 200",
        )
    assert any("app_tap_audit" in record.message for record in caplog.records)
    assert any("stage=adb_tap" in record.message for record in caplog.records)
    assert any("device_x=100" in record.message for record in caplog.records)
    assert any("device_y=200" in record.message for record in caplog.records)


def test_resolver_logs_source_and_device_coords(caplog) -> None:
    resolver = CanonicalPointTargetResolver(device_width=1440, device_height=3040)
    request = SemanticTargetResolutionRequest(
        task_id="t1",
        session_id="s1",
        source_capture_id="c1",
        action_type=InteractionOperation.CLICK,
        semantic_target="我",
        parameters={
            "point": {
                "x": 1260,
                "y": 2794,
                "space": "capture_px",
            },
            "base_width": 1440,
            "base_height": 3040,
            "display": {"width": 1440, "height": 3040},
        },
    )
    with caplog.at_level(logging.INFO, logger="alioth.app.tap_audit"):
        resolved = resolver.resolve_android(request)

    assert resolved.tap_x == 1260
    assert resolved.tap_y == 2794
    messages = " | ".join(record.message for record in caplog.records)
    assert "app_tap_audit" in messages
    assert "stage=resolve" in messages
    assert "source_x=1260" in messages
    assert "source_y=2794" in messages
    assert "device_x=1260" in messages
    assert "device_y=2794" in messages
    assert "source_space=capture_px" in messages


def test_adb_ops_tap_logs_actual_shell_coords(caplog, monkeypatch) -> None:
    from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps

    calls: list[list[str]] = []

    def fake_shell(args: list[str], timeout: float = 20.0) -> None:
        calls.append(list(args))

    ops = AdbDeviceOps(
        backend="google_emulator",
        width=1440,
        height=3040,
        _log=lambda *_a, **_k: None,
        _screencap_fn=lambda _dest: None,
        _shell_fn=fake_shell,
    )
    object.__setattr__(ops, "serial", "emulator-5554")
    object.__setattr__(ops, "_live_touch_indicator_ready", True)
    monkeypatch.setattr(ops, "_live_touch_indicator_on", lambda: False)

    with caplog.at_level(logging.INFO, logger="alioth.app.tap_audit"):
        ops.tap(333, 2222)

    assert calls == [["input", "tap", "333", "2222"]]
    messages = " | ".join(record.message for record in caplog.records)
    assert "stage=adb_tap" in messages
    assert "device_x=333" in messages
    assert "device_y=2222" in messages
    assert "input tap 333 2222" in messages
