"""裝置後端選擇：ldplayer | google_emulator | redroid | cloud_phone。"""

from __future__ import annotations

import os

BACKEND_LDPLAYER = "ldplayer"
BACKEND_GOOGLE_EMULATOR = "google_emulator"
BACKEND_REDROID = "redroid"
BACKEND_CLOUD_PHONE = "cloud_phone"

# Demo 頁「模擬器」選項對應的實際後端（可用環境變數覆寫）
_DEFAULT_DEMO_EMULATOR_BACKEND = BACKEND_REDROID


def normalize_device_backend(raw: str | None) -> str | None:
    """把別名正規化為標準後端名；無法識別則回傳 None。"""
    key = (raw or "").strip().lower()
    if not key:
        return None
    if key in (
        BACKEND_GOOGLE_EMULATOR,
        "google",
        "android_emulator",
        "avd",
        "sdk_emulator",
    ):
        return BACKEND_GOOGLE_EMULATOR
    if key in (
        BACKEND_CLOUD_PHONE,
        "cloud-phone",
        "cloudphone",
        "remote_phone",
        "remote-phone",
    ):
        return BACKEND_CLOUD_PHONE
    if key in (BACKEND_REDROID, "remote_android", "remote-android"):
        return BACKEND_REDROID
    if key in (BACKEND_LDPLAYER, "ld", "leidian", "雷电", "雷電"):
        return BACKEND_LDPLAYER
    return None


def resolve_demo_emulator_backend() -> str:
    """Demo 頁「模擬器」對應的後端：預設 redroid，可用 ALIOTH_MOBILE_DEMO_EMULATOR_BACKEND 覆寫。"""
    raw = (os.getenv("ALIOTH_MOBILE_DEMO_EMULATOR_BACKEND") or "").strip().lower()
    normalized = normalize_device_backend(raw) if raw else None
    if normalized in (BACKEND_REDROID, BACKEND_GOOGLE_EMULATOR, BACKEND_LDPLAYER):
        return normalized
    return _DEFAULT_DEMO_EMULATOR_BACKEND


def resolve_device_backend() -> str:
    """解析當前裝置後端；預設 ldplayer，保持既有行為。"""
    normalized = normalize_device_backend(os.getenv("ALIOTH_DEVICE_BACKEND"))
    if normalized:
        return normalized
    # 未設定時：非 Windows 預設嘗試 google_emulator，方便 Linux 伺服器 POC
    if os.name != "nt":
        return BACKEND_GOOGLE_EMULATOR
    return BACKEND_LDPLAYER


def set_device_backend(raw: str) -> str:
    """執行期切換裝置後端（寫入進程環境變數，供 Demo 頁切換）。

    支援別名：emulator／模擬器 → resolve_demo_emulator_backend()。
    """
    key = (raw or "").strip().lower()
    if key in ("emulator", "模擬器", "sim", "simulator"):
        normalized = resolve_demo_emulator_backend()
    else:
        normalized = normalize_device_backend(key)
    if not normalized:
        raise ValueError(
            f"不支援的裝置後端：{raw!r}（可用 cloud_phone / redroid / google_emulator / ldplayer / emulator）"
        )
    os.environ["ALIOTH_DEVICE_BACKEND"] = normalized
    return normalized


def demo_backend_options() -> list[dict[str, str]]:
    """Demo 頁可選後端（對外只暴露雲手機／模擬器兩類）。"""
    emu = resolve_demo_emulator_backend()
    return [
        {"value": BACKEND_CLOUD_PHONE, "label": "雲手機", "mode": "cloud_phone"},
        {"value": emu, "label": "模擬器", "mode": "emulator"},
    ]


def is_remote_adb_backend(backend: str | None = None) -> bool:
    """是否為遠端 ADB 裝置面（ReDroid / 雲手機）。"""
    b = backend or resolve_device_backend()
    return b in (BACKEND_REDROID, BACKEND_CLOUD_PHONE)
