"""根据可信 Snapshot 父链区分单篇正文与重复业务卡片。"""

from collections.abc import Mapping, Sequence


class WebSnapshotBodyScopes:
    """独立选择器通过 parent_selector 关联；旧路径选择器仅作为缺父链回退。"""

    def __init__(self, elements: Sequence[object]) -> None:
        self.nodes = {
            str(item["selector"]).strip(): item
            for item in elements
            if isinstance(item, Mapping) and item.get("visible") is True and item.get("selector")
        }
        self._ancestors: dict[str, tuple[str, ...]] = {}
        for item in tuple(self.nodes.values()):
            chain = item.get("web_ancestor_structure")
            if not isinstance(chain, list):
                continue
            for index, ancestor in enumerate(chain[:32]):
                if not isinstance(ancestor, Mapping) or not isinstance(
                    ancestor.get("selector"), str
                ):
                    break
                selector = ancestor["selector"].strip()
                if not selector:
                    break
                next_parent = (
                    chain[index + 1].get("selector")
                    if index + 1 < min(len(chain), 32) and isinstance(chain[index + 1], Mapping)
                    else None
                )
                # 祖先摘要补全关系索引，不作为额外页面候选或业务主体计数。
                self.nodes.setdefault(
                    selector,
                    {
                        "selector": selector,
                        "parent_selector": next_parent,
                        "tag": ancestor.get("tag"),
                        "repeat_group_size": ancestor.get("repeat_group_size"),
                    },
                )
                if next_parent and not self.nodes[selector].get("parent_selector"):
                    self.nodes[selector] = dict(self.nodes[selector], parent_selector=next_parent)

    def ancestors(self, selector: str) -> tuple[str, ...]:
        if selector in self._ancestors:
            return self._ancestors[selector]
        result = []
        seen = {selector}
        current = selector
        while current:
            node = self.nodes.get(current, {})
            parent = str(node.get("parent_selector") or "").strip()
            if not parent:
                # 只有旧快照缺父字段时，才使用已有 DOM 节点的路径边界补足关系。
                parents = [
                    item
                    for item in self.nodes
                    if current.startswith(item)
                    and len(current) > len(item)
                    and (current[len(item)].isspace() or current[len(item)] == ">")
                ]
                parent = max(parents, key=len, default="")
            if not parent or parent in seen:
                break
            result.append(parent)
            seen.add(parent)
            current = parent
        self._ancestors[selector] = tuple(result)
        return tuple(result)

    def within(self, selector: str, scopes: set[str]) -> bool:
        return selector in scopes or bool(set(self.ancestors(selector)) & scopes)

    def card_scopes(self, detail_selectors: Sequence[str]) -> set[str]:
        """选择包含一个可信详情锚点的最外层重复业务容器，保留正文后的推荐区。"""
        ancestors = {
            selector: (selector, *self.ancestors(selector)) for selector in detail_selectors
        }
        counts: dict[str, set[str]] = {}
        for selector, chain in ancestors.items():
            for scope in chain:
                counts.setdefault(scope, set()).add(selector)
        result = set()
        for chain in ancestors.values():
            eligible = []
            for scope in chain:
                node = self.nodes.get(scope, {})
                size = node.get("repeat_group_size")
                repeated = node.get("within_repeated_structure") is True or (
                    type(size) is int and size >= 2
                )
                if (
                    repeated
                    and str(node.get("tag") or "").casefold()
                    in {"div", "li", "article", "section", "a"}
                    and len(counts[scope]) == 1
                ):
                    eligible.append(scope)
            if eligible:
                result.add(eligible[-1])
        return result
