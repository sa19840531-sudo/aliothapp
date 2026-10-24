"""Capture 欄位名一致性：Writer 與 Demo 消費者必須同一協定欄位。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _read(rel: str) -> str:
    root = Path(__file__).resolve().parents[1]  # services/api
    return (root / "app" / rel).read_text(encoding="utf-8")


def test_android_runtime_and_demo_consumers_share_capture_field() -> None:
    runtime = _read("infrastructure/device/android_capture_runtime.py")
    root = Path(__file__).resolve().parents[1] / "app"
    if not (root / "mobile_demo_ai_driver.py").exists() or not (
        root / "api/v1/mobile_demo_agent_ad_capture.py"
    ).exists():
        pytest.skip("Mobile Demo 消费者已下线，正式链路只保留 Android Runtime")
    ai_driver = _read("mobile_demo_ai_driver.py")
    agent = _read("api/v1/mobile_demo_agent_ad_capture.py")

    runtime_uses_capture_id = "capture_id=" in runtime and "CaptureRef(" in runtime
    runtime_uses_page = "page_capture_id=" in runtime and "CaptureReference(" in runtime
    assert runtime_uses_capture_id or runtime_uses_page

    if runtime_uses_capture_id:
        assert ".after_capture.capture_id" in ai_driver
        assert ".after_capture.capture_id" in agent
        assert ".after_capture.page_capture_id" not in ai_driver
        assert ".after_capture.page_capture_id" not in agent
    else:
        assert ".after_capture.page_capture_id" in ai_driver
        assert ".after_capture.page_capture_id" in agent
        assert ".after_capture.capture_id" not in ai_driver
        assert ".after_capture.capture_id" not in agent
