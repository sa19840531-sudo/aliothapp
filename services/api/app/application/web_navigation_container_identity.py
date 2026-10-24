"""导航容器的语义身份；临时解析句柄和 DOM 位置不参与稳定哈希。"""

import json
import re
from collections import Counter
from collections.abc import Mapping
from hashlib import sha256


def _entry_observations(handle, elements):
    """入口集合只提供同帧消歧和跨帧匹配证据，不重算已签发身份。"""
    from services.api.app.application.web_navigation_recognition import navigation_entry_kind

    return frozenset(
        (
            " ".join(str(item.get("text") or "").split()).casefold(),
            str(item.get("href") or ""),
            str(item.get("tag") or ""),
            tuple(item.get("navigation_menu_path") or ()),
        )
        for item in elements
        if item.get("navigation_container_id") == handle
        and item.get("visible") is True
        and navigation_entry_kind(item) == "category"
    )


def _identity(material):
    return (
        "web-nav-container:"
        + sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    )


class WebNavigationContainerRegistry:
    """会话内签发并保留匿名容器身份，匹配多解时只输出待复核记录。"""

    def __init__(self):
        self._records = {}
        self._explicit = set()
        self._observation_owners = {}
        self._conflicted_semantic_keys = set()

    def resolve(self, descriptors, elements):
        identities, ambiguous = {}, set()
        anonymous = {}
        for handle, descriptor in descriptors.items():
            if descriptor.get("id"):
                identity = (
                    "web-nav-container:" + sha256(descriptor["id"].encode()).hexdigest()[:24]
                )
                identities[handle] = identity
                self._explicit.add(identity)
            else:
                semantic_key = json.dumps(descriptor, ensure_ascii=False, sort_keys=True)
                anonymous.setdefault(semantic_key, {})[handle] = _entry_observations(
                    handle, elements
                )
        for semantic_key, observations in anonymous.items():
            known = {
                identity: entries
                for identity, (key, entries) in self._records.items()
                if key == semantic_key
            }
            assignments, unresolved = _match_observations(observations, known)
            for handle, entries in observations.items():
                identity = assignments.get(handle)
                if handle in unresolved:
                    identity = _identity(("anonymous-unproven", semantic_key, sorted(entries)))
                    ambiguous.add(identity)
                elif identity is None:
                    # 语义唯一时主身份只用容器语义；并列同语义容器首次签发才用入口消歧。
                    identity = _identity(("anonymous", semantic_key))
                    if identity in self._records or len(observations) > 1:
                        identity = _identity(("anonymous", semantic_key, sorted(entries)))
                identities[handle] = identity
                if identity not in ambiguous:
                    self._records[identity] = (semantic_key, entries)
                    observation_key = (semantic_key, entries)
                    owner = self._observation_owners.get(observation_key)
                    if owner is not None and owner != identity:
                        self._conflicted_semantic_keys.add(semantic_key)
                    else:
                        self._observation_owners[observation_key] = identity
                if semantic_key in self._conflicted_semantic_keys:
                    ambiguous.add(identity)
        counts = Counter(identities.values())
        ambiguous.update(identity for identity, count in counts.items() if count > 1)
        return identities, ambiguous

    def seed_for(self, identity):
        """导出不含 DOM 位置的持久恢复种子。"""
        if identity in self._explicit:
            return {
                "version": "web-nav-container-seed.v1",
                "identity": identity,
                "kind": "explicit",
            }
        record = self._records.get(identity)
        if record is None:
            return None
        semantic_key, entries = record
        return {
            "version": "web-nav-container-seed.v1",
            "identity": identity,
            "kind": "anonymous",
            "semantic_key": semantic_key,
            "observations": [
                [label, href, tag, list(path)]
                for label, href, tag, path in sorted(entries)
            ],
        }

    def restore(self, seed):
        """只接受自洽且唯一的持久种子；冲突时返回固定阻断原因。"""
        if not isinstance(seed, Mapping):
            return "WEB_MENU_LINEAGE_UNAVAILABLE"
        identity = str(seed.get("identity") or "").strip()
        if (
            seed.get("version") != "web-nav-container-seed.v1"
            or not identity.startswith("web-nav-container:")
        ):
            return "WEB_MENU_LINEAGE_UNAVAILABLE"
        if seed.get("kind") == "explicit":
            self._explicit.add(identity)
            return None
        if seed.get("kind") != "anonymous":
            return "WEB_MENU_LINEAGE_UNAVAILABLE"
        semantic_key = seed.get("semantic_key")
        observations = seed.get("observations")
        if not isinstance(semantic_key, str) or not _valid_semantic_key(semantic_key):
            return "WEB_MENU_LINEAGE_UNAVAILABLE"
        entries = _restore_observations(observations)
        if entries is None:
            return "WEB_MENU_LINEAGE_UNAVAILABLE"
        observed_entries = entries
        current = self._records.get(identity)
        if current is not None:
            current_key, current_entries = current
            if current_key != semantic_key:
                return "WEB_MENU_LINEAGE_AMBIGUOUS"
            if not (
                current_entries.issubset(entries)
                or entries.issubset(current_entries)
            ):
                self._conflicted_semantic_keys.add(semantic_key)
                return "WEB_MENU_LINEAGE_AMBIGUOUS"
            entries = current_entries | entries
        observation_key = (semantic_key, observed_entries)
        owner = self._observation_owners.get(observation_key)
        if owner is not None and owner != identity:
            self._conflicted_semantic_keys.add(semantic_key)
            return "WEB_MENU_LINEAGE_AMBIGUOUS"
        if semantic_key in self._conflicted_semantic_keys and current is None:
            return "WEB_MENU_LINEAGE_AMBIGUOUS"
        self._records[identity] = (semantic_key, entries)
        self._observation_owners[observation_key] = identity
        if semantic_key in self._conflicted_semantic_keys:
            return "WEB_MENU_LINEAGE_AMBIGUOUS"
        return None


