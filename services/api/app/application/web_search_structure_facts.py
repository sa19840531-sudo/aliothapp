"""验证 Web 同帧搜索普查，只补充有界结构计数，不签发页面类型。"""

from collections.abc import Mapping


def web_secondary_region_nodes(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    """只消费同文档 Web 普查边界，不接受未知选择器或额外正文计数。"""
    if not web_search_structure_facts(snapshot):
        return []
    native = snapshot["web_native_structure"]
    nodes = native.get("secondary_regions")
    if not isinstance(nodes, list) or len(nodes) > 16:
        return []
    result = []
    for node in nodes:
        if not isinstance(node, Mapping) or not (
            node.get("visible") is True
            and node.get("web_secondary_recommendation") is True
            and node.get("tag") in {"section", "aside", "div"}
            and isinstance(node.get("selector"), str)
            and 0 < len(node["selector"]) <= 512
        ):
            return []
        result.append({key: node.get(key) for key in (
            "selector", "parent_selector", "tag", "visible", "web_secondary_recommendation",
        )})
    return result


def web_search_structure_facts(snapshot: Mapping[str, object]) -> dict[str, int]:
    native = snapshot.get("web_native_structure")
    document = snapshot.get("document")
    if not isinstance(native, Mapping) or not isinstance(document, Mapping):
        return {}
    facts = native.get("search_structure")
    if not isinstance(facts, Mapping):
        return {}
    epoch = document.get("time_origin_ms")
    if (
        snapshot.get("web_detail_candidate_priority_enabled") is not True
        or facts.get("schema_version") != "web-search-structure.v1"
        or facts.get("complete") is not True
        or type(epoch) not in (int, float)
        or epoch <= 0
        or facts.get("document_time_origin_ms") != epoch
        or native.get("document_time_origin_ms") != epoch
    ):
        return {}
    names = ("search_input_count", "search_trigger_count")
    if any(type(facts.get(name)) is not int or not 0 <= facts[name] <= 1000 for name in names):
        return {}
    if facts["search_trigger_count"] and not facts["search_input_count"]:
        return {}
    return {name: facts[name] for name in names}


def web_article_auxiliary_nodes(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    """仅消费同文档、有限且带固定关系类别的辅助区域。"""
    if not web_search_structure_facts(snapshot):
        return []
    nodes = snapshot["web_native_structure"].get("auxiliary_regions", [])
    if not isinstance(nodes, list) or len(nodes) > 16:
        return []
    result = []
    for node in nodes:
        if not isinstance(node, Mapping) or not (
            node.get("visible") is True and node.get("within_web_navigation") is True
            and node.get("tag") in {"div", "section", "nav"}
            and node.get("web_auxiliary_region_kind") in {
                "adjacent_article_navigation", "dated_article_metadata",
            }
            and isinstance(node.get("selector"), str) and 0 < len(node["selector"]) <= 512
        ):
            return []
        result.append({key: node.get(key) for key in (
            "selector", "parent_selector", "tag", "visible", "within_web_navigation",
            "web_auxiliary_region_kind",
        )})
    return result
