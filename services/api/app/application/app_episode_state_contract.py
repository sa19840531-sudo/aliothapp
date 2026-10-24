"""剧集同页状态：季／集／播放源扩展 PageStateRevision，不新建 page type。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

APP_EPISODE_STATE_CONTRACT_VERSION = "app-episode-state.v1"
PAGE_STATE_REVISION_ENTITY = "PageStateRevision"


def build_episode_state_revision(
    *,
    page_node_id: object,
    season: object = None,
    episode: object = None,
    play_source: object = None,
    selected_label: object = None,
    parent_capture_id: object = None,
) -> dict[str, Any]:
    season_text = str(season or "").strip() or None
    episode_text = str(episode or "").strip() or None
    source_text = str(play_source or "").strip() or None
    label = str(selected_label or "").strip() or None
    fingerprint_parts = [p for p in (season_text, episode_text, source_text, label) if p]
    return {
        "protocol_version": APP_EPISODE_STATE_CONTRACT_VERSION,
        "entity": PAGE_STATE_REVISION_ENTITY,
        "revision_kind": "episode",
        "page_node_id": str(page_node_id or "").strip() or None,
        "season": season_text,
        "episode": episode_text,
        "play_source": source_text,
        "selected_label": label,
        "parent_capture_id": str(parent_capture_id or "").strip() or None,
        "state_fingerprint": "|".join(fingerprint_parts) if fingerprint_parts else None,
        "creates_new_page_node": False,
        "creates_new_page_type": False,
    }


def episode_candidates_from_controls(
    controls: Sequence[Mapping[str, Any]] | None,
    *,
    page_node_id: object,
    parent_capture_id: object = None,
) -> list[dict[str, Any]]:
    """从 UI／无障碍控件列表生成不限量剧集候选（仍受授权／去重约束由调用方处理）。"""

    out: list[dict[str, Any]] = []
    for item in controls or ():
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or item.get("role") or "").casefold()
        label = item.get("label") or item.get("text") or item.get("content_desc")
        if kind not in {"episode", "season", "play_source", "集", "季"} and not (
            isinstance(label, str)
            and any(token in label for token in ("第", "集", "季", "EP", "ep"))
        ):
            # 宽松：显式 episode 字段也可
            if item.get("episode") is None and item.get("season") is None:
                continue
        out.append(
            build_episode_state_revision(
                page_node_id=page_node_id,
                season=item.get("season"),
                episode=item.get("episode") or label,
                play_source=item.get("play_source"),
                selected_label=label,
                parent_capture_id=parent_capture_id,
            )
        )
    return out


def same_page_episode_revision(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    return (
        previous.get("page_node_id") == current.get("page_node_id")
        and previous.get("entity") == PAGE_STATE_REVISION_ENTITY
        and current.get("entity") == PAGE_STATE_REVISION_ENTITY
        and current.get("creates_new_page_node") is False
    )
