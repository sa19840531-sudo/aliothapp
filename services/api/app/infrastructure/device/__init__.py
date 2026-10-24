"""原生設備自動化基礎設施層（與 browser 並列）。

後端：
- ldplayer：雷電（Windows）
- google_emulator：官方 Android Emulator（Linux/SDK）
- redroid：Docker ReDroid 遠端 ADB
- cloud_phone：雲手機遠端 ADB（與 redroid 同協定）
"""

from services.api.app.infrastructure.device.backend import (
    BACKEND_CLOUD_PHONE,
    BACKEND_GOOGLE_EMULATOR,
    BACKEND_LDPLAYER,
    BACKEND_REDROID,
    demo_backend_options,
    is_remote_adb_backend,
    resolve_demo_emulator_backend,
    resolve_device_backend,
    set_device_backend,
)

__all__ = [
    "BACKEND_CLOUD_PHONE",
    "BACKEND_GOOGLE_EMULATOR",
    "BACKEND_LDPLAYER",
    "BACKEND_REDROID",
    "demo_backend_options",
    "is_remote_adb_backend",
    "resolve_demo_emulator_backend",
    "resolve_device_backend",
    "set_device_backend",
]
