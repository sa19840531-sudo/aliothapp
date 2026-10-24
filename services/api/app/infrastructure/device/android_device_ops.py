"""Android 设备操作可替换 Provider 合同；生产唯一实作仍是 AdbDeviceOps。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from services.api.app.infrastructure.device.adb_ops import ScreencapStageTrace

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class AndroidDeviceOps(Protocol):
    """四动作 + screencap 的最小设备合同；禁止并行双 Owner 写同一 Session。"""

    backend: str
    width: int
    height: int

    def tap(self, x: int, y: int) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, ms: int = 350) -> None: ...

    def back(self) -> None: ...

    def type_text(self, text: str) -> None: ...

    def screencap(self, dest: Path) -> ScreencapStageTrace: ...


# 计划口径别名：AdbProvider ≡ AndroidDeviceOps
AdbProvider = AndroidDeviceOps


class ShadowAndroidDeviceOps:
    """影子 Provider：只记录拟执行命令，零设备输入。"""

    def __init__(
        self,
        *,
        backend: str = "shadow",
        width: int = 1080,
        height: int = 1920,
        audit_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.width = width
        self.height = height
        self._audit_sink = audit_sink
        self.commands: list[dict[str, object]] = []

    def _record(self, operation: str, **payload: object) -> None:
        entry: dict[str, object] = {"operation": operation, **payload}
        self.commands.append(entry)
        if self._audit_sink is not None:
            self._audit_sink(entry)
        else:
            _LOGGER.info("shadow_adb op=%s payload=%s", operation, payload)

    def tap(self, x: int, y: int) -> None:
        self._record("tap", x=x, y=y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, ms: int = 350) -> None:
        self._record("swipe", x1=x1, y1=y1, x2=x2, y2=y2, ms=ms)

    def back(self) -> None:
        self._record("back")

    def type_text(self, text: str) -> None:
        self._record("type_text", text_len=len(str(text or "")))

    def screencap(self, dest: Path) -> ScreencapStageTrace:
        self._record("screencap", dest=str(dest))
        return ScreencapStageTrace(path_mode="shadow", png_bytes=0)
