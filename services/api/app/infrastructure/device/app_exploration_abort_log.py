"""App 探索因错误未跑完时的审计日志：与 ``app_tap_audit`` 同风格，便于检索。

日志关键字：``app_exploration_abort``。典型 stage：

1. ``stage=technical_failure`` — 技术失败事实落库前／旁路记录
2. ``stage=bootstrap`` — Session／Install／Open／首帧等启动阶段打断
3. ``stage=exploration`` — 探索循环中技术终态
4. ``stage=policy`` / ``stage=runtime`` — 策略或未分类运行时异常
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

_LOGGER = logging.getLogger("alioth.app.exploration_abort")


def log_app_exploration_abort(stage: str, **fields: Any) -> None:
    """以稳定可检索格式输出「探索未跑完」审计。

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
        elif isinstance(value, bool):
            parts.append(f"{key}={'true' if value else 'false'}")
        else:
            text = str(value).replace("\n", " ").strip()
            if len(text) > 240:
                text = text[:237] + "..."
            parts.append(f"{key}={text}")
    _LOGGER.info("app_exploration_abort %s", " ".join(parts))
