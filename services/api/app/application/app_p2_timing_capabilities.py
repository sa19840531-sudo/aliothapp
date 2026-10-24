"""App P2-10：Toast 时序观察与 network_fault 能力声明。

App 端无设备级断网注入；明确 stub，禁止伪造成功。
Toast／spec_48 依赖 timed_capture 连拍后的 visible_text／ui_snapshot 短窗命中。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

APP_NETWORK_FAULT_UNSUPPORTED = "network_fault_unsupported_on_app"
APP_TOAST_OBSERVATION_KEY = "toast_observation"


def app_network_fault_capability() -> dict[str, object]:
    """返回 App network_fault 能力声明（供命令／规则能力混合层读取）。"""

    return {
        "supported": False,
        "result": APP_NETWORK_FAULT_UNSUPPORTED,
        "evidence_status": "evidence_insufficient",
        "reason": (
            "App Runtime 无设备级 network_fault 注入；"
            "请使用弱网代理近似或人工故障截图，禁止宣称断网成功。"
        ),
    }


def build_toast_observation(
    *,
    frames: Sequence[Mapping[str, Any]] | None,
    markers: Sequence[str] = (),
    window_ms: int = 2000,
) -> dict[str, object] | None:
    """从 timed_capture 帧扫描 Toast／瞬时文案；无命中返回 None。"""

    if not frames:
        return None
    needles = tuple(
        str(item).strip()
        for item in (
            markers
            or (
                "成功",
                "失败",
                "已复制",
                "网络异常",
                "请稍候",
                "toast",
            )
        )
        if str(item).strip()
    )
    hits: list[dict[str, object]] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            continue
        blob = " ".join(
            (
                str(frame.get("visible_text") or ""),
                str(frame.get("ocr_text") or ""),
                str(frame.get("ui_snapshot") or "")[:2000],
            )
        )
        matched = [needle for needle in needles if needle.casefold() in blob.casefold()]
        if matched:
            hits.append(
                {
                    "frame_index": index,
                    "markers": matched,
                    "capture_id": str(frame.get("capture_id") or "") or None,
                }
            )
    if not hits:
        return None
    return {
        "protocol_version": "app-toast-observation.v1",
        "window_ms": max(0, int(window_ms)),
        "hit_count": len(hits),
        "hits": hits,
    }
