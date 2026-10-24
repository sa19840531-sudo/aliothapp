"""星語 Official 動作／座標正規化（對齊青雲 Explorer 合同）。"""

from __future__ import annotations

from typing import Any


def normalize_action_type(action_type: str | None) -> str:
    """青雲能力清單使用 ``click``；星語裝置原語統一為 ``tap``。"""
    op = (action_type or "").strip().lower()
    if op == "click":
        return "tap"
    return op


def normalize_execute_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """把青雲 ``resolved_target``／扁平座標整理成 Resolver 可讀的 ``point``。

    青雲 ``AppInteractionExecutor`` 會注入::

        parameters.resolved_target = {
            "x": <capture_px>,
            "y": <capture_px>,
            "space": "capture_px",
            "base_width": <capture_w>,
            "base_height": <capture_h>,
        }

    截圖像素與裝置像素可能不一致（例如雲手機 API 預覽圖縮小）。有
    ``base_width``／``base_height`` 時按截圖座標系等比映射到裝置；舊契約僅
    ``{x,y}`` 時仍標記 ``device_px`` 以保持相容。
    """
    params = dict(parameters or {})
    if isinstance(params.get("point"), dict):
        return params

    resolved = params.get("resolved_target")
    if isinstance(resolved, dict) and "x" in resolved and "y" in resolved:
        space = str(resolved.get("space") or "").strip()
        base_w = resolved.get("base_width")
        base_h = resolved.get("base_height")
        has_capture_base = (
            not isinstance(base_w, bool)
            and not isinstance(base_h, bool)
            and isinstance(base_w, int)
            and isinstance(base_h, int)
            and base_w > 0
            and base_h > 0
        )
        if space == "capture_px" or (has_capture_base and space not in {"device_px", "normalized_01"}):
            if not has_capture_base:
                # capture_px 但缺基準尺寸時不可冒充 device_px，交給 Resolver 用 canonical 基準會偏點
                params["point"] = {
                    "x": resolved["x"],
                    "y": resolved["y"],
                    "space": "capture_px",
                }
                return params
            params["point"] = {
                "x": resolved["x"],
                "y": resolved["y"],
                "space": "capture_px",
            }
            params["base_width"] = int(base_w)
            params["base_height"] = int(base_h)
            params["display"] = {"width": int(base_w), "height": int(base_h)}
            return params
        params["point"] = {
            "x": resolved["x"],
            "y": resolved["y"],
            "space": space or "device_px",
        }
        return params

    if "x" in params and "y" in params:
        params["point"] = {
            "x": params["x"],
            "y": params["y"],
            "space": str(
                params.get("coordinate_space") or params.get("space") or "device_px"
            ),
        }
    return params
