"""探索链路的分栏身份与内容就绪判定。"""

from collections.abc import Mapping

from services.api.app.application.app_constrained_locate import center_from_normalized_bounds
from services.api.app.domain.interaction import InteractionOperation


def tab_label(value):
    """去除平台签发的语意包装，保持真实栏目文字。"""
    text = str(value or "").strip()
    for prefix in ("底栏「", "底欄「", "底部导航「", "底部導覽「"):
        if text.startswith(prefix) and text.endswith("」"):
            return text[len(prefix) : -1].strip()
    return text


def visible_bottom_tabs(work):
    """只用本帧明确标注的底栏候选建立实际清单。"""
    result = []
    seen = set()
    for candidate in work.frontier.candidates:
        if candidate.metadata.get("bottom_navigation_tab_seek") is not True:
            continue
        if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
            continue
        label = tab_label(candidate.semantic_target or candidate.metadata.get("text"))
        bounds = candidate.metadata.get("normalized_bounds")
        center = center_from_normalized_bounds(bounds if isinstance(bounds, Mapping) else None)
        if label and label not in seen:
            seen.add(label)
            result.append((center[0] if center else len(result), label, candidate))
    return sorted(result, key=lambda item: item[0])


def tab_role(label):
    """名称只用于语意角色归一化，不决定是否允许遍历。"""
    labels = {
        "home": {"首页", "首頁", "主页", "主頁", "home"},
        "profile": {"我的", "我", "个人中心", "個人中心", "mine", "profile", "me"},
        "library": {"剧库", "劇庫", "剧场", "劇場", "短剧", "短劇", "library"},
        "vote": {"投票", "vote", "voting"},
        "topic": {"专题", "專題", "topic", "topics"},
        "darkweb": {"暗网", "暗網"},
        "ai": {"ai"},
        "community": {"社区", "社區", "community"},
    }
    return next((role for role, names in labels.items() if label.casefold() in names), None)


def content_ready(page_state):
    """只有明确稳定或空态才视为加载结束。"""
    return page_state in {"stable", "loaded", "ready", "empty", "empty_state"}


def tab_destination_confirmed(work, label):
    """优先使用真实选中／目的页证据，兼容已知语意页型。"""
    page = str(work.frontier.observation.page_type or "").casefold()
    facts = work.frontier.observation.metadata.get("surface_facts", {})
    selected_labels = facts.get("pixel_selected_labels") if isinstance(facts, Mapping) else None
    if isinstance(selected_labels, (list, tuple)) and selected_labels:
        return label in {tab_label(item) for item in selected_labels}
    for _, name, candidate in visible_bottom_tabs(work):
        if name != label:
            continue
        if candidate.metadata.get("selected") is True:
            return True
        if candidate.metadata.get("selected") is False:
            return False
        expected = candidate.metadata.get("destination_page_type") or candidate.metadata.get(
            "expected_next_page_type"
        )
        if expected and page == str(expected).casefold():
            return True
    expected_pages = {
        "home": {"home"},
        "profile": {"profile", "mine"},
        "library": {"library", "content_list", "video_list"},
        "vote": {"vote"},
        "topic": {"topic"},
    }
    return page in expected_pages.get(tab_role(label), set())
