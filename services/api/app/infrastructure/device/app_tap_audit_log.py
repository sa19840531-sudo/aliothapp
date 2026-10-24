"""App 探索点击坐标审计日志：统一前缀，便于排查偏移。

日志关键字：``app_tap_audit``。典型链路：

1. ``stage=capture_target`` — Runtime 从候选算出 capture_px
2. ``stage=resolve`` — Resolver 映射到 device_px（含 letterbox／旋转审计）
3. ``stage=driver_execute`` — SurfaceDriver 即将下发点击
4. ``stage=adb_tap`` — ADB ``input tap``／短 swipe 实际坐标
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

_LOGGER = logging.getLogger("alioth.app.tap_audit")


def log_app_tap_audit(stage: str, **fields: Any) -> None:
    """以稳定可检索格式输出点击坐标审计。

    字段顺序固定：stage 在前，其余按 key 排序，避免日志噪声难 diff。
    """
    parts = [f"stage={stage}"]
    for key in sorted(fields):
        value = fields[key]
        if value is None:
            continue
        if isinstance(value, Mapping):
            compact = ",".join(
                f"{sub}={value[sub]}" for sub in sorted(value) if value[sub] is not None
            )
            parts.append(f"{key}={{{compact}}}")
        elif isinstance(value, float):
            parts.append(f"{key}={value:.6g}")
        else:
            text = str(value).replace("\n", " ").strip()
            if len(text) > 240:
                text = text[:237] + "..."
            parts.append(f"{key}={text}")
    _LOGGER.info("app_tap_audit %s", " ".join(parts))
