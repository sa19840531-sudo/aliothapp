"""App 定向发现审计：区分「APP 不含」与「本轮未探索到」。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

APP_DIRECTED_DISCOVERY_PROTOCOL_VERSION = "app-directed-discovery.v1"
APP_DIRECTED_DISCOVERY_ATTEMPTS_KEY = "directed_discovery_attempts"

# 适用性加严：无定向负面证据不得默认 not_applicable
HARD_APPLICABILITY_PAGE_TYPES = frozenset(
    {
        "payment",
        "post_publish",
        "video_detail",
    }
)

# 硬页型 ← 定向搜寻的来源业务页
HARD_PAGE_SOURCE_TYPES: dict[str, frozenset[str]] = {
    "payment": frozenset({"profile"}),
    "post_publish": frozenset({"post_list"}),
    "video_detail": frozenset({"video_list", "home"}),
}

_GUIDE_PAGE_MARKERS = (
    "安卓下载",
    "苹果下载",
    "安卓手机下载",
    "苹果手机下载",
    "安卓版下载",
    "苹果版下载",
    "ios下载",
    "macos下载",
    "windows下载",
    "guide_page",
    "下载引导",
)

_OUTCOME_ENTRY_SEEN = "entry_seen"
_OUTCOME_NO_ENTRY = "no_entry"
_OUTCOME_CAPABILITY_BLOCKED = "capability_blocked"
_OUTCOME_SEEK_FAILED = "seek_failed"
_OUTCOME_TERMINAL = "terminal"


def directed_discovery_attempts_from_metadata(
    metadata: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if not isinstance(metadata, Mapping):
        return []
    raw = metadata.get(APP_DIRECTED_DISCOVERY_ATTEMPTS_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, Mapping):
            result.append({str(key): value for key, value in item.items()})
    return result


def append_directed_discovery_attempt(
    attempts: Sequence[Mapping[str, object]],
    *,
    page_type: str,
    source_page_type: str,
    source_capture_id: str | None,
    outcome: str,
    blocker_code: str | None = None,
    candidate_id: str | None = None,
    observed_at: datetime | str | None = None,
) -> list[dict[str, object]]:
    """单调追加定向发现记录；同 capture+page+outcome 去重。"""

    normalized_page = page_type.strip().casefold()
    normalized_source = source_page_type.strip().casefold()
    capture = (source_capture_id or "").strip()
    result = [dict(item) for item in attempts]
    for item in result:
        if (
            str(item.get("page_type") or "").casefold() == normalized_page
            and str(item.get("outcome") or "") == outcome
            and str(item.get("source_capture_id") or "") == capture
        ):
            return result
    entry: dict[str, object] = {
        "protocol_version": APP_DIRECTED_DISCOVERY_PROTOCOL_VERSION,
        "page_type": normalized_page,
        "source_page_type": normalized_source,
        "source_capture_id": capture or None,
        "outcome": outcome,
    }
    if blocker_code:
        entry["blocker_code"] = blocker_code
    if candidate_id:
        entry["candidate_id"] = candidate_id
    if observed_at is not None:
        entry["observed_at"] = (
            observed_at.isoformat()
            if isinstance(observed_at, datetime)
            else str(observed_at)
        )
    result.append(entry)
    return result


def has_directed_outcome(
    attempts: Sequence[Mapping[str, object]],
    *,
    page_type: str,
    outcomes: frozenset[str] | set[str],
) -> bool:
    normalized = page_type.strip().casefold()
    allowed = {item for item in outcomes}
    return any(
        str(item.get("page_type") or "").casefold() == normalized
        and str(item.get("outcome") or "") in allowed
        for item in attempts
    )


def merge_blocked_page_types(
    prior: Sequence[str],
    *,
    extra: Sequence[str],
) -> list[str]:
    """blocked_page_types 单调累加。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in (*prior, *extra):
        normalized = str(value).strip().casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def hard_targets_missing_entry_on_source(
    *,
    current_page_type: str | None,
    missing_page_types: set[str],
    analysis_has_entry_for: set[str],
) -> tuple[str, ...]:
    """来源页稳定且缺硬页型、本帧无验签入口时，可记 no_entry。"""

    if not isinstance(current_page_type, str) or not current_page_type.strip():
        return ()
    current = current_page_type.strip().casefold()
    proven: list[str] = []
    for page_type, sources in HARD_PAGE_SOURCE_TYPES.items():
        if page_type not in missing_page_types:
            continue
        if current not in sources:
            continue
        if page_type in analysis_has_entry_for:
            continue
        proven.append(page_type)
    return tuple(proven)


