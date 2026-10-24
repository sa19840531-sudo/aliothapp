"""持久菜单谱系的共享只读验证，避免恢复层与证据层规则漂移。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


def persisted_menu_lineages_are_valid(
    ledger: Sequence[Mapping[str, object]],
) -> bool:
    """验证运行时候选完整父链；历史 unavailable 只允许缺父的保守记录。"""
    runtime_rows = {
        _text(row.get("menu_entry_id")): row
        for row in ledger
        if row.get("source_kind") == "runtime_discovered"
    }
    for entry_id, row in runtime_rows.items():
        depth = row.get("menu_depth")
        path = row.get("exploration_path")
        logical_identity = _text(row.get("logical_target_identity"))
        parent_id = _text(row.get("parent_menu_entry_id"))
        parent_logical_identity = _text(row.get("parent_logical_target_identity"))
        if (
            isinstance(depth, bool)
            or not isinstance(depth, int)
            or depth < 1
            or not isinstance(path, list | tuple)
            or not path
            or any(not isinstance(item, str) or not item.strip() for item in path)
            or not re.fullmatch(r"[0-9a-f]{64}", logical_identity)
            or entry_id != "discovery:" + logical_identity
            or _text(path[-1]) != _runtime_menu_label(row)
        ):
            return False
        if not parent_id:
            if depth == 1 and not parent_logical_identity:
                continue
            if row.get("error_code") == "WEB_MENU_LINEAGE_UNAVAILABLE":
                continue
            return False
        parent = runtime_rows.get(parent_id)
        if parent is None:
            return False
        parent_path = parent.get("exploration_path")
        if not isinstance(parent_path, list | tuple):
            return False
        if (
            depth != int(parent.get("menu_depth") or 0) + 1
            or len(path) <= len(parent_path)
            or tuple(path[: len(parent_path)]) != tuple(parent_path)
            or parent_logical_identity != _text(parent.get("logical_target_identity"))
        ):
            return False
    return True


def _text(value: object) -> str:
    return str(value or "").strip()


def _runtime_menu_label(row: Mapping[str, object]) -> str:
    discovery_manifest = row.get("discovery_manifest")
    if isinstance(discovery_manifest, Mapping):
        candidate = discovery_manifest.get("candidate")
        if isinstance(candidate, Mapping) and _text(candidate.get("label")):
            return _text(candidate.get("label"))
    candidate_manifest = row.get("candidate_manifest")
    if isinstance(candidate_manifest, Mapping) and _text(candidate_manifest.get("label")):
        return _text(candidate_manifest.get("label"))
    menu_path = row.get("menu_path")
    if isinstance(menu_path, list | tuple) and menu_path:
        return _text(menu_path[-1])
    return ""
