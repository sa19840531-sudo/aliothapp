"""菜单探索父链和跨 Capture 稳定身份的唯一构造规则。"""

import json
from dataclasses import replace
from hashlib import sha256

from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


def logical_navigation_identity(candidate) -> str:
    """临时节点、位置、Capture 与 epoch 仅用于动作审计，不进入逻辑身份。"""
    if candidate.url:
        try:
            target = DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(candidate.url)
        except ValueError:
            # 非权威 URL 留待既有身份隔离器阻断，不能让审计候选丢失。
            target = candidate.url
        identity = ("url", target)
    else:
        identity = (
            "control",
            candidate.navigation_container_id,
            " ".join(candidate.label.split()).casefold(),
            candidate.target_hint,
            candidate.source_kind,
        )
    payload = (
        identity,
        tuple(" ".join(item.split()).casefold() for item in candidate.menu_path),
        tuple(" ".join(item.split()).casefold() for item in candidate.exploration_path),
        candidate.parent_logical_target_identity,
    )
    return sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def inherit_navigation_lineage(candidate, *, parent_candidate, parent_menu_entry_id):
    """同页 DOM 路径与跨页父路径只在共同边界重合，完整保留其余层级。"""
    if parent_candidate is None:
        return candidate
    prefix = parent_candidate.exploration_path
    suffix = candidate.menu_path
    overlap = 0
    for size in range(1, min(len(prefix), len(suffix)) + 1):
        if prefix[-size:] == suffix[:size]:
            overlap = size
    path = (*prefix, *suffix[overlap:])
    return replace(
        candidate,
        parent_menu_entry_id=parent_menu_entry_id,
        parent_logical_target_identity=parent_candidate.logical_target_identity,
        menu_depth=parent_candidate.menu_depth + 1,
        exploration_path=path,
    )


def navigation_observation_identity(candidate):
    """识别连续 Capture 中持续可见的同一控件，避免将父菜单重挂为自己的子项。"""
    return (
        candidate.navigation_container_id,
        candidate.url or candidate.target_hint,
        tuple(" ".join(item.split()).casefold() for item in candidate.menu_path),
        candidate.source_kind,
    )


def global_root_navigation_identity(candidate):
    """跨页面仅以可规范化 URL、标签和 DOM 菜单路径恢复唯一全局根项。"""
    if not candidate.url:
        return None
    try:
        target = DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(candidate.url)
    except ValueError:
        return None
    return (
        target,
        " ".join(candidate.label.split()).casefold(),
        tuple(" ".join(item.split()).casefold() for item in candidate.menu_path),
    )


def retain_navigation_lineage(candidate, previous):
    """持续可见控件刷新动作来源，同时保留最初确定的探索父链。"""
    return replace(
        candidate,
        parent_menu_entry_id=previous.parent_menu_entry_id,
        parent_logical_target_identity=previous.parent_logical_target_identity,
        menu_depth=previous.menu_depth,
        exploration_path=previous.exploration_path,
    )


def reconcile_navigation_lineages(candidates, *, history, parent_candidate):
    """父上下文限定子项复用；已确认根控件和当前祖先可跨加载空帧恢复。"""
    by_logical = {item.logical_target_identity: item for item in history}
    ancestors = set()
    ancestor = parent_candidate
    while ancestor is not None and ancestor.logical_target_identity not in ancestors:
        ancestors.add(ancestor.logical_target_identity)
        by_logical[ancestor.logical_target_identity] = ancestor
        ancestor = by_logical.get(ancestor.parent_logical_target_identity)
    root_groups = {}
    for item in by_logical.values():
        if item.parent_menu_entry_id is None and item.menu_depth == 1:
            root_groups.setdefault(item.navigation_container_id, []).append(item)
    current_observations = {navigation_observation_identity(item) for item in candidates}
    proven_roots = {
        item.logical_target_identity
        for group in root_groups.values()
        if len(group) >= 2
        and all(navigation_observation_identity(root) in current_observations for root in group)
        for item in group
    }
    historical_semantic_roots = {}
    for item in history:
        semantic_identity = global_root_navigation_identity(item)
        if (
            semantic_identity is not None
            and item.parent_menu_entry_id is None
            and item.menu_depth == 1
        ):
            historical_semantic_roots.setdefault(semantic_identity, []).append(item)
    current_semantic_counts = {}
    for item in candidates:
        semantic_identity = global_root_navigation_identity(item)
        if semantic_identity is not None:
            current_semantic_counts[semantic_identity] = (
                current_semantic_counts.get(semantic_identity, 0) + 1
            )
    output = []
    for candidate in candidates:
        observed = navigation_observation_identity(candidate)
        matches = {
            item.logical_target_identity: item
            for item in by_logical.values()
            if navigation_observation_identity(item) == observed
            and (
                item.parent_logical_target_identity == candidate.parent_logical_target_identity
                or item.logical_target_identity in ancestors
                or item.logical_target_identity in proven_roots
            )
        }
        if len(matches) == 1:
            candidate = retain_navigation_lineage(candidate, next(iter(matches.values())))
        elif len(matches) > 1:
            # 多条可能父链无法由当前证据唯一恢复，不能创建新的可执行身份。
            candidate = replace(
                candidate,
                actionability="blocked",
                identity_review_reason="WEB_MENU_LINEAGE_AMBIGUOUS",
            )
        elif (
            (semantic_identity := global_root_navigation_identity(candidate)) is not None
            and semantic_identity in historical_semantic_roots
        ):
            semantic_roots = historical_semantic_roots[semantic_identity]
            if len(semantic_roots) == 1 and current_semantic_counts[semantic_identity] == 1:
                candidate = retain_navigation_lineage(candidate, semantic_roots[0])
            else:
                candidate = replace(
                    candidate,
                    actionability="blocked",
                    identity_review_reason="WEB_MENU_LINEAGE_AMBIGUOUS",
                )
        elif parent_candidate is not None:
            uncertain_roots = [
                item
                for group in root_groups.values()
                for item in group
                if navigation_observation_identity(item) == observed
            ]
            if uncertain_roots:
                if len(uncertain_roots) == 1:
                    candidate = retain_navigation_lineage(candidate, uncertain_roots[0])
                candidate = replace(
                    candidate,
                    actionability="blocked",
                    identity_review_reason="WEB_MENU_LINEAGE_UNPROVEN",
                )
        output.append(candidate)
    return tuple(output)
