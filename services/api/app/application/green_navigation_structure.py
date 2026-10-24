"""将同一采集的现有 HTML 与 UI 原件对齐，只识别能完整枚举的标准导航组。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from services.api.app.application.web_navigation_recognition import navigation_entry_kind
from services.api.app.application.web_navigation_snapshot_alignment import (
    align_navigation_snapshot_nodes,
)

_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_IGNORED = frozenset({"script", "style", "template", "noscript"})


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    dom_path: tuple[int, ...]
    parent: _Node | None
    children: list[_Node] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    @property
    def label(self):
        return " ".join(" ".join(self.text_parts).split())

    @property
    def target_hint(self):
        return _target(self.attrs)

    @property
    def hidden(self):
        return False


class _Tree(HTMLParser):
    """仅建立既有原件的有界结构索引，不执行 HTML 或页面脚本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("__root__", {}, (), None)
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        if len(self.nodes) >= 20000 or len(self.stack) >= 128:
            raise ValueError("navigation_dom_capacity_exceeded")
        parent = self.stack[-1]
        node = _Node(
            tag,
            {key: value or "" for key, value in attrs},
            (*parent.dom_path, len(parent.children) + 1),
            parent,
        )
        parent.children.append(node)
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not any(node.tag in _IGNORED for node in self.stack):
            for node in self.stack[1:]:
                node.text_parts.append(data)


def _target(values):
    return next(
        (
            str(values.get(key))
            for key in ("href", "to", "data-route", "data-href", "data-url", "onclick")
            if values.get(key)
        ),
        "",
    )


def _control(node):
    return node.tag in {"a", "button", "router-link"} or node.attrs.get("role") in {
        "tab",
        "link",
        "menuitem",
        "button",
    }


def _slots(container):
    current = container
    for _ in range(4):
        children = [node for node in current.children if node.tag not in _IGNORED]
        if len(children) == 1 and not _control(children[0]) and children[0].tag != "li":
            current = children[0]
            continue
        if len(children) < 2:
            return None
        result = []
        for slot in children:
            if _control(slot):
                result.append((slot, slot))
            elif slot.tag == "li":
                controls = [node for node in slot.children if _control(node)]
                if len(controls) != 1:
                    return None
                result.append((slot, controls[0]))
            else:
                return None
        return result
    return None


@dataclass(frozen=True)
class NavigationGroup:
    """完整 HTML 枚举及同一原件快照一对一证明的导航槽位。"""

    identity: str
    slots: tuple[Mapping, ...]
    controls: tuple[Mapping, ...]
    text_only: tuple[bool, ...]
    container: Mapping | None


def read_navigation_groups(dom: str, snapshot: Mapping):
    """缺失节点或截断均留缺口，不能把可见子集或空数组认定为全量分类。"""
    if "alioth:dom-truncated" in dom:
        return (), ("navigation_dom_truncated",)
    tree = _Tree()
    try:
        tree.feed(dom)
        tree.close()
    except (ValueError, RecursionError):
        return (), ("navigation_dom_unreadable_or_over_capacity",)
    containers = [
        node
        for node in tree.nodes
        if node.tag == "nav" or node.attrs.get("role") in {"navigation", "tablist"}
    ]
    # 嵌套 tablist 属于外层导航的同一组，不重复计数；复杂层级仍显式缺证。
    container_ids = {id(node) for node in containers}
    containers = [node for node in containers if not _has_container_parent(node, container_ids)]
    if len(containers) > 32:
        return (), ("navigation_group_capacity_exceeded",)
    groups, issues = [], []
    rows = snapshot.get("elements")
    rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    by_selector = {}
    for row in rows:
        by_selector.setdefault(row.get("selector"), []).append(row)
    base_url = str(snapshot.get("url") or "")
    semantic_counts = Counter(
        (node.tag, node.label, urljoin(base_url, node.target_hint) if node.target_hint else "")
        for node in tree.nodes
        if _control(node)
    )
    for index, container in enumerate(containers):
        identity = f"navigation:{index + 1}"
        pairs = _slots(container)
        if pairs is None or len(pairs) > 300:
            issues.append(identity + ":complete_navigation_siblings_missing")
            continue
        controls = [control for _, control in pairs]
        aligned = align_navigation_snapshot_nodes(
            controls,
            snapshot,
            base_url=base_url,
            target_hint=_target,
        )
        if len(aligned) != len(controls):
            issues.append(identity + ":navigation_dom_snapshot_incomplete")
            continue
        slot_rows, control_rows = [], []
        for position, (slot, control) in enumerate(pairs):
            row = aligned[position]
            signature = (
                control.tag,
                control.label,
                urljoin(base_url, control.target_hint) if control.target_hint else "",
            )
            if not row.get("web_navigation_dom_path") and semantic_counts[signature] != 1:
                # 旧 H5 原件缺精确 DOM 路径时，全 HTML 有同名同目标就不能跨导航借节点。
                break
            candidates = (
                [row] if slot is control else by_selector.get(row.get("parent_selector"), [])
            )
            if (
                len(candidates) != 1
                or candidates[0].get("tag") != slot.tag
                or row.get("visible") is not True
                or navigation_entry_kind(row) != "category"
                or row.get("within_navigation") is not True
            ):
                break
            slot_rows.append(candidates[0])
            control_rows.append(row)
        if len(slot_rows) != len(pairs):
            issues.append(identity + ":navigation_slot_identity_unverified")
            continue
        parents = {row.get("parent_selector") for row in slot_rows}
        if len(parents) != 1 or None in parents or "" in parents:
            issues.append(identity + ":navigation_slot_parent_unverified")
            continue
        # HTML 与 UI 是同一 Capture 内顺序产生的原件；新增同级节点不能被旧 HTML 子集漏掉。
        parent = next(iter(parents))
        expected_slots = {row.get("selector") for row in slot_rows}
        observed_slots = {
            row.get("selector")
            for row in rows
            if row.get("parent_selector") == parent and row.get("within_navigation") is True
        }
        if observed_slots != expected_slots:
            issues.append(identity + ":navigation_dom_snapshot_sibling_drift")
            continue
        parent_rows = by_selector.get(next(iter(parents)), []) if len(parents) == 1 else []
        groups.append(
            NavigationGroup(
                identity,
                tuple(slot_rows),
                tuple(control_rows),
                tuple(not control.children and bool(control.label) for control in controls),
                parent_rows[0] if len(parent_rows) == 1 else None,
            )
        )
    if not containers:
        issues.append("complete_navigation_siblings_missing")
    return tuple(groups), tuple(issues)


def _has_container_parent(node, container_ids):
    parent = node.parent
    while parent is not None:
        if id(parent) in container_ids:
            return True
        parent = parent.parent
    return False
