"""从同一浏览器快照提取主体视频排行列表的有界结构事实。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit

_RANK_ROW_TOKEN = re.compile(
    r"(?:^|[-_])(?:rank|ranking|leaderboard)[-_](?:item|row|entry)(?:$|[-_])"
)
_RANK_LIST_TOKEN = re.compile(
    r"(?:^|[-_])(?:rank|ranking|leaderboard)[-_](?:list|grid|items)(?:$|[-_])"
)
_VIDEO_PATH_PARTS = frozenset({"video", "watch", "play", "movie", "film", "episode"})
_EXCLUDED_ANCESTORS = frozenset({"article", "aside", "footer", "nav"})


def ranked_video_row_count(
    snapshot: Mapping[str, object], *, origin: str | None, current_url: str | None
) -> int:
    """只计同一 main 排行容器内可见且指向同源视频详情的独立标题行。"""

    if not origin or not current_url or not isinstance(snapshot.get("elements"), list):
        return 0
    try:
        origin_parts = urlsplit(origin)
        current_parts = urlsplit(current_url)
    except ValueError:
        return 0
    if not _same_origin(origin_parts, current_parts):
        return 0
    native = snapshot.get("web_native_structure")
    if not isinstance(native, Mapping) or native.get("complete") is not True:
        return 0
    if type(native.get("native_video_count")) is not int or native["native_video_count"] != 0:
        return 0
    if any(_visible_primary_player(raw) for raw in snapshot["elements"]):
        return 0

    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for raw in snapshot["elements"]:
        if not isinstance(raw, Mapping) or not _primary_rank_title(raw):
            continue
        try:
            destination = urlsplit(urljoin(current_url, str(raw["href"])))
        except ValueError:
            continue
        if not _same_origin(destination, origin_parts):
            continue
        path_parts = [part.casefold() for part in destination.path.split("/") if part]
        if not path_parts or path_parts[0] not in _VIDEO_PATH_PARTS or len(path_parts) < 2:
            continue
        row = _rank_row_scope(raw)
        if row is None:
            continue
        group, row_selector = row
        # 同一行的封面和标题不能重复充当两条证据，查询参数也不产生新详情。
        destination_key = destination.path.rstrip("/").casefold()
        grouped[group].setdefault(row_selector, destination_key)

    distinct_targets_per_list = (
        len(set(rows.values())) for rows in grouped.values() if len(rows) >= 5
    )
    return max((count for count in distinct_targets_per_list if count >= 5), default=0)


def _primary_rank_title(raw: Mapping[str, object]) -> bool:
    if (
        str(raw.get("tag") or "").casefold() != "a"
        or raw.get("interactive") is not True
        or raw.get("visible") is not True
        or not str(raw.get("text") or "").strip()
        or not str(raw.get("href") or "").strip()
        or raw.get("web_ancestor_structure_complete") is not True
        or str(raw.get("web_obstruction_layer") or "").strip()
        or any(raw.get(key) is True for key in (
            "within_web_navigation", "within_navigation", "within_footer",
            "within_pagination", "within_breadcrumb", "is_advertisement",
        ))
    ):
        return False
    rect = raw.get("rect")
    if not isinstance(rect, Mapping) or not all(
        isinstance(rect.get(key), (int, float)) and rect[key] > 0
        for key in ("width", "height")
    ):
        return False
    raw_tokens = raw.get("ancestor_class_tokens")
    if not isinstance(raw_tokens, list):
        return False
    tokens = {
        str(token).casefold()
        for token in raw_tokens
        if isinstance(token, str)
    }
    return any(_RANK_ROW_TOKEN.search(token) for token in tokens) and any(
        _RANK_LIST_TOKEN.search(token) for token in tokens
    )


def _rank_row_scope(raw: Mapping[str, object]) -> tuple[str, str] | None:
    ancestors = raw.get("web_ancestor_structure")
    if not isinstance(ancestors, list) or not ancestors:
        return None
    if any(
        isinstance(item, Mapping)
        and str(item.get("tag") or "").casefold() in _EXCLUDED_ANCESTORS
        for item in ancestors
    ):
        return None
    if not any(
        isinstance(item, Mapping) and str(item.get("tag") or "").casefold() == "main"
        for item in ancestors
    ):
        return None
    repeated = [
        str(item.get("selector") or "")
        for item in ancestors
        if isinstance(item, Mapping)
        and type(item.get("repeat_group_size")) is int
        and item["repeat_group_size"] >= 5
        and str(item.get("selector") or "")
    ]
    if not repeated:
        return None
    row_selector = repeated[-1]
    if " > main > " not in row_selector or " > " not in row_selector:
        return None
    return row_selector.rsplit(" > ", 1)[0], row_selector


def _visible_primary_player(raw: object) -> bool:
    if not isinstance(raw, Mapping) or raw.get("visible") is not True:
        return False
    if any(raw.get(key) is True for key in (
        "within_repeated_structure", "within_web_navigation", "within_navigation",
        "within_footer", "is_advertisement",
    )):
        return False
    ancestors = raw.get("web_ancestor_structure")
    if isinstance(ancestors, list) and ancestors:
        tags = {
            str(item.get("tag") or "").casefold()
            for item in ancestors if isinstance(item, Mapping)
        }
        if "main" not in tags or tags & {"aside", "nav", "footer"}:
            return False
    elif "main > " not in str(raw.get("selector") or ""):
        return False
    tag = str(raw.get("tag") or "").casefold()
    if tag in {"video", "iframe"}:
        return True
    classes = raw.get("class_tokens")
    if not isinstance(classes, list):
        return False
    return bool({"player-root", "video-player", "xgplayer", "dplayer"} & {
        str(token).casefold() for token in classes if isinstance(token, str)
    })


def _same_origin(left, right) -> bool:
    try:
        return (
            left.scheme in {"http", "https"}
            and left.scheme == right.scheme
            and left.hostname is not None
            and left.hostname == right.hostname
            and left.port == right.port
        )
    except ValueError:
        return False


__all__ = ["ranked_video_row_count"]