def guide_page_markers_present(
    *,
    page_type: str | None,
    surface_facts: Mapping[str, object] | None,
    visible_texts: Sequence[str] = (),
) -> bool:
    """轻量识别下载引导页语义（不替代 H5 全量确定性检测）。"""

    if isinstance(page_type, str) and page_type.strip().casefold() == "guide_page":
        return True
    blobs: list[str] = []
    if isinstance(surface_facts, Mapping):
        for key in ("page_labels", "visible_texts", "ocr_texts", "risk_markers"):
            value = surface_facts.get(key)
            if isinstance(value, str):
                blobs.append(value)
            elif isinstance(value, (list, tuple)):
                blobs.extend(str(item) for item in value)
    blobs.extend(str(item) for item in visible_texts)
    haystack = " ".join(blobs).casefold()
    return any(marker.casefold() in haystack for marker in _GUIDE_PAGE_MARKERS)


def record_entry_seen_attempts(
    attempts: Sequence[Mapping[str, object]],
    *,
    discovered_page_types: Sequence[str],
    source_page_type: str | None,
    source_capture_id: str | None,
    observed_at: datetime | str | None = None,
) -> list[dict[str, object]]:
    result = [dict(item) for item in attempts]
    source = (source_page_type or "unknown").strip().casefold() or "unknown"
    for page_type in discovered_page_types:
        normalized = page_type.strip().casefold()
        if normalized not in HARD_APPLICABILITY_PAGE_TYPES and normalized != "guide_page":
            continue
        result = append_directed_discovery_attempt(
            result,
            page_type=normalized,
            source_page_type=source,
            source_capture_id=source_capture_id,
            outcome=_OUTCOME_ENTRY_SEEN,
            observed_at=observed_at,
        )
    return result


def record_no_entry_attempts(
    attempts: Sequence[Mapping[str, object]],
    *,
    page_types: Sequence[str],
    source_page_type: str,
    source_capture_id: str | None,
    observed_at: datetime | str | None = None,
) -> list[dict[str, object]]:
    result = [dict(item) for item in attempts]
    for page_type in page_types:
        result = append_directed_discovery_attempt(
            result,
            page_type=page_type,
            source_page_type=source_page_type,
            source_capture_id=source_capture_id,
            outcome=_OUTCOME_NO_ENTRY,
            observed_at=observed_at,
        )
    return result


def record_capability_blocked_attempts(
    attempts: Sequence[Mapping[str, object]],
    *,
    page_types: Sequence[str],
    source_page_type: str,
    source_capture_id: str | None,
    blocker_code: str,
    observed_at: datetime | str | None = None,
) -> list[dict[str, object]]:
    result = [dict(item) for item in attempts]
    for page_type in page_types:
        result = append_directed_discovery_attempt(
            result,
            page_type=page_type,
            source_page_type=source_page_type,
            source_capture_id=source_capture_id,
            outcome=_OUTCOME_CAPABILITY_BLOCKED,
            blocker_code=blocker_code,
            observed_at=observed_at,
        )
    return result


def record_seek_attempt(
    attempts: Sequence[Mapping[str, object]],
    *,
    page_type: str,
    source_page_type: str,
    source_capture_id: str | None,
    candidate_id: str | None,
    outcome: str = _OUTCOME_ENTRY_SEEN,
    observed_at: datetime | str | None = None,
) -> list[dict[str, object]]:
    return append_directed_discovery_attempt(
        attempts,
        page_type=page_type,
        source_page_type=source_page_type,
        source_capture_id=source_capture_id,
        outcome=outcome,
        candidate_id=candidate_id,
        observed_at=observed_at,
    )


def record_guide_page_attempt(
    attempts: Sequence[Mapping[str, object]],
    *,
    source_page_type: str | None,
    source_capture_id: str | None,
    outcome: str = _OUTCOME_ENTRY_SEEN,
    observed_at: datetime | str | None = None,
) -> list[dict[str, object]]:
    return append_directed_discovery_attempt(
        attempts,
        page_type="guide_page",
        source_page_type=source_page_type or "unknown",
        source_capture_id=source_capture_id,
        outcome=outcome,
        observed_at=observed_at,
    )


# 导出 outcome 常量供测试／调用方使用
OUTCOME_ENTRY_SEEN = _OUTCOME_ENTRY_SEEN
OUTCOME_NO_ENTRY = _OUTCOME_NO_ENTRY
OUTCOME_CAPABILITY_BLOCKED = _OUTCOME_CAPABILITY_BLOCKED
OUTCOME_SEEK_FAILED = _OUTCOME_SEEK_FAILED
OUTCOME_TERMINAL = _OUTCOME_TERMINAL
