"""原动作后有界只读采样；不新建会话、不导航、不增加动作或模型调用。"""

import asyncio
import hashlib
import time

from services.api.app.application.evidence_presentation import derive_h5_presentation
from services.api.app.application.h5_logical_surface_evidence import _is_visible_loading_indicator
from services.api.app.infrastructure.browser.h5_player_frames import (
    business_videos_in_view,
    video_region_digest,
)


async def collect_checkpoint_samples(
    page,
    *,
    read_snapshot,
    capture_frame,
    sleep=asyncio.sleep,
    clock=time.monotonic,
    retain_snapshots=False,
    business_video_only=False,
):
    samples, pngs, snapshots = [], [], []
    for index in range(2):
        if index:
            await sleep(0.6)
        snapshot = await read_snapshot(page)
        if retain_snapshots:
            snapshots.append(snapshot)
        observed_at_ms = clock() * 1000
        png, origin = await capture_frame(page, full_page=False)
        # 只保留原生媒体和完整性事实；没有原生媒体不猜正文/播放已经就绪。
        native = {
            key: snapshot.get(key)
            for key in (
                "url",
                "document",
                "document_state",
                "viewport",
                "collection_error",
                "capture_degraded",
                "budget_truncated",
                "element_limit_reached",
                "truncated",
            )
        }
        native["elements"] = [
            item for item in snapshot.get("elements", ()) if item.get("tag") == "video"
        ]
        if business_video_only:
            native["elements"] = business_videos_in_view(snapshot)
        videos = native["elements"]
        if len(videos) > 2:
            native["truncated"] = True
        native["elements"] = [
            {
                **{key: video.get(key) for key in (
                    "tag", "visible", "h5_media_instance_id", "rect", "bounds",
                    "h5_video_frame", "h5_player_presentation",
                ) if key in video},
                "media": {key: (video.get("media") or {}).get(key) for key in (
                    "paused", "current_time", "ready_state", "seeking",
                    "video_width", "video_height",
                )},
            }
            for video in videos[:2]
        ]
        native["loading"] = any(
            _is_visible_loading_indicator(item, snapshot.get("viewport") or {})
            for item in snapshot.get("elements", ())
        )
        samples.append(
            {
                "observed_at_ms": observed_at_ms,
                "screenshot_sha256": hashlib.sha256(png).hexdigest(),
                "frame_origin": origin,
                "snapshot": native,
                "video_frame": native["elements"][0].get("h5_video_frame")
                if len(native["elements"]) == 1
                else None,
                "video_region_sha256": video_region_digest(png, native),
                "presentation": derive_h5_presentation(ui_snapshot=snapshot).to_dict(),
            }
        )
        pngs.append(png)
    return {
        "schema_version": "checkpoint-observation.v1",
        "samples": samples,
        "pngs": pngs,
        **({"snapshots": snapshots} if retain_snapshots else {}),
    }
