"""正式 H5 质检读取本帧就绪事实，模型页型不能替代物理采集证明。"""

import json
from collections.abc import Mapping

from services.api.app.application.h5_video_publication import video_publication_is_ready


def h5_capture_is_inspection_ready(
    metadata: object,
    *,
    page_state: object,
    page_type: object = None,
    screenshot_checksums=None,
    primary_screenshot_checksums=None,
) -> bool:
    """动态但可判定的正文允许发布，缺事实、诊断帧和阻塞加载一律留作不足。"""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    if not isinstance(metadata, Mapping):
        return False
    metrics = metadata.get("pipeline_metrics")
    if not isinstance(metrics, Mapping) or metrics.get("page_inspection_ready") is not True:
        return False
    diagnostics = metrics.get("page_settle_diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("blocking_reasons"):
        return False
    video = metrics.get("h5_video_publication")
    if (video is not None or str(page_type or "").casefold() == "video_detail") and not (
        video_publication_is_ready(
            video,
            screenshot_checksums=screenshot_checksums,
            primary_screenshot_checksums=primary_screenshot_checksums,
        )
    ):
        return False
    return (
        str(page_state or "").casefold() not in {"loading", "error", "load_failed"}
        and metadata.get("diagnostic_capture") is not True
        and metrics.get("diagnostic_capture") is not True
    )
