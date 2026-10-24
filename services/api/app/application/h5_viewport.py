"""H5 V2 探索链路的固定移动端视口策略。"""

from __future__ import annotations

H5_VIEWPORT_WIDTH = 390
H5_VIEWPORT_HEIGHT = 844


def h5_viewport_parameters() -> dict[str, int]:
    """返回独立参数字典，避免各动作链路共享并修改全局状态。"""

    return {
        "viewport_width": H5_VIEWPORT_WIDTH,
        "viewport_height": H5_VIEWPORT_HEIGHT,
    }


__all__ = [
    "H5_VIEWPORT_HEIGHT",
    "H5_VIEWPORT_WIDTH",
    "h5_viewport_parameters",
]
