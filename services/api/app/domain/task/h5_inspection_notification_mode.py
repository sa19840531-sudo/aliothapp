"""H5 证据到规则质检通知链的锁定模式。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class H5InspectionNotificationMode(StrEnum):
    """任务创建时冻结的 H5 通知链运行模式。"""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCED = "enforced"


def parse_h5_inspection_notification_mode(
    config: Mapping[str, object] | None,
) -> H5InspectionNotificationMode:
    """读取持久化模式；历史任务缺字段时保持旧同步链。"""

    value = (config or {}).get("h5_inspection_notification_mode")
    if value is None:
        return H5InspectionNotificationMode.OFF
    try:
        return H5InspectionNotificationMode(str(value))
    except ValueError as exc:
        raise ValueError(f"h5_inspection_notification_mode 不受支持: {value}") from exc


def lock_h5_inspection_notification_mode(
    config: Mapping[str, object],
    mode: H5InspectionNotificationMode,
) -> dict[str, object]:
    """首次写入模式；同值幂等，禁止运行中任务改链。"""

    if not isinstance(mode, H5InspectionNotificationMode):
        raise TypeError("mode 必须为 H5InspectionNotificationMode")
    locked = dict(config)
    current = locked.get("h5_inspection_notification_mode")
    if current is not None and str(current) != mode.value:
        raise ValueError("h5_inspection_notification_mode 已锁定，不可改写")
    locked["h5_inspection_notification_mode"] = mode.value
    return locked
