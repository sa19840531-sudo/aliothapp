"""MoreLogin 設備 Open API（截圖／shell）單測。"""

from __future__ import annotations

import base64
import io

import pytest

from services.api.app.infrastructure.device.morelogin_device_api import (
    MoreLoginShellCommandUnsupportedError,
    decode_morelogin_capture_base64,
    morelogin_execute_shell_command,
    morelogin_screenshot_png_bytes,
    resolve_cloud_phone_morelogin_device_api_mode,
    shell_args_to_command,
    should_use_morelogin_device_api,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_decode_morelogin_capture_base64_data_url() -> None:
    encoded = base64.b64encode(_PNG).decode("ascii")
    raw = f"data:image/png;base64,{encoded}"
    assert decode_morelogin_capture_base64(raw) == _PNG


def test_decode_morelogin_capture_base64_jpeg() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    encoded = base64.b64encode(jpeg).decode("ascii")
    assert decode_morelogin_capture_base64(encoded) == jpeg


def test_morelogin_capture_bytes_to_png_from_jpeg() -> None:
    from PIL import Image

    from services.api.app.infrastructure.device.morelogin_device_api import (
        morelogin_capture_bytes_to_png,
    )

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buffer, format="JPEG")
    png = morelogin_capture_bytes_to_png(buffer.getvalue())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_shell_args_to_command_quotes_spaces() -> None:
    assert shell_args_to_command(["input", "tap", "100", "200"]) == "input tap 100 200"
    assert shell_args_to_command(["echo", "hello world"]) == 'echo "hello world"'


def test_should_use_morelogin_device_api_requires_open_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_MORELOGIN_API", "hybrid")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key")
    assert should_use_morelogin_device_api("1721996810228932")
    assert not should_use_morelogin_device_api("")


def test_resolve_mode_api_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_MORELOGIN_API", "1")
    assert resolve_cloud_phone_morelogin_device_api_mode() == "api"


def test_morelogin_screenshot_png_bytes_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(_PNG).decode("ascii")

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        assert "screenCapBase64" in url
        assert payload["id"] == 1721996810228932
        return {
            "code": 0,
            "data": {
                "captureBase64": f"data:image/png;base64,{encoded}",
                "success": True,
                "errorMessage": "success",
            },
        }

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_device_api.post_morelogin_json",
        _fake_post,
    )
    assert morelogin_screenshot_png_bytes("1721996810228932") == _PNG


def test_morelogin_execute_shell_command_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        assert "exeCommand" in url
        assert payload["command"] == "wm size"
        return {"code": 0, "data": "Physical size: 1080x1920", "msg": ""}

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_device_api.post_morelogin_json",
        _fake_post,
    )
    out = morelogin_execute_shell_command("1721996810228932", "wm size")
    assert "1080x1920" in out


@pytest.mark.parametrize(
    "reply",
    ["Command not found", "  command not found  ", "Not support"],
)
def test_execute_shell_command_rejects_unsupported_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
) -> None:
    """Agent 未放行命令时必须抛错，调用方才会回退真实 ADB。"""

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        return {"code": 0, "data": reply, "msg": ""}

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_device_api.post_morelogin_json",
        _fake_post,
    )
    with pytest.raises(MoreLoginShellCommandUnsupportedError):
        morelogin_execute_shell_command("1721996810228932", "dumpsys activity activities")


def test_execute_shell_command_keeps_output_containing_sentinel_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 dumpsys 文本里恰好含哨兵词时不得误判为通道不可用。"""

    payload_text = "mResumedActivity: ActivityRecord{a b/.Main}\ncommand not found\n"

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        return {"code": 0, "data": payload_text, "msg": ""}

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_device_api.post_morelogin_json",
        _fake_post,
    )
    assert morelogin_execute_shell_command("1", "dumpsys activity activities") == payload_text
