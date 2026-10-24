"""App 多角色补证：location_1／refreshed／attempt_N 投影到 evidence_capture_roles。

契约侧已声明这些角色；本模块负责从 Capture metadata.requirement_role
与 sibling Capture 解析出 Judge 可消费的角色映射，并提供写入约定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SUPPORTED_REQUIREMENT_ROLES = frozenset(
    {
        "before",
        "primary",
        "refreshed",
        "source",
        "location_1",
        "attempt_1",
        "attempt_2",
        "attempt_3",
    }
)


def normalize_requirement_role(raw: object | None) -> str | None:
    text = str(raw or "").strip().casefold()
    if not text:
        return None
    # 兼容 semantic_role／capture_role 别名。
    aliases = {
        "after_action": "primary",
        "before_action": "before",
        "popup_before_close": "before",
        "popup_after_close": "primary",
        "popup_reobserve": "refreshed",
        "surface_reobserve": "refreshed",
        "reload": "refreshed",
        "second_location": "location_1",
        "price_location_1": "location_1",
        "ad_outbound_source": "source",
    }
    text = aliases.get(text, text)
    if text in SUPPORTED_REQUIREMENT_ROLES:
        return text
    return None


def merge_evidence_capture_roles(
    *,
    primary_capture_id: str,
    before_capture_id: str | None = None,
    supplemental: Mapping[str, str] | None = None,
    requirement_role: str | None = None,
    requirement_capture_id: str | None = None,
) -> dict[str, str]:
    """合并 before/primary 与多角色补证；primary 必须指向主 Capture。"""

    roles: dict[str, str] = {"primary": primary_capture_id}
    if before_capture_id and before_capture_id != primary_capture_id:
        roles["before"] = before_capture_id
    if isinstance(supplemental, Mapping):
        for key, value in supplemental.items():
            role = normalize_requirement_role(key)
            capture_id = str(value or "").strip()
            if role and capture_id and role != "primary":
                roles[role] = capture_id
    extra_role = normalize_requirement_role(requirement_role)
    extra_id = str(requirement_capture_id or "").strip()
    if extra_role and extra_id and extra_role != "primary":
        roles[extra_role] = extra_id
    return roles


def extract_requirement_roles_from_capture_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    primary_capture_id: str,
) -> dict[str, str]:
    """从仓储行（含 metadata）抽出 requirement_role → capture_id。"""

    roles: dict[str, str] = {"primary": primary_capture_id}
    for row in rows:
        capture_id = str(row.get("capture_id") or row.get("id") or "").strip()
        if not capture_id:
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        role = normalize_requirement_role(
            metadata.get("requirement_role") or row.get("capture_role")
        )
        if role == "before" or (
            str(row.get("capture_role") or "") == "before_action"
        ):
            if capture_id != primary_capture_id:
                roles["before"] = capture_id
            continue
        if role and role not in {"primary", "before"}:
            roles[role] = capture_id
    return roles


INTERACTION_REFRESH_CAPTURE_GAP = "interaction_refresh_capture_missing"
_FEEDBACK_ACTIONS = frozenset({"click", "tap"})


def is_interaction_feedback_action(
    *,
    action: str,
    semantic_target: str | None,
) -> bool:
    """关注／收藏／点赞需要操作前、操作后、刷新后三张互不相同的画面。"""

    if str(action or "").strip().casefold() not in _FEEDBACK_ACTIONS:
        return False
    text = str(semantic_target or "")
    if any(token in text for token in ("关注", "收藏", "点赞")):
        return True
    lowered = text.casefold()
    return any(token in lowered for token in ("follow", "favorite", "favourite"))


def interaction_feedback_capture_set(
    *,
    before_capture_id: str,
    primary_capture_id: str,
    refreshed_capture_id: str | None,
) -> dict[str, Any]:
    """三张画面必须是不同 Capture。缺刷新帧只记缺口，不把操作后画面再当成刷新后。"""

    before_id = str(before_capture_id or "").strip()
    primary_id = str(primary_capture_id or "").strip()
    refreshed_id = str(refreshed_capture_id or "").strip()
    ids = [item for item in (before_id, primary_id, refreshed_id) if item]
    complete = bool(before_id and primary_id and refreshed_id and len(set(ids)) == 3)
    if complete:
        gap_codes: list[str] = []
    else:
        gap_codes = [INTERACTION_REFRESH_CAPTURE_GAP]
        refreshed_id = ""
    return {
        "required_roles": ["before", "primary", "refreshed"],
        "before_capture_id": before_id,
        "primary_capture_id": primary_id,
        "refreshed_capture_id": refreshed_id or None,
        "complete": complete,
        "gap_codes": gap_codes,
    }
