"""从同帧有界媒体摘要恢复被 UI 元素裁剪的详情主播放器事实。"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from services.api.app.application.web_logical_page import is_web_home_url
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.application.web_subject_region_scopes import SubjectRegions, valid_rect


@dataclass(frozen=True)
class RecoveredDetailMedia:
    regions: WebSubjectRegionFacts
    lower_cards_demoted: bool


def recover_truncated_detail_media(
    snapshot: Mapping[str, object],
    *,
    elements: Sequence[Mapping[str, object]],
    regions: WebSubjectRegionFacts,
    detail_candidates: Sequence[object],
    page_role: str | None,
    current_url: str | None,
) -> RecoveredDetailMedia | None:
    """仅在可信同帧原生视频已加载且详情 UI 被限额裁剪时补足分类事实。"""

    if (
        page_role != "detail"
        or not current_url
        or snapshot.get("url") != current_url
        or is_web_home_url(current_url)
        or snapshot.get("web_detail_candidate_priority_enabled") is not True
        or snapshot.get("truncated_reason") != "serialized_size_limit"
        or snapshot.get("media_truncated") is not False
        or regions.primary_subject_count != 0
        or regions.primary_player_count != 0
        or regions.primary_article_body_count != 0
        or regions.inline_article_media_count != 0
    ):
        return None
    native = snapshot.get("web_native_structure")
    if (
        not isinstance(native, Mapping)
        or native.get("scope") != "current_document_light_dom"
        or type(native.get("native_video_count")) is not int
    ):
        return None
    # 原生计数仅覆盖当前文档的 light DOM；不把 complete=False 推断为全页无其他视频。
    if native["native_video_count"] != 1:
        return None
    raw_media = snapshot.get("media")
    if not isinstance(raw_media, list):
        return None
    videos = [
        item for item in raw_media
        if isinstance(item, Mapping) and item.get("tag") == "video"
    ]
    if len(videos) != 1:
        return None
    video = videos[0]
    if video.get("media_ad_context_complete") is not True or any(
        video.get(key) is not False for key in (
        "is_advertisement",
        "within_navigation",
        "within_web_navigation",
        "within_repeated_structure",
        "within_footer",
        )
    ):
        return None
    hit_test = video.get("hit_test")
    ad_click_overlay = (
        isinstance(hit_test, Mapping)
        and hit_test.get("center_ad_click_overlay") is True
    )
    if (
        not isinstance(hit_test, Mapping)
        or hit_test.get("center_obstruction_layer") is not False
        or not (
            hit_test.get("center_hits_target") is True
            or hit_test.get("center_within_player_surface") is True
            or (
                ad_click_overlay
                and hit_test.get("center_pointer_interception") is True
                and hit_test.get("center_hits_target") is False
                and hit_test.get("center_within_player_surface") is False
            )
        )
        or (ad_click_overlay and (
            hit_test.get("center_hits_target") is not False
            or hit_test.get("center_within_player_surface") is not False
        ))
    ):
        return None
    selector = video.get("selector")
    if not isinstance(selector, str) or not selector.strip() or len(selector) > 512:
        return None
    if any(
        item.get("tag") == "video" and item.get("selector") == selector
        for item in elements
        if isinstance(item, Mapping)
    ):
        return None
    state = video.get("state")
    if not isinstance(state, Mapping) or type(state.get("ready_state")) is not int:
        return None
    duration = state.get("duration")
    if (
        state["ready_state"] < 2
        or type(duration) not in {int, float}
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None
    rect = valid_rect(video.get("bounds"))
    viewport = snapshot.get("viewport")
    if rect is None or not isinstance(viewport, Mapping):
        return None
    width, height = viewport.get("width"), viewport.get("height")
    if (
        type(width) not in {int, float}
        or type(height) not in {int, float}
        or not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
    ):
        return None
    x, y, media_width, media_height = rect
    visible_width = max(0, min(x + media_width, width) - max(x, 0))
    visible_height = max(0, min(y + media_height, height) - max(y, 0))
    if (
        media_width < max(480, width * 0.4)
        or media_height < max(270, height * 0.35)
        or media_width * media_height < width * height * 0.2
        or visible_width * visible_height < media_width * media_height * 0.8
    ):
        return None
    scopes = SubjectRegions(elements)
    if scopes.within(selector, scopes.secondary | scopes.cards):
        return None

    lower_cards_demoted = _all_cards_below_video(
        scopes, detail_candidates=detail_candidates, video_bottom=y + media_height,
    )
    selector_summary = dict(regions.selector_summaries)
    token_summary = dict(regions.token_summaries)
    player_ref = "video:sha256:" + hashlib.sha256(selector.encode("utf-8")).hexdigest()[:24]
    selector_summary["primary_subject"] = (player_ref,)
    selector_summary["primary_media_player"] = (player_ref,)
    token_summary["primary_subject"] = ("video",)
    token_summary["primary_media_player"] = (
        "media_geometry_dominant", "native_media_ready_state", "video",
    )
    if lower_cards_demoted:
        selector_summary["secondary_recommendations"] = tuple(sorted(set(
            selector_summary.get("secondary_recommendations", ())
            + selector_summary.get("primary_cards", ())
        )))[:32]
        selector_summary["primary_cards"] = ()
        token_summary["secondary_recommendations"] = tuple(sorted(set(
            token_summary.get("secondary_recommendations", ())
            + token_summary.get("primary_cards", ())
        )))[:32]
        token_summary["primary_cards"] = ()
    return RecoveredDetailMedia(
        regions=replace(
            regions,
            primary_subject_count=1,
            primary_subject_kind="video",
            primary_player_count=1,
            repeated_primary_card_count=(
                0 if lower_cards_demoted else regions.repeated_primary_card_count
            ),
            secondary_recommendation_card_count=(
                regions.secondary_recommendation_card_count
                + (regions.repeated_primary_card_count if lower_cards_demoted else 0)
            ),
            selector_summaries=tuple(sorted(selector_summary.items())),
            token_summaries=tuple(sorted(token_summary.items())),
        ),
        lower_cards_demoted=lower_cards_demoted,
    )


def _all_cards_below_video(
    scopes: SubjectRegions, *, detail_candidates: Sequence[object], video_bottom: float
) -> bool:
    cards = scopes.primary_cards
    if not cards:
        return not detail_candidates
    if not detail_candidates or any(
        not any(scopes.within(str(getattr(item, "selector", "")), {card}) for card in cards)
        for item in detail_candidates
    ):
        return False
    for card in cards:
        positions = [
            rect[1]
            for selector, node in scopes.nodes.items()
            if scopes.within(selector, {card})
            and (rect := valid_rect(node.get("rect"))) is not None
        ]
        if not positions or min(positions) < video_bottom + 64:
            return False
    return True
