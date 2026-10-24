"""H5 页面探索剖面：从脱敏 DOM 中提取稳定结构信号。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Literal

from services.api.app.application.h5_interaction_capabilities import (
    COUNTDOWN_JUMP_DISMISSAL_SELECTOR,
    GLOBAL_LAUNCH_DISMISSAL_SELECTOR,
    LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR,
    POPUP_COUNT_DOWN_DISMISSAL_SELECTOR,
    POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
    STARTUP_AD_DISMISSAL_SELECTOR,
)
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP,
    H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA,
    H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT,
    H5_BOTTOM_NAVIGATION_FAMILY_VANT,
    H5_GENERIC_NAVIGATION_CONTAINER_CLASSES,
    H5_GENERIC_NAVIGATION_ITEM_CLASSES,
    h5_bottom_navigation_selector,
)
from services.api.app.domain.page.registry import get_default_page_type_registry

H5Shell = Literal["line_loading", "startup_ad", "stable", "unknown"]

DIALOG_CONTENT_CLOSE_SELECTOR = (
    ':is([role="dialog"], .van-dialog):visible '
    "> .van-dialog__content > .btn:visible > img:visible, "
    ':is([role="dialog"], .van-dialog):visible '
    "> .van-dialog__content > :is(.btn-box.end, .btn-box.close):visible"
)
DIALOG_ACKNOWLEDGEMENT_SELECTOR = ':is([role="dialog"], .van-dialog):visible .app-btn:visible'
VISIBLE_TABBAR_ITEM_SELECTOR = "div.van-tabbar:visible > div.van-tabbar-item"
VISIBLE_PAGE_TAB_SELECTOR = ".van-tabs:visible .van-tab"

_CONTENT_CARD_CLASSES = {
    "content-card",
    "data-item",
    "feed-item",
    "longvideocard",
    "media-item",
    "post-item",
    "video-item",
}
_UNSAFE_CARD_MARKERS = (
    "ad",
    "ads",
    "banner",
    "download",
    "install",
    "login",
    "signin",
    "sign-in",
    "pay",
    "payment",
    "recharge",
    "vip",
    "game",
    "external",
)
_BUSINESS_STRUCTURE_CLASSES = {
    "home-page",
    "product-page",
    "content-list",
    "video-list",
    "movie-list",
    "post-list",
    "profile-page",
    "mine-page",
    "recharge-page",
    "payment-page",
    "activity-page",
    "task-page",
    "van-tabbar",
    "van-tabs",
    "pages-foot-fix",
    "footer-wrap",
}
# 站点自定义 SPA 底栏没有统一的组件库标记，只接受明确的导航容器或
# 底栏项类名。普通 footer、列表 item 不在这里，避免把内容区域误判为导航。
_GENERIC_IMPLICIT_NAVIGATION_ITEM_CLASSES = {
    "bottom-bar-item",
    "bottom-nav-item",
    "bottom-navigation-item",
    "bottom-navbar-item",
    "bottom-tab-item",
    "footer-item",
    "navbar-item",
    "navigation-item",
    "tabbar-item",
}
_KNOWN_NAVIGATION_CLASS_MARKERS = {
    "pages-foot-fix",
    "van-tab",
    "van-tabbar",
    "van-tabbar-item",
    "van-tabs",
    "van-tabs__nav",
}
_GENERIC_NAVIGATION_ITEM_ROLES = {"button", "link", "menuitem", "tab"}
_GENERIC_NAVIGATION_MAX_ITEMS = 12
_GENERIC_NAVIGATION_MAX_LABEL_LENGTH = 64
_PAYMENT_SUBMIT_CLASSES = {
    "pay-btn",
    "payment-btn",
    "buy-btn",
    "submit-order",
    "order-submit",
}
_PAYMENT_SUBMIT_MARKERS = (
    "立即支付",
    "确认支付",
    "支付",
    "购买",
    "下单",
    "提交订单",
    "绑卡",
    "立即充值",
)
_DOWNLOAD_INSTALL_MARKERS = ("下载", "安装", "打开APP", "唤起APP")
_LOGIN_SUBMIT_MARKERS = ("登录", "注册", "绑定手机号", "验证码")
_STATE_CHANGING_MARKERS = ("发布", "投稿", "上传", "评论", "关注", "点赞")
_EXPLICIT_RISK_ACTIONS = {
    "payment": ("payment_submit", "PAYMENT_SUBMIT_ACTION"),
    "post_publish": ("post_publish", "POST_PUBLISH_ACTION"),
    "upload": ("upload", "UPLOAD_ACTION"),
}
_RISK_FLAGS_BY_REASON = {
    "PAYMENT_SUBMIT_ACTION": "high_risk_payment_entry",
    "POST_PUBLISH_ACTION": "high_risk_publish_entry",
    "UPLOAD_ACTION": "high_risk_upload_entry",
}


@dataclass(frozen=True)
class H5ObstructionSignal:
    """当前可见遮挡和推荐关闭入口。"""

    present: bool
    dismiss_selector: str
    z_index: int | None = None
    source: str = "dom"
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class H5TabbarItem:
    """可见导航项。"""

    label: str
    selector: str
    active: bool
    expected_page_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class H5ContentCard:
    """列表页首个低风险内容卡片。"""

    selector: str
    expected_page_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class H5RiskEntry:
    """只记录不执行的高风险入口。"""

    label: str
    risk_level: str
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class H5ExplorationProfile:
    """H5 页面探索剖面输出。"""

    profile: str = "h5_common"
    shell: H5Shell = "unknown"
    shell_confidence: float = 0.0
    business_structure_present: bool = False
    obstruction: H5ObstructionSignal | None = None
    tabbar_items: list[H5TabbarItem] = field(default_factory=list)
    page_tab_items: list[H5TabbarItem] = field(default_factory=list)
    content_card: H5ContentCard | None = None
    risk_entries: list[H5RiskEntry] = field(default_factory=list)
    page_type_candidates: list[str] = field(default_factory=list)
    stability_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "shell": self.shell,
            "shell_confidence": self.shell_confidence,
            "business_structure_present": self.business_structure_present,
            "obstruction": (self.obstruction.to_dict() if self.obstruction is not None else None),
            "tabbar_items": [item.to_dict() for item in self.tabbar_items],
            "page_tab_items": [item.to_dict() for item in self.page_tab_items],
            "content_card": (
                self.content_card.to_dict() if self.content_card is not None else None
            ),
            "risk_entries": [item.to_dict() for item in self.risk_entries],
            "page_type_candidates": list(self.page_type_candidates),
            "stability_hints": list(self.stability_hints),
        }


def build_h5_exploration_profile(
    dom: str,
    *,
    page_type: str | None = None,
) -> H5ExplorationProfile:
    """从当前 DOM 提取 H5 通用探索信号。"""
    parser = _H5SignalParser()
    try:
        parser.feed(dom)
        parser.close()
    except Exception:  # noqa: BLE001
        return H5ExplorationProfile(shell="unknown")

    shell = _detect_shell(parser=parser, dom=dom)
    tabbar_items = [
        H5TabbarItem(
            label=item.label,
            selector=h5_bottom_navigation_selector(
                family=item.family,
                index=item.index,
            ),
            active=item.active,
            expected_page_type=_expected_page_type_from_label(item.label),
        )
        for item in parser.tabbar_items
    ]
    for generic_item in extract_h5_generic_navigation_items(dom):
        if any(
            _normalized_text(item.label) == _normalized_text(generic_item.label)
            for item in tabbar_items
        ):
            continue
        tabbar_items.append(generic_item)
    page_tab_items = [
        H5TabbarItem(
            label=item.label,
            selector=f"{VISIBLE_PAGE_TAB_SELECTOR}:nth-of-type({item.index}):visible",
            active=item.active,
            expected_page_type=_expected_page_type_from_label(item.label),
        )
        for item in parser.page_tab_items
    ]
    content_card = _content_card(page_type=page_type, parser=parser)
    page_type_candidates = _page_type_candidates(
        tabbar_items=tabbar_items,
        page_tab_items=page_tab_items,
        content_card=content_card,
    )
    business_structure_present = bool(
        parser.business_structure_present
        or tabbar_items
        or page_tab_items
        or content_card is not None
        or page_type_candidates
    )
    if shell == "line_loading" and business_structure_present:
        shell = "stable"
    obstruction = _obstruction_signal(shell=shell, parser=parser)
    stability_hints = _stability_hints(
        parser=parser,
        business_structure_present=business_structure_present,
        tabbar_items=tabbar_items,
    )
    return H5ExplorationProfile(
        shell=shell,
        shell_confidence=_shell_confidence(shell=shell, parser=parser),
        business_structure_present=business_structure_present,
        obstruction=obstruction,
        tabbar_items=tabbar_items,
        page_tab_items=page_tab_items,
        content_card=content_card,
        risk_entries=_risk_entries(parser=parser),
        page_type_candidates=page_type_candidates,
        stability_hints=stability_hints,
    )


def extract_h5_generic_navigation_items(dom: str) -> list[H5TabbarItem]:
    """提取有明确结构证明的自定义 SPA 底栏项。

    该解析器只输出可见、同一导航组内的事件候选；未知文案仍保留在画像中，
    但不在这里猜测页面类型，调用方必须继续通过目录或别名解析确认目标。
    自定义项使用平台内部位置 token 作为语义目标；执行层必须再从实时 UI
    Snapshot 同时复核容器、项结构、位置、文案和可点击性，才返回真实 selector。
    """
    parser = _H5GenericNavigationParser()
    try:
        parser.feed(dom)
        parser.close()
    except Exception:  # noqa: BLE001
        return []

    items = parser.navigation_items()
    return [
        H5TabbarItem(
            label=item.label,
            selector=h5_bottom_navigation_selector(
                family=H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA,
                index=item.index,
            ),
            active=item.active,
            expected_page_type=_expected_page_type_from_label(item.label),
        )
        for item in items
    ]


def h5_profile_risk_flags(profile: H5ExplorationProfile) -> list[str]:
    """把 H5 结构化风险入口投影为页面检测稳定标记。"""
    return list(
        dict.fromkeys(
            flag
            for entry in profile.risk_entries
            if (flag := _RISK_FLAGS_BY_REASON.get(entry.reason_code)) is not None
        )
    )


def _detect_shell(*, parser: _H5SignalParser, dom: str) -> H5Shell:
    if parser.has_startup_ad_dismissal:
        return "startup_ad"
    normalized_text = _normalized_text(parser.visible_text)
    if parser.has_loading_page and any(
        marker in normalized_text for marker in ("获取线路", "最佳线路", "加载中", "loading")
    ):
        return "line_loading"
    if dom.strip():
        return "stable"
    return "unknown"


def _shell_confidence(*, shell: H5Shell, parser: _H5SignalParser) -> float:
    if shell == "startup_ad" and parser.has_startup_ad_dismissal:
        return 0.95
    if shell == "line_loading" and parser.has_loading_page:
        return 0.9
    if shell == "stable" and parser.business_structure_present:
        return 0.75
    if shell == "stable":
        return 0.5
    return 0.0


def _obstruction_signal(
    *,
    shell: H5Shell,
    parser: _H5SignalParser,
) -> H5ObstructionSignal | None:
    if shell == "startup_ad":
        dismiss_selector = (
            parser.startup_ad_dismissal_selector or STARTUP_AD_DISMISSAL_SELECTOR
        )
        return H5ObstructionSignal(
            present=True,
            dismiss_selector=dismiss_selector,
            z_index=parser.loading_page_z_index,
            source="startup_ad",
            fingerprint=_fingerprint("startup_ad", dismiss_selector),
        )
    if parser.has_dialog_content_close:
        return H5ObstructionSignal(
            present=True,
            dismiss_selector=DIALOG_CONTENT_CLOSE_SELECTOR,
            z_index=parser.dialog_z_index,
            source="dialog",
            fingerprint=_fingerprint("dialog", DIALOG_CONTENT_CLOSE_SELECTOR),
        )
    if parser.has_dialog_acknowledgement:
        return H5ObstructionSignal(
            present=True,
            dismiss_selector=DIALOG_ACKNOWLEDGEMENT_SELECTOR,
            z_index=parser.dialog_z_index,
            source="dialog",
            fingerprint=_fingerprint("dialog", DIALOG_ACKNOWLEDGEMENT_SELECTOR),
        )
    return None


def _content_card(
    *,
    page_type: str | None,
    parser: _H5SignalParser,
) -> H5ContentCard | None:
    if page_type == "video_list":
        if "long_video_card" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".longVideoCard:visible",
                expected_page_type="video_detail",
            )
        if "video_list_media" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".video-list .media-item:visible",
                expected_page_type="video_detail",
            )
        if "movie_list_media" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".movie-list .media-item:visible",
                expected_page_type="video_detail",
            )
        if "generic_video" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".video-item:visible",
                expected_page_type="video_detail",
            )
        if "generic_media" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".media-item:visible",
                expected_page_type="video_detail",
            )
    if page_type == "post_list":
        if "post_list_post" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".post-list .post-item:visible",
                expected_page_type="content_detail",
            )
        if "generic_post" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".post-item:visible",
                expected_page_type="content_detail",
            )
        if "generic_feed" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".feed-item:visible",
                expected_page_type="content_detail",
            )
        if "generic_media" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".media-item:visible",
                expected_page_type="content_detail",
            )
    if page_type == "content_list":
        if "content_list_data" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".content-list .data-item:visible",
                expected_page_type="content_detail",
            )
        if "generic_data" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".data-item:visible",
                expected_page_type="content_detail",
            )
        if "generic_media" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".media-item:visible",
                expected_page_type="content_detail",
            )
        if "generic_content_card" in parser.safe_content_card_signals:
            return H5ContentCard(
                selector=".content-card:visible",
                expected_page_type="content_detail",
            )
    return None


def _risk_entries(*, parser: _H5SignalParser) -> list[H5RiskEntry]:
    normalized = _normalized_text(parser.visible_text)
    entries: list[H5RiskEntry] = []
    for action_kind in sorted(parser.explicit_risk_actions):
        label, reason_code = _EXPLICIT_RISK_ACTIONS[action_kind]
        entries.append(
            H5RiskEntry(
                label=label,
                risk_level="high",
                reason_code=reason_code,
            )
        )
    if parser.class_tokens & _PAYMENT_SUBMIT_CLASSES or any(
        marker in normalized for marker in _PAYMENT_SUBMIT_MARKERS
    ):
        entries.append(
            H5RiskEntry(
                label="payment_submit",
                risk_level="high",
                reason_code="PAYMENT_SUBMIT_ACTION",
            )
        )
    if any(marker in normalized for marker in _DOWNLOAD_INSTALL_MARKERS):
        entries.append(
            H5RiskEntry(
                label="download_or_install",
                risk_level="high",
                reason_code="DOWNLOAD_OR_INSTALL_ACTION",
            )
        )
    if any(marker in normalized for marker in _LOGIN_SUBMIT_MARKERS):
        entries.append(
            H5RiskEntry(
                label="login_or_register",
                risk_level="high",
                reason_code="LOGIN_OR_REGISTER_ACTION",
            )
        )
    if any(marker in normalized for marker in _STATE_CHANGING_MARKERS):
        entries.append(
            H5RiskEntry(
                label="state_changing_action",
                risk_level="high",
                reason_code="STATE_CHANGING_ACTION",
            )
        )
    return _dedupe_risk_entries(entries)


def _dedupe_risk_entries(entries: list[H5RiskEntry]) -> list[H5RiskEntry]:
    deduped: list[H5RiskEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.reason_code in seen:
            continue
        seen.add(entry.reason_code)
        deduped.append(entry)
    return deduped


def _page_type_candidates(
    *,
    tabbar_items: list[H5TabbarItem],
    page_tab_items: list[H5TabbarItem],
    content_card: H5ContentCard | None,
) -> list[str]:
    values = [
        item.expected_page_type
        for item in [*tabbar_items, *page_tab_items]
        if item.expected_page_type is not None
    ]
    if content_card is not None:
        values.append(content_card.expected_page_type)
    return list(dict.fromkeys(values))


def _stability_hints(
    *,
    parser: _H5SignalParser,
    business_structure_present: bool,
    tabbar_items: list[H5TabbarItem] | None = None,
) -> list[str]:
    hints: list[str] = []
    if business_structure_present:
        hints.append("business_structure_present")
    if parser.tabbar_items or tabbar_items:
        hints.append("visible_tabbar")
    if parser.page_tab_items:
        hints.append("visible_page_tabs")
    if parser.safe_content_card_signals:
        hints.append("safe_content_card")
    return hints


def _expected_page_type_from_label(label: str) -> str | None:
    registry = get_default_page_type_registry()
    exact_match = registry.resolve_alias(label)
    if exact_match is not None:
        return exact_match.canonical_code
    normalized = _normalized_text(label)
    for page_type in registry.catalog.page_types:
        for alias in page_type.navigation_aliases:
            if any(_normalized_text(term) in normalized for term in alias.terms):
                return page_type.code
    return None


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _attributes_explicitly_hidden(attributes: dict[str, str]) -> bool:
    style = re.sub(r"\s+", "", attributes.get("style", "").casefold())
    return (
        "hidden" in attributes
        or attributes.get("aria-hidden", "").casefold() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
        or "visibility:collapse" in style
    )


def _style_z_index(style: str) -> int | None:
    match = re.search(r"z-index\s*:\s*(-?\d+)", style, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _fingerprint(source: str, selector: str) -> str:
    payload = f"{source}\0{selector}".encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class _NavigationParseItem:
    index: int
    label: str
    active: bool
    family: str = H5_BOTTOM_NAVIGATION_FAMILY_VANT


@dataclass
class _GenericNavigationNode:
    """自定义导航树节点，仅保存探索所需的脱敏结构字段。"""

    tag: str
    attributes: dict[str, str]
    hidden: bool
    text_parts: list[str] = field(default_factory=list)
    children: list[_GenericNavigationNode] = field(default_factory=list)


@dataclass(frozen=True)
class _GenericNavigationParseItem:
    """自定义导航项的可见标签与激活状态。"""

    index: int
    label: str
    active: bool


class _H5GenericNavigationParser(HTMLParser):
    """识别非组件库 SPA 底栏，避免把普通内容列表当作导航。

    解析分两步完成：先建立轻量 DOM 树，再只接受显式导航容器，或包含高置信
    ``footer-item``/``bottom-nav-item`` 等类名的同级项组。这样既覆盖 d24 式
    ``ul.type.color > li.footer-item``，也不会因任意中文文案而猜测页面类型。
    """

    _VOID_TAGS = {
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
    _IGNORED_TAGS = {"script", "style", "template", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _GenericNavigationNode(
            tag="__root__",
            attributes={},
            hidden=False,
        )
        self._stack: list[_GenericNavigationNode] = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.casefold(): (value or "") for name, value in attrs}
        normalized_tag = tag.casefold()
        parent = self._stack[-1]
        hidden = (
            parent.hidden
            or normalized_tag in self._IGNORED_TAGS
            or _generic_navigation_attributes_hidden(attributes)
        )
        node = _GenericNavigationNode(
            tag=normalized_tag,
            attributes=attributes,
            hidden=hidden,
        )
        parent.children.append(node)
        if normalized_tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.casefold(): (value or "") for name, value in attrs}
        normalized_tag = tag.casefold()
        parent = self._stack[-1]
        parent.children.append(
            _GenericNavigationNode(
                tag=normalized_tag,
                attributes=attributes,
                hidden=(
                    parent.hidden
                    or normalized_tag in self._IGNORED_TAGS
                    or _generic_navigation_attributes_hidden(attributes)
                ),
            )
        )

    def handle_data(self, data: str) -> None:
        if self._stack and not self._stack[-1].hidden:
            self._stack[-1].text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def navigation_items(self) -> list[_GenericNavigationParseItem]:
        """返回最可信的一组自定义底栏项，组外文本不会被带入。"""
        groups: list[tuple[int, int, list[_GenericNavigationNode]]] = []

        def walk(node: _GenericNavigationNode, blocked: bool = False) -> None:
            node_classes = _class_tokens(node)
            node_blocked = blocked or bool(
                node_classes & _KNOWN_NAVIGATION_CLASS_MARKERS
            )
            if node_blocked:
                return
            explicit_container = _is_generic_navigation_container(node)
            direct_items = [
                child
                for child in node.children
                if not child.hidden
                and not _generic_navigation_contains_anchor(child)
                and _is_generic_navigation_item(
                    child,
                    parent_is_explicit_container=explicit_container,
                )
            ]
            if len(direct_items) >= 2 and _generic_navigation_group_is_safe(
                direct_items=direct_items,
                parent_is_explicit_container=explicit_container,
            ):
                marker_count = sum(
                    bool(_class_tokens(item) & _GENERIC_IMPLICIT_NAVIGATION_ITEM_CLASSES)
                    for item in direct_items
                )
                score = (
                    100 if explicit_container else 60
                ) + marker_count * 10 + min(len(direct_items), 10)
                groups.append((score, len(direct_items), direct_items))
            for child in node.children:
                if not child.hidden:
                    walk(child, node_blocked)

        walk(self.root)
        if not groups:
            return []
        _, _, nodes = max(groups, key=lambda group: (group[0], group[1]))
        result: list[_GenericNavigationParseItem] = []
        seen_labels: set[str] = set()
        for index, node in enumerate(nodes[:_GENERIC_NAVIGATION_MAX_ITEMS], start=1):
            label = _generic_navigation_label(node)
            normalized_label = _normalized_text(label)
            if not normalized_label or normalized_label in seen_labels:
                continue
            seen_labels.add(normalized_label)
            result.append(
                _GenericNavigationParseItem(
                    index=index,
                    label=label,
                    active=_generic_navigation_item_is_active(node),
                )
            )
        return result if len(result) >= 2 else []


def _class_tokens(node: _GenericNavigationNode) -> set[str]:
    return set(node.attributes.get("class", "").casefold().split())


def _is_generic_navigation_container(node: _GenericNavigationNode) -> bool:
    # 执行前 UI Snapshot 目前只保留祖先 class token；检测阶段也只接受能在
    # 实时快照中独立复核的容器，避免签发只能检测、不能安全执行的候选。
    return bool(_class_tokens(node) & H5_GENERIC_NAVIGATION_CONTAINER_CLASSES)


def _is_generic_navigation_item(
    node: _GenericNavigationNode,
    *,
    parent_is_explicit_container: bool,
) -> bool:
    if node.tag == "a":
        # 锚点已经由同源链接解析负责；此解析器只补非 <a> 事件导航。
        return False
    classes = _class_tokens(node)
    role = node.attributes.get("role", "").casefold()
    if (
        "disabled" in classes
        or "is-disabled" in classes
        or node.attributes.get("disabled") is not None
        or node.attributes.get("aria-disabled", "").casefold() == "true"
    ):
        return False
    if classes & H5_GENERIC_NAVIGATION_ITEM_CLASSES:
        return True
    if role in _GENERIC_NAVIGATION_ITEM_ROLES:
        return True
    if parent_is_explicit_container and (
        node.tag == "button" or node.tag == "li" or "item" in classes
    ):
        return True
    return False


def _generic_navigation_attributes_hidden(attributes: dict[str, str]) -> bool:
    if _attributes_explicitly_hidden(attributes):
        return True
    style = re.sub(r"\s+", "", attributes.get("style", "").casefold())
    return bool(
        re.search(r"(?:^|;)opacity:0(?:\.0+)?(?:;|$)", style)
        or re.search(r"(?:^|;)pointer-events:none(?:;|$)", style)
    )


def _generic_navigation_group_is_safe(
    *,
    direct_items: list[_GenericNavigationNode],
    parent_is_explicit_container: bool,
) -> bool:
    if parent_is_explicit_container:
        return True
    return all(
        bool(_class_tokens(item) & _GENERIC_IMPLICIT_NAVIGATION_ITEM_CLASSES)
        for item in direct_items
    )


def _generic_navigation_label(node: _GenericNavigationNode) -> str:
    for attribute_name in (
        "aria-label",
        "data-label",
        "data-name",
        "title",
    ):
        value = re.sub(r"\s+", " ", node.attributes.get(attribute_name, "")).strip()
        if 0 < len(value) <= _GENERIC_NAVIGATION_MAX_LABEL_LENGTH:
            return value
    text = re.sub(r"\s+", " ", _generic_navigation_text(node)).strip()
    return (
        text
        if 0 < len(text) <= _GENERIC_NAVIGATION_MAX_LABEL_LENGTH
        else ""
    )


def _generic_navigation_text(node: _GenericNavigationNode) -> str:
    parts = list(node.text_parts)
    for child in node.children:
        if not child.hidden:
            parts.append(_generic_navigation_text(child))
    return " ".join(parts)


def _generic_navigation_contains_anchor(node: _GenericNavigationNode) -> bool:
    if node.tag == "a":
        return True
    return any(_generic_navigation_contains_anchor(child) for child in node.children)


def _generic_navigation_item_is_active(node: _GenericNavigationNode) -> bool:
    classes = _class_tokens(node)
    if classes.intersection({"active", "checked", "current", "on", "selected"}):
        return True
    if classes.intersection({"is-active", "is-current", "is-selected"}):
        return True
    return (
        node.attributes.get("aria-selected", "").casefold() == "true"
        or node.attributes.get("aria-current", "").casefold() in {"page", "true"}
    )


class _H5SignalParser(HTMLParser):
    """有限 HTML 解析器，只提取 H5 探索需要的结构信号。"""

    _VOID_TAGS = {
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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_loading_page = False
        self.has_startup_ad_dismissal = False
        self.startup_ad_dismissal_selector: str | None = None
        self.loading_page_z_index: int | None = None
        self.has_dialog_content_close = False
        self.has_dialog_acknowledgement = False
        self.dialog_z_index: int | None = None
        self.tabbar_items: list[_NavigationParseItem] = []
        self.page_tab_items: list[_NavigationParseItem] = []
        self.class_tokens: set[str] = set()
        self.visible_text = ""
        self.business_structure_present = False
        self.safe_content_card_signals: set[str] = set()
        self.explicit_risk_actions: set[str] = set()
        self._stack: list[dict[str, Any]] = []
        self._tabbar_depth: int | None = None
        self._tabbar_item_depth: int | None = None
        self._tabbar_item_index = 0
        self._tabbar_item_active = False
        self._tabbar_item_text: list[str] = []
        self._tabbar_family: str | None = None
        self._page_tabs_depth: int | None = None
        self._page_tab_item_depth: int | None = None
        self._page_tab_item_index = 0
        self._page_tab_item_active = False
        self._page_tab_item_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.casefold(): (value or "") for name, value in attrs}
        inherited_hidden = bool(self._stack[-1]["hidden"]) if self._stack else False
        hidden = inherited_hidden or _attributes_explicitly_hidden(attributes)
        class_names = set(attributes.get("class", "").casefold().split())
        self.class_tokens.update(class_names)
        parent = self._stack[-1] if self._stack else {}
        inside_loading_page = bool(parent.get("inside_loading_page"))
        inside_global_launch = bool(parent.get("inside_global_launch"))
        inside_global_launch_header = bool(parent.get("inside_global_launch_header"))
        inside_countdown = bool(parent.get("inside_countdown"))
        inside_van_popup = bool(parent.get("inside_van_popup"))
        inside_skip_wrap = bool(parent.get("inside_skip_wrap"))
        inside_count_down = bool(parent.get("inside_count_down"))
        inside_launch_page = bool(parent.get("inside_launch_page"))
        inside_screen_ad = bool(parent.get("inside_screen_ad"))
        inside_launch_entry = bool(parent.get("inside_launch_entry"))
        inside_dialog = bool(parent.get("inside_dialog"))
        inside_dialog_content = bool(parent.get("inside_dialog_content"))
        inside_video_list = bool(parent.get("inside_video_list"))
        inside_movie_list = bool(parent.get("inside_movie_list"))
        inside_content_list = bool(parent.get("inside_content_list"))
        inside_post_list = bool(parent.get("inside_post_list"))
        unsafe_card_context = bool(parent.get("unsafe_card_context"))
        inside_footer_wrap = bool(parent.get("inside_footer_wrap"))

        inside_loading_page = inside_loading_page or "loading-page" in class_names
        inside_global_launch = inside_global_launch or "globallaunch" in class_names
        inside_global_launch_header = inside_global_launch_header or (
            inside_global_launch and "header-title" in class_names
        )
        inside_countdown = inside_countdown or "countdown" in class_names
        inside_van_popup = inside_van_popup or "van-popup" in class_names
        inside_skip_wrap = inside_skip_wrap or (
            inside_van_popup and "skip-wrap" in class_names
        )
        inside_count_down = inside_count_down or (
            inside_van_popup and "count-down" in class_names
        )
        inside_launch_page = inside_launch_page or "launch-page" in class_names
        inside_screen_ad = inside_screen_ad or (
            inside_launch_page and "screen-ad" in class_names
        )
        inside_launch_entry = inside_launch_entry or (
            inside_screen_ad and "button" in class_names
        )
        inside_video_list = inside_video_list or "video-list" in class_names
        inside_movie_list = inside_movie_list or "movie-list" in class_names
        inside_content_list = inside_content_list or "content-list" in class_names
        inside_post_list = inside_post_list or "post-list" in class_names
        unsafe_card_context = unsafe_card_context or _classes_look_unsafe(class_names)
        inside_footer_wrap = inside_footer_wrap or (
            tag.casefold() == "div" and "footer-wrap" in class_names and not hidden
        )
        self.business_structure_present |= bool(class_names & _BUSINESS_STRUCTURE_CLASSES)
        explicit_risk_action = attributes.get("data-risk-action", "").strip().casefold()
        if not hidden and explicit_risk_action in _EXPLICIT_RISK_ACTIONS:
            self.explicit_risk_actions.add(explicit_risk_action)

        is_dialog = attributes.get("role", "").casefold() == "dialog" or "van-dialog" in class_names
        inside_dialog = inside_dialog or (is_dialog and not hidden)
        inside_dialog_content = inside_dialog_content or (
            inside_dialog and "van-dialog__content" in class_names
        )

        if "loading-page" in class_names and not hidden:
            self.has_loading_page = True
            self.loading_page_z_index = _max_optional(
                self.loading_page_z_index,
                _style_z_index(attributes.get("style", "")),
            )
        if inside_loading_page and "down-time" in class_names and not hidden:
            self.has_startup_ad_dismissal = True
            self.startup_ad_dismissal_selector = STARTUP_AD_DISMISSAL_SELECTOR
        if "globallaunch" in class_names and not hidden:
            self.loading_page_z_index = _max_optional(
                self.loading_page_z_index,
                _style_z_index(attributes.get("style", "")),
            )
        if is_dialog and not hidden:
            self.dialog_z_index = _max_optional(
                self.dialog_z_index,
                _style_z_index(attributes.get("style", "")),
            )
        if inside_dialog_content and "btn" in class_names and not hidden:
            self.has_dialog_content_close = True
        if inside_dialog and "app-btn" in class_names and not hidden:
            self.has_dialog_acknowledgement = True

        depth = len(self._stack)
        normalized_tag = tag.casefold()
        self._open_tabbar_item(
            tag=normalized_tag,
            class_names=class_names,
            hidden=hidden,
            depth=depth,
            inside_footer_wrap=inside_footer_wrap,
        )
        self._open_page_tab_item(class_names=class_names, hidden=hidden, depth=depth)
        if not hidden:
            self._remember_safe_content_card(
                class_names=class_names,
                unsafe_card_context=unsafe_card_context,
                inside_video_list=inside_video_list,
                inside_movie_list=inside_movie_list,
                inside_content_list=inside_content_list,
                inside_post_list=inside_post_list,
            )

        item = {
            "tag": normalized_tag,
            "hidden": hidden,
            "class_names": frozenset(class_names),
            "inside_loading_page": inside_loading_page,
            "inside_global_launch": inside_global_launch,
            "inside_global_launch_header": inside_global_launch_header,
            "inside_countdown": inside_countdown,
            "inside_van_popup": inside_van_popup,
            "inside_skip_wrap": inside_skip_wrap,
            "inside_count_down": inside_count_down,
            "inside_launch_page": inside_launch_page,
            "inside_screen_ad": inside_screen_ad,
            "inside_launch_entry": inside_launch_entry,
            "inside_dialog": inside_dialog,
            "inside_dialog_content": inside_dialog_content,
            "inside_video_list": inside_video_list,
            "inside_movie_list": inside_movie_list,
            "inside_content_list": inside_content_list,
            "inside_post_list": inside_post_list,
            "unsafe_card_context": unsafe_card_context,
            "inside_footer_wrap": inside_footer_wrap,
        }
        if normalized_tag not in self._VOID_TAGS:
            self._stack.append(item)

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1]["hidden"]:
            return
        self.visible_text += data
        current = self._stack[-1] if self._stack else {}
        current_classes = current.get("class_names", frozenset())
        if (
            current.get("inside_global_launch_header")
            and current.get("tag") == "span"
            and data.strip() == "跳过"
        ):
            self.has_startup_ad_dismissal = True
            self.startup_ad_dismissal_selector = GLOBAL_LAUNCH_DISMISSAL_SELECTOR
        if (
            current.get("inside_countdown")
            and "jump_over" in current_classes
            and data.strip() == "跳过"
        ):
            self.has_startup_ad_dismissal = True
            self.startup_ad_dismissal_selector = COUNTDOWN_JUMP_DISMISSAL_SELECTOR
        if current.get("inside_skip_wrap") and data.strip() == "0s(跳过)":
            self.has_startup_ad_dismissal = True
            self.startup_ad_dismissal_selector = POPUP_SKIP_WRAP_DISMISSAL_SELECTOR
        if current.get("inside_count_down") and data.strip() == "跳过":
            self.has_startup_ad_dismissal = True
            self.startup_ad_dismissal_selector = POPUP_COUNT_DOWN_DISMISSAL_SELECTOR
        if current.get("inside_launch_entry") and data.strip() == "进入":
            self.has_startup_ad_dismissal = True
            self.startup_ad_dismissal_selector = LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR
        if (
            current.get("inside_dialog_content")
            and "btn-box" in current_classes
            and current_classes.intersection({"end", "close"})
            and data.strip().casefold() in {"关闭", "跳过", "close", "dismiss", "skip"}
        ):
            self.has_dialog_content_close = True
        if self._tabbar_item_depth is not None:
            self._tabbar_item_text.append(data)
        if self._page_tab_item_depth is not None:
            self._page_tab_item_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        depth = len(self._stack) - 1
        if self._tabbar_item_depth == depth:
            self._close_tabbar_item()
        if self._page_tab_item_depth == depth:
            self._close_page_tab_item()
        if self._tabbar_depth == depth:
            self._tabbar_depth = None
            self._tabbar_family = None
        if self._page_tabs_depth == depth:
            self._page_tabs_depth = None

        normalized_tag = tag.casefold()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == normalized_tag:
                del self._stack[index:]
                break

    def _open_tabbar_item(
        self,
        *,
        tag: str,
        class_names: set[str],
        hidden: bool,
        depth: int,
        inside_footer_wrap: bool,
    ) -> None:
        if "van-tabbar" in class_names and not hidden and self._tabbar_depth is None:
            self._tabbar_depth = depth
            self._tabbar_family = H5_BOTTOM_NAVIGATION_FAMILY_VANT
            self._tabbar_item_index = 0
        elif (
            tag == "ul"
            and "pages-foot-fix" in class_names
            and not hidden
            and self._tabbar_depth is None
        ):
            self._tabbar_depth = depth
            self._tabbar_family = H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT
            self._tabbar_item_index = 0
        elif (
            tag == "ul"
            and "type" in class_names
            and inside_footer_wrap
            and not hidden
            and self._tabbar_depth is None
        ):
            self._tabbar_depth = depth
            self._tabbar_family = H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP
            self._tabbar_item_index = 0
        elif (
            self._tabbar_depth is not None
            and (
                (
                    self._tabbar_family == H5_BOTTOM_NAVIGATION_FAMILY_VANT
                    and "van-tabbar-item" in class_names
                )
                or (
                    self._tabbar_family == H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT
                    and tag == "li"
                    and depth == self._tabbar_depth + 1
                )
                or (
                    self._tabbar_family == H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP
                    and tag == "li"
                    and "footer-item" in class_names
                    and depth == self._tabbar_depth + 1
                )
            )
            and not hidden
            and self._tabbar_item_depth is None
        ):
            self._tabbar_item_index += 1
            self._tabbar_item_depth = depth
            self._tabbar_item_active = bool(
                "van-tabbar-item--active" in class_names
                or (
                    self._tabbar_family == H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT
                    and "active" in class_names
                )
                or (
                    self._tabbar_family == H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP
                    and "active" in class_names
                )
            )
            self._tabbar_item_text = []
        if (
            self._tabbar_item_depth is not None
            and self._tabbar_family == H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP
            and "ico-act" in class_names
            and not hidden
        ):
            self._tabbar_item_active = True

    def _open_page_tab_item(
        self,
        *,
        class_names: set[str],
        hidden: bool,
        depth: int,
    ) -> None:
        if "van-tabs" in class_names and not hidden and self._page_tabs_depth is None:
            self._page_tabs_depth = depth
            self._page_tab_item_index = 0
        elif (
            self._page_tabs_depth is not None
            and "van-tab" in class_names
            and "van-tabbar-item" not in class_names
            and not hidden
            and self._page_tab_item_depth is None
        ):
            self._page_tab_item_index += 1
            self._page_tab_item_depth = depth
            self._page_tab_item_active = "van-tab--active" in class_names
            self._page_tab_item_text = []

    def _close_tabbar_item(self) -> None:
        label = " ".join(part.strip() for part in self._tabbar_item_text).strip()
        if label:
            self.tabbar_items.append(
                _NavigationParseItem(
                    index=self._tabbar_item_index,
                    label=label,
                    active=self._tabbar_item_active,
                    family=(
                        self._tabbar_family or H5_BOTTOM_NAVIGATION_FAMILY_VANT
                    ),
                )
            )
        self._tabbar_item_depth = None
        self._tabbar_item_active = False
        self._tabbar_item_text = []

    def _close_page_tab_item(self) -> None:
        label = " ".join(part.strip() for part in self._page_tab_item_text).strip()
        if label:
            self.page_tab_items.append(
                _NavigationParseItem(
                    index=self._page_tab_item_index,
                    label=label,
                    active=self._page_tab_item_active,
                )
            )
        self._page_tab_item_depth = None
        self._page_tab_item_active = False
        self._page_tab_item_text = []

    def _remember_safe_content_card(
        self,
        *,
        class_names: set[str],
        unsafe_card_context: bool,
        inside_video_list: bool,
        inside_movie_list: bool,
        inside_content_list: bool,
        inside_post_list: bool,
    ) -> None:
        if unsafe_card_context or not class_names.intersection(_CONTENT_CARD_CLASSES):
            return
        if "media-item" in class_names:
            self.safe_content_card_signals.add("generic_media")
            if inside_video_list:
                self.safe_content_card_signals.add("video_list_media")
            if inside_movie_list:
                self.safe_content_card_signals.add("movie_list_media")
        if "data-item" in class_names:
            self.safe_content_card_signals.add("generic_data")
            if inside_content_list:
                self.safe_content_card_signals.add("content_list_data")
        if "post-item" in class_names:
            self.safe_content_card_signals.add("generic_post")
            if inside_post_list:
                self.safe_content_card_signals.add("post_list_post")
        if "feed-item" in class_names:
            self.safe_content_card_signals.add("generic_feed")
        if "video-item" in class_names:
            self.safe_content_card_signals.add("generic_video")
        if "longvideocard" in class_names:
            self.safe_content_card_signals.add("long_video_card")
        if "content-card" in class_names:
            self.safe_content_card_signals.add("generic_content_card")


def _classes_look_unsafe(class_names: set[str]) -> bool:
    return any(marker in token for token in class_names for marker in _UNSAFE_CARD_MARKERS)


def _max_optional(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)
