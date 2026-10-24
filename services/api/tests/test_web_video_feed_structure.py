"""连续视频信息流的同帧结构证据，不借用全页推荐链接。"""

from __future__ import annotations

from copy import deepcopy

import pytest


def _feed_snapshot() -> dict[str, object]:
    wrapper = "main > div.video-region > div.swiper > div.swiper-wrapper"

    def video(*, slide: str, y: int) -> dict[str, object]:
        return {
            "tag": "video",
            "visible": True,
            "interactive": True,
            "rect": {"x": 160, "y": y, "width": 1700, "height": 1020},
            "selector": f"{wrapper} > div.swiper-slide-{slide} > video",
            "ancestor_class_tokens": [
                "swiper-wrapper", "swiper-slide", f"swiper-slide-{slide}",
            ],
            "web_ancestor_structure": [
                {"tag": "div", "selector": f"{wrapper} > div.swiper-slide-{slide}"},
                {"tag": "div", "selector": wrapper},
            ],
        }

    return {
        "viewport": {"width": 1920, "height": 1080},
        "elements": [video(slide="active", y=60), video(slide="next", y=1080)],
    }


def test_adjacent_fullscreen_video_slides_prove_continuous_feed() -> None:
    from services.api.app.application.web_page_classification import web_page_structure_facts
    from services.api.app.application.web_video_feed_structure import (
        continuous_video_feed_count,
    )

    assert continuous_video_feed_count(_feed_snapshot()) == 2
    assert web_page_structure_facts(_feed_snapshot()).continuous_video_feed_count == 2


@pytest.mark.parametrize(
    "case",
    (
        "no_next", "different_container", "recommendation_carousel",
        "horizontal_carousel", "no_next_video", "obstructed_active",
    ),
)
def test_unrelated_or_untrusted_videos_do_not_prove_feed(case: str) -> None:
    from services.api.app.application.web_video_feed_structure import (
        continuous_video_feed_count,
    )

    snapshot = deepcopy(_feed_snapshot())
    active, next_item = snapshot["elements"]
    if case == "no_next":
        next_item["ancestor_class_tokens"] = ["swiper-wrapper", "swiper-slide"]
    elif case == "different_container":
        next_item["web_ancestor_structure"][1]["selector"] += "-recommendations"
    elif case == "recommendation_carousel":
        active["rect"] = {"x": 160, "y": 60, "width": 300, "height": 170}
        next_item["rect"] = {"x": 160, "y": 230, "width": 300, "height": 170}
    elif case == "horizontal_carousel":
        next_item["rect"]["x"] = 1860
        next_item["rect"]["y"] = 60
    elif case == "no_next_video":
        next_item["tag"] = "div"
    elif case == "obstructed_active":
        active["occluded"] = True

    assert continuous_video_feed_count(snapshot) == 0
