"""从可见导航范围形成事实与确定性容器门，业务卡片不参与菜单计数。"""

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from math import isfinite

WEB_NAVIGATION_CONFIDENCE_FLOOR = 0.7


@dataclass(frozen=True)
class WebNavigationStructureFacts:
    top_navigation_container_count: int = 0
    visible_category_entry_count: int = 0
    search_entry_count: int = 0
    auth_entry_count: int = 0
    logo_entry_count: int = 0
    footer_only_entry_count: int = 0
    dropdown_trigger_count: int = 0
    expanded_submenu_count: int = 0
    advertisement_entry_count: int = 0
    navigation_obstructed: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WebNavigationAssessment:
    proven: bool
    facts: WebNavigationStructureFacts
    proven_container_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def navigation_entry_kind(element: Mapping[str, object]) -> str:
    """支持控件不计作分类入口，风险文字与显式副作用共同参与否决。"""
    if element.get("within_breadcrumb") is True:
        return "breadcrumb"
    text = " ".join(
        str(element.get(key) or "")
        for key in (
            "text",
            "href",
            "target_hint",
            "aria_label",
            "class",
            "class_tokens",
            "role",
        )
    ).casefold()
    if (
        element.get("disabled") is True
        or element.get("web_navigation_disabled") is True
        or element.get("aria_disabled") in (True, "true")
        or element.get("inert") is True
    ):
        return "disabled"
    if element.get("within_footer") is True:
        return "footer"
    if element.get("is_advertisement") is True:
        return "advertisement"
    if element.get("within_pagination") is True:
        return "pagination"
    if element.get("target_risk") in {"cross_origin", "dangerous"}:
        return str(element["target_risk"])
    if element.get("side_effect_kind"):
        return "dangerous"
    if re.search(
        r"登录|注册|授权|退出|login|log-in|signin|sign-in|signup|sign-up|register|oauth|logout|authorize",
        text,
    ):
        return "auth"
    if re.search(r"搜索|search", text):
        return "search"
    if re.search(r"logo|站标|站点标识", text):
        return "logo"
    if re.search(
        r"支付|购买|充值|转账|下载|上传|提交|保存|删除|发布|授权|checkout|purchase|payment|download|upload|submit|delete|signout|sign-out",
        text,
    ):
        return "dangerous"
    return "category"


def navigation_runtime_evidence_is_trusted(element: Mapping[str, object]) -> bool:
    """只接受达到阈值且未被命中测试否决的运行时导航事实。"""

    confidence = element.get("navigation_confidence", 1.0)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not isfinite(float(confidence))
        or float(confidence) < WEB_NAVIGATION_CONFIDENCE_FLOOR
    ):
        return False
    hit_test = element.get("hit_test")
    if not isinstance(hit_test, Mapping):
        return False
    return navigation_hit_test_is_positive(element)


def navigation_hit_test_is_positive(element: Mapping[str, object]) -> bool:
    """仅正向命中事实可使导航候选进入可执行路径。"""

    hit_test = element.get("hit_test")
    # 生产 pointer_interception 表示目标接收指针，不表示目标被其他节点拦截。
    # 是否可点以中心实际命中和视觉遮挡为准，不能反转该历史字段语义。
    return isinstance(hit_test, Mapping) and (
        hit_test.get("center_hits_target") is True
        and hit_test.get("visual_occlusion_detected") is not True
    )


def navigation_is_obstructed(snapshot: Mapping[str, object]) -> bool:
    if snapshot.get("human_verification_present") is True:
        return True
    elements = snapshot.get("elements")
    return (
        any(
            isinstance(item, Mapping)
            and item.get("visible") is True
            and (
                item.get("aria_modal") is True
                or item.get("web_human_verification_present") is True
                or (
                    item.get("web_obstruction_layer_structural") is True
                    # 非模态几何角标只影响实际被其遮住的目标，不能否决全页导航。
                    and item.get("web_obstruction_layer_kind") != "geometric_overlay"
                )
            )
            for item in elements
            if isinstance(elements, list)
        )
        if isinstance(elements, list)
        else False
    )


