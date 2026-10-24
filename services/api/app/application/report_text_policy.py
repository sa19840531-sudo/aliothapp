"""App 报告面向调用方的文案策略。

App 质检报告禁止出现“灰产”“黑产”等规则口径外定性词；
生成与展示路径统一在此清洗，避免仅靠 Prompt 约束。
"""

from __future__ import annotations

import re
from typing import Any

# 简繁一并覆盖，避免报告漏洗
_APP_REPORT_FORBIDDEN_LABELS = re.compile(r"灰产|黑产|灰產|黑產")
_COLLAPSE_SPACES = re.compile(r"[ \t\u3000]{2,}")
_EMPTY_BRACKETS = re.compile(r"（\s*）|\(\s*\)|「\s*」|“\s*”|\"\s*\"")
_DUP_PAUSE = re.compile(r"[，、]{2,}")


def is_app_report_target(
    *,
    target_platform: str | None = None,
    product_code: str | None = None,
    target_url: str | None = None,
    task_config: dict[str, Any] | None = None,
) -> bool:
    """判断任务是否应按 App 报告文案策略清洗。"""
    del target_url  # 保留参数便于调用方统一传入，不参与判定
    platform = str(target_platform or "").strip().casefold()
    if platform == "app":
        return True
    config = task_config if isinstance(task_config, dict) else {}
    product = config.get("product") if isinstance(config.get("product"), dict) else {}
    code = str(
        product_code
        or product.get("product_code")
        or config.get("product_code")
        or ""
    ).strip()
    if code == "app-qc":
        return True
    target = config.get("_task_target") if isinstance(config.get("_task_target"), dict) else {}
    return str(target.get("platform") or "").strip().casefold() == "app"


def scrub_app_report_text(text: str | None) -> str | None:
    """从单段报告文案中移除灰产/黑产字样。"""
    if text is None:
        return None
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串或 None")
    if not text:
        return text
    cleaned = _APP_REPORT_FORBIDDEN_LABELS.sub("", text)
    cleaned = _COLLAPSE_SPACES.sub(" ", cleaned)
    cleaned = _EMPTY_BRACKETS.sub("", cleaned)
    cleaned = _DUP_PAUSE.sub("，", cleaned)
    return cleaned.strip()


def scrub_app_report_value(value: Any) -> Any:
    """递归清洗报告载荷中的字符串字段。"""
    if isinstance(value, str):
        return scrub_app_report_text(value)
    if isinstance(value, list):
        return [scrub_app_report_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_app_report_value(item) for item in value)
    if isinstance(value, dict):
        return {key: scrub_app_report_value(item) for key, item in value.items()}
    return value


def maybe_scrub_app_report_payload(
    payload: dict[str, Any],
    *,
    target_platform: str | None = None,
    product_code: str | None = None,
    target_url: str | None = None,
    task_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """App 任务报告整包清洗；非 App 原样返回。"""
    if not isinstance(payload, dict):
        raise TypeError("payload 必须是 dict")
    if not is_app_report_target(
        target_platform=target_platform,
        product_code=product_code,
        target_url=target_url,
        task_config=task_config,
    ):
        return payload
    scrubbed = scrub_app_report_value(payload)
    if not isinstance(scrubbed, dict):
        raise TypeError("报告清洗结果必须是 dict")
    return scrubbed
