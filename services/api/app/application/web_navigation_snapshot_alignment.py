"""将 HTML 导航控件与同一 Capture 的稀疏节点一对一对齐。"""

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol
from urllib.parse import urljoin


class _NavigationNode(Protocol):
    tag: str
    label: str
    target_hint: str
    dom_path: tuple[int, ...]
    hidden: bool


def align_navigation_snapshot_nodes(
    nodes: Sequence[_NavigationNode],
    snapshot: Mapping[str, object],
    *,
    base_url: str,
    target_hint: Callable[[dict[str, str | None]], str],
) -> dict[int, Mapping[str, object]]:
    """优先精确 DOM 路径；历史缺路径时只接受双方唯一的语义目标。"""
    raw_elements = snapshot.get("elements")
    observed = (
        [
            item
            for item in raw_elements
            if isinstance(item, Mapping) and item.get("interactive") is True
        ]
        if isinstance(raw_elements, list)
        else []
    )
    used: set[int] = set()
    result: dict[int, Mapping[str, object]] = {}

    def observed_target(item: Mapping[str, object]) -> str:
        value = target_hint(
            {
                "href": item.get("href"),
                "to": item.get("router_to"),
                "data-route": item.get("data_route"),
                "data-href": item.get("data_href"),
                "data-url": item.get("data_url"),
                "onclick": item.get("onclick"),
            }
        )
        return urljoin(base_url, value) if value else ""

    def node_target(node: _NavigationNode) -> str:
        return urljoin(base_url, node.target_hint) if node.target_hint else ""

    targets = [observed_target(item) for item in observed]
    for node_index, node in enumerate(nodes):
        if node.hidden:
            continue
        path = "/".join(str(index) for index in node.dom_path)
        matches = [
            index
            for index, item in enumerate(observed)
            if index not in used
            and item.get("web_navigation_dom_path") == path
            and item.get("tag") == node.tag
            and targets[index] == node_target(node)
        ]
        if not matches:
            # 禁止把一个可见节点借给多份同名 HTML；新路径节点不得降级语义匹配。
            same_nodes = [
                candidate
                for candidate in nodes
                if not candidate.hidden
                and candidate.tag == node.tag
                and candidate.label == node.label
                and node_target(candidate) == node_target(node)
            ]
            if len(same_nodes) == 1:
                matches = [
                    index
                    for index, item in enumerate(observed)
                    if index not in used
                    and not item.get("web_navigation_dom_path")
                    and item.get("tag") == node.tag
                    and " ".join(str(item.get("text") or "").split()) == node.label
                    and targets[index] == node_target(node)
                ]
        if len(matches) == 1:
            index = matches[0]
            used.add(index)
            result[node_index] = observed[index]
    return result