def navigation_is_outside_viewport(
    element: Mapping[str, object],
    snapshot: Mapping[str, object],
) -> bool:
    """有实际几何事实时，负坐标及中心离屏入口不参与发现或授权。"""
    rect, viewport = element.get("rect"), snapshot.get("viewport")
    if not isinstance(rect, Mapping) or not isinstance(viewport, Mapping):
        return False
    values = [rect.get(key) for key in ("x", "y", "width", "height")]
    values.extend(viewport.get(key) for key in ("width", "height"))
    if not all(type(value) in (int, float) and isfinite(value) for value in values):
        return True
    x, y, width, height, viewport_width, viewport_height = values
    return not (
        x >= 0
        and y >= 0
        and width > 0
        and height > 0
        and x + width / 2 < viewport_width
        and y + height / 2 < viewport_height
    )


def navigation_container_identity(element: Mapping[str, object]) -> str | None:
    if not (
        element.get("within_web_navigation") is True
        or ("within_web_navigation" not in element and element.get("within_navigation") is True)
        or element.get("tag") == "nav"
        or element.get("role") == "navigation"
    ):
        return None
    explicit = element.get("navigation_container_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    # 稀疏快照没有容器节点时，最近已采语义祖先或路径前缀仍可说明同一范围。
    ancestors = element.get("web_ancestor_structure")
    if isinstance(ancestors, list):
        for ancestor in ancestors:
            if isinstance(ancestor, Mapping) and ancestor.get("tag") == "nav":
                return str(ancestor.get("selector") or "") or None
    selector = str(element.get("selector") or "")
    if element.get("tag") == "nav" or element.get("role") == "navigation":
        return selector or None
    return selector.rsplit(" > ", 1)[0] if " > " in selector else None


def assess_navigation_container(snapshot: Mapping[str, object]) -> WebNavigationAssessment:
    elements = snapshot.get("elements")
    groups: dict[str, set[str]] = {}
    counts = dict.fromkeys(("search", "auth", "logo", "footer", "advertisement"), 0)
    dropdowns = expanded = 0
    global_obstruction = navigation_is_obstructed(snapshot)
    obstructed = global_obstruction
    for index, element in enumerate(elements if isinstance(elements, list) else []):
        if not isinstance(element, Mapping) or element.get("visible") is not True:
            continue
        if element.get("top_navigation_position_proven") is False:
            continue
        container = navigation_container_identity(element)
        if container is None:
            continue
        if not navigation_runtime_evidence_is_trusted(element):
            continue
        kind = navigation_entry_kind(element)
        if kind in counts:
            counts[kind] += 1
        local_block = element.get("occluded") is True or element.get("obstructed") is True
        obstructed = obstructed or local_block
        if global_obstruction or local_block:
            continue
        if kind != "category" or element.get("interactive") is not True:
            continue
        dropdowns += int(element.get("aria_expanded") in (True, False, "true", "false"))
        expanded += int(element.get("aria_expanded") in (True, "true"))
        identity = str(
            element.get("href") or element.get("target_hint") or element.get("text") or index
        )
        groups.setdefault(container, set()).add(identity)
    proven = tuple(sorted(key for key, entries in groups.items() if len(entries) >= 2))
    facts = WebNavigationStructureFacts(
        top_navigation_container_count=len(proven),
        visible_category_entry_count=sum(len(groups[key]) for key in proven),
        search_entry_count=counts["search"],
        auth_entry_count=counts["auth"],
        logo_entry_count=counts["logo"],
        footer_only_entry_count=counts["footer"],
        advertisement_entry_count=counts["advertisement"],
        dropdown_trigger_count=dropdowns,
        expanded_submenu_count=expanded,
        navigation_obstructed=obstructed,
    )
    return WebNavigationAssessment(
        bool(proven), facts, proven, () if proven else ("WEB_NAVIGATION_CONTAINER_UNPROVEN",)
    )
