"""MoreLogin 雲手機設備操作 Open API：截圖與 Shell（繞過公網 ADB 瞬斷）。

文檔：
- POST /cloudphone/screenCapBase64
- POST /cloudphone/screenCap
- POST /cloudphone/exeCommand

環境變數：
- ``ALIOTH_CLOUD_PHONE_MORELOGIN_API``：``adb``（僅公網 ADB）／``api``（僅 Open API）／
  ``hybrid``（預設：Open API 優先，失敗回退 ADB）
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import re
import urllib.request
from typing import Any

from services.api.app.infrastructure.device.adb_ops import PNG_MAGIC
from services.api.app.infrastructure.device.morelogin_http import (
    MoreLoginAdbError,
    morelogin_cloudphone_url,
    post_morelogin_json,
    resolve_morelogin_api_mode,
)

__all__ = [
    "MoreLoginDeviceApiError",
    "MoreLoginShellCommandUnsupportedError",
    "decode_morelogin_capture_base64",
    "morelogin_execute_shell_command",
    "morelogin_screenshot_png_bytes",
    "resolve_cloud_phone_morelogin_device_api_mode",
]

# 部分雲手機鏡像的 MoreLogin Agent 不放行 dumpsys 等命令，exeCommand 仍回
# HTTP 200，data 只有一句「Command not found」。這是通道能力缺失，不是命令輸出，
# 必須當失敗處理，否則 hybrid 模式不會回退真實 ADB，前景身份會長期讀成空。
_SHELL_UNSUPPORTED_REPLIES: frozenset[str] = frozenset(
    {
        "command not found",
        "command not support",
        "command not supported",
        "not support",
        "unsupported command",
    }
)


class MoreLoginDeviceApiError(MoreLoginAdbError):
    """MoreLogin 設備操作 Open API 失敗。"""


class MoreLoginShellCommandUnsupportedError(MoreLoginDeviceApiError):
    """MoreLogin Agent 未放行該 shell 命令；調用方應回退真實 ADB。"""


def resolve_cloud_phone_morelogin_device_api_mode() -> str:
    """解析雲手機高頻操作（截圖／shell）是否走 MoreLogin Open API。"""
    raw = (os.getenv("ALIOTH_CLOUD_PHONE_MORELOGIN_API") or "hybrid").strip().lower()
    if raw in ("1", "true", "yes", "on", "api"):
        return "api"
    if raw in ("hybrid", "auto", "prefer_api"):
        return "hybrid"
    if raw in ("0", "false", "no", "off", "adb", "legacy"):
        return "adb"
    return "hybrid"


def should_use_morelogin_device_api(
    env_id: str | None,
    *,
    mode: str | None = None,
) -> bool:
    """是否應嘗試 MoreLogin 設備 Open API（需 env_id 且為 Open API 模式）。"""
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        cloud_phone_vendor_is_onirent,
        looks_like_adb_endpoint,
    )

    eid = str(env_id or "").strip()
    if not eid:
        return False
    if cloud_phone_vendor_is_onirent() or looks_like_adb_endpoint(eid):
        return False
    if resolve_morelogin_api_mode() != "open":
        return False
    resolved = (mode or resolve_cloud_phone_morelogin_device_api_mode()).strip().lower()
    return resolved in ("api", "hybrid")


def _env_id_payload(env_id: str) -> int | str:
    eid = str(env_id or "").strip()
    if not eid:
        raise MoreLoginDeviceApiError("MoreLogin env_id 未設定")
    try:
        return int(eid)
    except ValueError:
        return eid


def _raise_api_error(
    action: str,
    *,
    code: int,
    msg: str,
    payload: dict[str, Any] | None = None,
) -> None:
    detail = f"code={code} msg={msg}"
    if payload:
        detail = f"{detail} data={str(payload)[:200]}"
    raise MoreLoginDeviceApiError(f"MoreLogin {action} 失敗：{detail}")


def decode_morelogin_capture_base64(raw: str) -> bytes:
    """解析 screenCapBase64 回傳（支援 data URL 與純 Base64；PNG／JPEG）。"""
    text = str(raw or "").strip()
    if not text:
        raise MoreLoginDeviceApiError("screenCapBase64 回傳為空")
    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1].strip()
    text = re.sub(r"\s+", "", text)
    try:
        content = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise MoreLoginDeviceApiError("screenCapBase64 Base64 解碼失敗") from exc
    if len(content) < 3:
        raise MoreLoginDeviceApiError(f"screenCapBase64 內容過短（bytes={len(content)}）")
    if content.startswith(PNG_MAGIC):
        return content
    if content[:3] == b"\xff\xd8\xff":
        return content
    raise MoreLoginDeviceApiError(
        f"screenCapBase64 非 PNG／JPEG（bytes={len(content)} head={content[:8].hex()})"
    )


def morelogin_capture_bytes_to_png(content: bytes) -> bytes:
    """將 MoreLogin 截圖位元組統一為 PNG（API 常回 JPEG）。"""
    if content.startswith(PNG_MAGIC):
        return content
    if content[:3] == b"\xff\xd8\xff":
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
    raise MoreLoginDeviceApiError(
        f"無法轉 PNG（bytes={len(content)} head={content[:8].hex()})"
    )


def morelogin_screenshot_png_bytes(
    env_id: str,
    *,
    timeout_seconds: float = 90.0,
    api_base: str | None = None,
) -> bytes:
    """透過 Open API 取得即時截圖 PNG 位元組。"""
    url = morelogin_cloudphone_url("screenCapBase64", api_base=api_base)
    resp = post_morelogin_json(
        url,
        {"id": _env_id_payload(env_id)},
        timeout_seconds=timeout_seconds,
    )
    code = int(resp.get("code") or 0)
    if code != 0:
        _raise_api_error("screenCapBase64", code=code, msg=str(resp.get("msg") or ""))
    data = resp.get("data")
    if not isinstance(data, dict):
        _raise_api_error(
            "screenCapBase64",
            code=code,
            msg="data 格式異常",
            payload={"data_type": type(data).__name__},
        )
    if not data.get("success", True):
        err = str(data.get("errorMessage") or data.get("msg") or "unknown")
        _raise_api_error("screenCapBase64", code=code, msg=err, payload=data)
    raw_bytes = decode_morelogin_capture_base64(str(data.get("captureBase64") or ""))
    return morelogin_capture_bytes_to_png(raw_bytes)


def morelogin_screenshot_download_url(
    env_id: str,
    *,
    timeout_seconds: float = 90.0,
    api_base: str | None = None,
) -> str:
    """透過 screenCap 取得臨時下載 URL（備援路徑）。"""
    url = morelogin_cloudphone_url("screenCap", api_base=api_base)
    resp = post_morelogin_json(
        url,
        {"id": _env_id_payload(env_id)},
        timeout_seconds=timeout_seconds,
    )
    code = int(resp.get("code") or 0)
    if code != 0:
        _raise_api_error("screenCap", code=code, msg=str(resp.get("msg") or ""))
    data = resp.get("data")
    if not isinstance(data, dict):
        _raise_api_error("screenCap", code=code, msg="data 格式異常")
    if not data.get("success", True):
        err = str(data.get("errorMessage") or data.get("status") or "unknown")
        _raise_api_error("screenCap", code=code, msg=err, payload=data)
    download_url = str(data.get("url") or "").strip()
    if not download_url:
        _raise_api_error("screenCap", code=code, msg="缺少 url", payload=data)
    return download_url


def morelogin_screenshot_png_bytes_via_url(
    env_id: str,
    *,
    timeout_seconds: float = 90.0,
    api_base: str | None = None,
) -> bytes:
    """screenCapBase64 失敗時的備援：下載臨時 URL。"""
    download_url = morelogin_screenshot_download_url(
        env_id,
        timeout_seconds=timeout_seconds,
        api_base=api_base,
    )
    try:
        with urllib.request.urlopen(download_url, timeout=timeout_seconds) as resp:
            content = resp.read()
    except Exception as exc:  # noqa: BLE001
        raise MoreLoginDeviceApiError(
            f"screenCap URL 下載失敗：{download_url[:120]}（{exc}）"
        ) from exc
    if len(content) < len(PNG_MAGIC) or not content.startswith(PNG_MAGIC):
        # screenCap URL 偶爾也回 JPEG
        return morelogin_capture_bytes_to_png(content)
    return content


def morelogin_execute_shell_command(
    env_id: str,
    command: str,
    *,
    timeout_seconds: float = 45.0,
    api_base: str | None = None,
) -> str:
    """透過 Open API 執行單條 shell 命令並回傳 stdout 文本。"""
    cmd = str(command or "").strip()
    if not cmd:
        raise MoreLoginDeviceApiError("exeCommand command 為空")
    url = morelogin_cloudphone_url("exeCommand", api_base=api_base)
    resp = post_morelogin_json(
        url,
        {"id": _env_id_payload(env_id), "command": cmd},
        timeout_seconds=timeout_seconds,
    )
    code = int(resp.get("code") or 0)
    if code != 0:
        _raise_api_error("exeCommand", code=code, msg=str(resp.get("msg") or ""))
    data = resp.get("data")
    if data is None:
        return ""
    if isinstance(data, str):
        return _require_supported_shell_output(cmd, data)
    if isinstance(data, dict):
        for key in ("output", "result", "data", "stdout"):
            if key in data:
                return _require_supported_shell_output(cmd, str(data[key] or ""))
        return _require_supported_shell_output(cmd, str(data))
    return _require_supported_shell_output(cmd, str(data))


def _require_supported_shell_output(command: str, output: str) -> str:
    """把「命令未放行」的哨兵回覆轉成異常，讓 hybrid 模式回退真實 ADB。"""

    text = str(output or "")
    if text.strip().casefold() in _SHELL_UNSUPPORTED_REPLIES:
        raise MoreLoginShellCommandUnsupportedError(
            f"MoreLogin Agent 未放行命令：{command}"
        )
    return text


def shell_args_to_command(args: list[str]) -> str:
    """將 adb shell 參數列表拼成 exeCommand 單行命令。"""
    parts: list[str] = []
    for arg in args:
        text = str(arg)
        if not text:
            parts.append('""')
        elif re.fullmatch(r"[\w.@:/_-]+", text):
            parts.append(text)
        else:
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
    return " ".join(parts)