def _valid_semantic_key(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and not str(parsed.get("id") or "").strip()


def _restore_observations(value):
    if not isinstance(value, list):
        return None
    restored = set()
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or not all(isinstance(part, str) for part in item[:3])
            or not isinstance(item[3], list)
            or not all(isinstance(part, str) for part in item[3])
        ):
            return None
        restored.add((item[0], item[1], item[2], tuple(item[3])))
    return frozenset(restored)


def _match_observations(observations, known):
    """只接受双向唯一的最优匹配；同集合副本不能靠遍历顺序挑选。"""
    duplicates = Counter(observations.values())
    unresolved = {handle for handle, entries in observations.items() if duplicates[entries] > 1}
    remaining = set(observations) - unresolved
    available = set(known)
    assignments = {}
    while remaining and available:
        scores = {
            handle: {
                identity: (
                    int(observations[handle] == known[identity]),
                    len(observations[handle] & known[identity]),
                )
                for identity in available
                if observations[handle] == known[identity] or observations[handle] & known[identity]
            }
            for handle in remaining
        }
        pairs = {}
        for handle, row in scores.items():
            if not row:
                continue
            best = [identity for identity, score in row.items() if score == max(row.values())]
            if len(best) != 1:
                continue
            identity = best[0]
            column = {
                other: values[identity] for other, values in scores.items() if identity in values
            }
            winners = [other for other, score in column.items() if score == max(column.values())]
            if winners == [handle]:
                pairs[handle] = identity
        if not pairs:
            break
        assignments.update(pairs)
        remaining.difference_update(pairs)
        available.difference_update(pairs.values())
    if len(observations) == len(known) == 1 and remaining and not unresolved:
        # 单一语义容器的全部入口替换仍是同一容器，不依赖暂时可见的某个入口。
        assignments[next(iter(remaining))] = next(iter(known))
        remaining.clear()
        available.clear()
    if available:
        unresolved.update(remaining)
    return assignments, unresolved


def resolve_navigation_container_identities(descriptors, elements, *, registry=None):
    """无会话调用仍执行同帧消歧；生产 Capture 传入会话级身份注册表。"""
    return (registry or WebNavigationContainerRegistry()).resolve(descriptors, elements)


def _is_transient_class(token):
    """状态必须占据独立词边界；普通结构类中的相似子串不能被删除。"""
    states = {
        "active",
        "inactive",
        "selected",
        "expanded",
        "collapsed",
        "open",
        "opened",
        "closed",
        "hidden",
        "visible",
        "show",
        "shown",
        "focused",
        "disabled",
        "enabled",
    }
    parts = re.split(r"[-_:]+", token)
    if parts[-1] in states:
        return True
    # has-active-child 等关系状态允许尾部对象，但前缀后的第一词必须是已知状态。
    return len(parts) > 1 and parts[0] in {"is", "has"} and parts[1] in states


def navigation_container_descriptor(tag, values):
    """只保留身份语义，不将展开、激活、隐藏等瞬时状态纳入签名。"""
    return {
        "id": str(values.get("id") or "").strip(),
        "tag": tag,
        "role": str(values.get("role") or "").strip().casefold(),
        "label": " ".join(str(values.get("aria-label") or "").split()).casefold(),
        "class_tokens": sorted(
            token
            for token in set(str(values.get("class") or "").casefold().split())
            if not _is_transient_class(token)
        ),
        "data_nav": str(values.get("data-nav") or values.get("data-navigation") or ""),
    }
