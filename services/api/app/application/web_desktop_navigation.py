"""桌面 Web 导航结构解析与菜单身份生成。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from math import isfinite
from urllib.parse import urljoin, urlsplit, urlunsplit

from services.api.app.application.web_navigation_container_identity import (
    WebNavigationContainerRegistry,
    navigation_container_descriptor,
    resolve_navigation_container_identities,
)
from services.api.app.application.web_navigation_lineage import (
    inherit_navigation_lineage,
    logical_navigation_identity,
)
from services.api.app.application.web_navigation_recognition import (
    WEB_NAVIGATION_CONFIDENCE_FLOOR,
    assess_navigation_container,
    navigation_entry_kind,
    navigation_hit_test_is_positive,
    navigation_is_obstructed,
    navigation_is_outside_viewport,
    navigation_runtime_evidence_is_trusted,
)
from services.api.app.application.web_navigation_snapshot_alignment import (
    align_navigation_snapshot_nodes,
)
from services.api.app.domain.page.web_origin import canonical_web_origin, web_origin_key

WEB_NAVIGATION_SOURCE_SELECTOR = (
    "nav :is(a[href],[role='link'],button,[role='button'],router-link,[onclick],"
    "[data-route]), [role='navigation'] :is(a[href],[role='link'],button,"
    "[role='button'],router-link,[onclick],[data-route]), "
    '[class*="nav" i] :is(a[href],[role="link"],button,[role="button"],'
    "router-link,[onclick],[data-route])"
)
# 兼容既有导入名；实际规则不绑定某个 navbar class。
WEB_NAVBAR_SOURCE_SELECTOR = WEB_NAVIGATION_SOURCE_SELECTOR


@dataclass(frozen=True, kw_only=True)
class WebNavigationCandidate:
    """从 DOM 得到的导航候选；候选身份不等于页面到达证明。"""

    entry_id: str
    label: str
    url: str | None
    menu_path: tuple[str, ...]
    dom_fingerprint: str
    source_signal: str
    page_type_key: str
    source_selector: str = WEB_NAVIGATION_SOURCE_SELECTOR
    menu_position: int = 1
    discovery_capture_id: str | None = None
    source_capture_id: str | None = None
    source_page_url: str | None = None
    source_kind: str = "anchor"
    target_hint: str = ""
    source_epoch: int = 0
    visibility: str = "visible"
    actionability: str = "actionable"
    risk_level: str = "low"
    allowlist_match: bool = False
    identity_review_reason: str | None = None
    side_effect_kind: str | None = None
    navigation_container_id: str | None = None
    navigation_container_restore_seed: Mapping[str, object] | None = None
    navigation_evidence: tuple[str, ...] = ()
    parent_menu_entry_id: str | None = None
    parent_logical_target_identity: str | None = None
    menu_depth: int = 1
    exploration_path: tuple[str, ...] = ()
    logical_target_identity: str = ""
    frontier_route_shape: str | None = None
    frontier_dom_role: str | None = None
    frontier_structure_fingerprint: str | None = None
    frontier_structure_cluster_key: str | None = None
    frontier_site_origin: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.menu_depth, bool)
            or not isinstance(self.menu_depth, int)
            or self.menu_depth < 1
        ):
            raise ValueError("WEB_MENU_LINEAGE_INVALID: menu_depth 必须为正整数")
        path = self.exploration_path or self.menu_path
        if (
            not isinstance(path, (tuple, list))
            or not path
            or any(not isinstance(item, str) or not item.strip() for item in path)
            or " ".join(path[-1].split()).casefold() != " ".join(self.label.split()).casefold()
        ):
            raise ValueError("WEB_MENU_LINEAGE_INVALID: exploration_path 必须非空且末项为当前标签")
        object.__setattr__(self, "exploration_path", tuple(" ".join(item.split()) for item in path))
        object.__setattr__(self, "logical_target_identity", logical_navigation_identity(self))
        if self.source_kind not in {
            "anchor",
            "semantic_link",
            "button",
            "js_control",
            "spa_route",
            "visual_card",
        }:
            raise ValueError("source_kind 不受支持")
        if self.visibility not in {"visible", "hidden"}:
            raise ValueError("visibility 不受支持")
        if self.actionability not in {
            "actionable",
            "requires_runtime_validation",
            "blocked",
        }:
            raise ValueError("actionability 不受支持")
        if (
            isinstance(self.source_epoch, bool)
            or not isinstance(self.source_epoch, int)
            or self.source_epoch < 0
        ):
            raise ValueError("source_epoch 必须为非负整数")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "entry_id": self.entry_id,
            "label": self.label,
            "url": self.url,
            "menu_path": list(self.menu_path),
            "dom_fingerprint": self.dom_fingerprint,
            "source_signal": self.source_signal,
            "page_type_key": self.page_type_key,
            "source_selector": self.source_selector,
            "menu_position": self.menu_position,
            "source_capture_id": self.source_capture_id,
            "source_page_url": self.source_page_url,
            "source_kind": self.source_kind,
            "target_hint": self.target_hint,
            "source_epoch": self.source_epoch,
            "visibility": self.visibility,
            "actionability": self.actionability,
            "risk_level": self.risk_level,
            "allowlist_match": self.allowlist_match,
            "navigation_container_id": self.navigation_container_id,
            "navigation_container_restore_seed": (
                dict(self.navigation_container_restore_seed)
                if self.navigation_container_restore_seed is not None
                else None
            ),
            "navigation_evidence": list(self.navigation_evidence),
            "parent_menu_entry_id": self.parent_menu_entry_id,
            "parent_logical_target_identity": self.parent_logical_target_identity,
            "menu_depth": self.menu_depth,
            "exploration_path": list(self.exploration_path),
            "logical_target_identity": self.logical_target_identity,
            "frontier_route_shape": self.frontier_route_shape,
            "frontier_dom_role": self.frontier_dom_role,
            "frontier_structure_fingerprint": self.frontier_structure_fingerprint,
            "frontier_structure_cluster_key": self.frontier_structure_cluster_key,
            "frontier_site_origin": self.frontier_site_origin,
        }
        if self.identity_review_reason is not None:
            payload["identity_review_reason"] = self.identity_review_reason
        if self.side_effect_kind is not None:
            payload["side_effect_kind"] = self.side_effect_kind
        if self.discovery_capture_id is not None:
            payload["discovery_capture_id"] = self.discovery_capture_id
        return payload


def isolate_navigation_candidate_identity(candidate, *, identity_policy=None):
    """非权威候选单独待复核；原 href 留在 target_hint，不授予导航 URL。"""
    from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY

    policy = identity_policy or DEFAULT_WEB_URL_IDENTITY_POLICY
    if candidate.url is None:
        return candidate
    try:
        policy.canonical(candidate.url)
    except ValueError as exc:
        if str(exc) == "WEB_POLICY_VERSION_UNSUPPORTED":
            raise
        return replace(
            candidate,
            url=None,
            actionability="blocked",
            allowlist_match=False,
            target_hint=candidate.target_hint or candidate.url,
            identity_review_reason=(
                "WEB_URL_HASH_NEEDS_REVIEW"
                if str(exc) == "WEB_URL_HASH_NEEDS_REVIEW"
                else "WEB_URL_IDENTITY_INVALID"
            ),
        )
    return candidate


@dataclass
class _OpenElement:
    tag: str
    in_navigation: bool
    hidden: bool
    source_signal: str | None
    excluded_navigation_region: bool
    visual_card: bool
    container_id: str | None = None
    exclusion_kind: str | None = None
    li_label: str | None = None
    direct_text_parts: list[str] = field(default_factory=list)
    dom_path: tuple[int, ...] = ()
    child_count: int = 0
    disabled: bool = False
    inert_template: bool = False
    top_navigation_structural: bool = False
    within_header: bool = False


@dataclass
class _OpenControl:
    tag: str
    target_hint: str
    hidden: bool
    excluded_navigation_region: bool
    visual_card: bool
    source_signal: str
    source_kind: str
    dom_order: int
    stack_signature: tuple[str, ...]
    text_parts: list[str]
    side_effect_kind: str | None
    container_id: str | None
    exclusion_kind: str | None
    class_tokens: tuple[str, ...]
    aria_expanded: str | None
    accessible_label: str
    dom_path: tuple[int, ...]
    disabled: bool
    inert_template: bool
    top_navigation_structural: bool
    within_header: bool


@dataclass(frozen=True)
class _RawNavigationLink:
    target_hint: str
    label: str
    parent_path: tuple[str, ...]
    source_signal: str
    dom_order: int
    dom_fingerprint: str
    source_kind: str
    hidden: bool
    excluded_navigation_region: bool
    visual_card: bool
    side_effect_kind: str | None
    container_id: str | None
    exclusion_kind: str | None
    class_tokens: tuple[str, ...]
    aria_expanded: str | None
    tag: str
    dom_path: tuple[int, ...]
    disabled: bool
    inert_template: bool
    top_navigation_structural: bool
    within_header: bool


class _NavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_OpenElement] = []
        self.control: _OpenControl | None = None
        self.links: list[_RawNavigationLink] = []
        self._anchor_order = 0
        self._container_order = 0
        self._root_element_count = 0
        self.container_descriptors: dict[str, dict[str, object]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        values = {name.casefold(): value for name, value in attrs}
        if self.stack:
            self.stack[-1].child_count += 1
            dom_path = (*self.stack[-1].dom_path, self.stack[-1].child_count)
        else:
            self._root_element_count += 1
            dom_path = (self._root_element_count,)
        disabled = (
            "disabled" in values
            or "inert" in values
            or str(values.get("aria-disabled") or "").casefold() == "true"
            or bool(self.stack and self.stack[-1].disabled)
        )
        if self.control is not None and normalized_tag == "img":
            image_identity = " ".join(
                str(values.get(name) or "") for name in ("class", "src", "alt")
            )
            if "logo" in image_identity.casefold():
                self.control.class_tokens = (*self.control.class_tokens, "logo")
        inherited_source = next(
            (
                item.source_signal
                for item in reversed(self.stack)
                if item.in_navigation and item.source_signal is not None
            ),
            None,
        )
        own_source = _navigation_source_signal(normalized_tag, values)
        source_signal = inherited_source or own_source
        within_header = normalized_tag == "header" or any(item.within_header for item in self.stack)
        inherited_top_navigation = next(
            (item.top_navigation_structural for item in reversed(self.stack) if item.in_navigation),
            False,
        )
        top_navigation_structural = inherited_top_navigation or bool(
            own_source
            and (
                within_header
                or not any(
                    item.tag in {"main", "article", "aside", "footer", "section", "form"}
                    for item in self.stack
                )
            )
        )
        inherited_hidden = bool(self.stack and self.stack[-1].hidden)
        inherited_inert_template = bool(self.stack and self.stack[-1].inert_template)
        inert_template = inherited_inert_template or normalized_tag in {
            "template",
            "script",
            "style",
            "noscript",
        }
        inherited_excluded = bool(self.stack and self.stack[-1].excluded_navigation_region)
        hidden = inherited_hidden or inert_template or _attributes_hide_element(values)
        in_navigation = source_signal is not None
        excluded_navigation_region = inherited_excluded or _excluded_navigation_region(
            normalized_tag,
            values,
        )
        inherited_visual_card = bool(self.stack and self.stack[-1].visual_card)
        visual_card = inherited_visual_card or _is_visual_card(normalized_tag, values)
        container_id = next((item.container_id for item in self.stack if item.container_id), None)
        if own_source and container_id is None and normalized_tag not in {"body", "html"}:
            self._container_order += 1
            # 顺序仅用于解析期分组；对外身份在完整可见入口形成后按语义签发。
            container_id = f"parse-container:{self._container_order}"
            self.container_descriptors[container_id] = navigation_container_descriptor(
                normalized_tag, values
            )
        exclusion_kind = next(
            (item.exclusion_kind for item in reversed(self.stack) if item.exclusion_kind), None
        )
        if excluded_navigation_region and exclusion_kind is None:
            marker = _navigation_region_marker(values)
            exclusion_kind = (
                "footer"
                if normalized_tag == "footer" or "footer" in marker
                else "pagination"
                if re.search(r"pagination|pager", marker)
                else "breadcrumb"
                if re.search(r"breadcrumb|bread-crumb", marker)
                else "advertisement"
            )

        source_kind = _navigation_source_kind(normalized_tag, values, visual_card)
        if source_kind is not None and container_id is not None and self.control is None:
            self._promote_open_list_labels()
            self._anchor_order += 1
            self.control = _OpenControl(
                tag=normalized_tag,
                target_hint=_target_hint(values),
                hidden=hidden,
                excluded_navigation_region=excluded_navigation_region,
                visual_card=visual_card,
                source_signal=str(source_signal or source_kind),
                source_kind=source_kind,
                dom_order=self._anchor_order,
                stack_signature=tuple(item.tag for item in self.stack),
                text_parts=[],
                accessible_label=str(values.get("aria-label") or ""),
                dom_path=dom_path,
                disabled=disabled,
                inert_template=inert_template,
                top_navigation_structural=top_navigation_structural,
                within_header=within_header,
                container_id=container_id,
                exclusion_kind=exclusion_kind,
                class_tokens=tuple(str(values.get("class") or "").split()),
                aria_expanded=values.get("aria-expanded"),
                side_effect_kind=(
                    "download"
                    if "download" in values
                    else "form_action"
                    if "form" in values
                    or any(item.tag == "form" for item in self.stack)
                    or (
                        normalized_tag in {"button", "input"}
                        and str(values.get("type") or "").casefold() in {"submit", "reset"}
                    )
                    else None
                ),
            )
        if normalized_tag in {
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
        }:
            return
        self.stack.append(
            _OpenElement(
                tag=normalized_tag,
                in_navigation=in_navigation,
                hidden=hidden,
                source_signal=source_signal,
                excluded_navigation_region=excluded_navigation_region,
                visual_card=visual_card,
                container_id=container_id,
                exclusion_kind=exclusion_kind,
                dom_path=dom_path,
                disabled=disabled,
                inert_template=inert_template,
                top_navigation_structural=top_navigation_structural,
                within_header=within_header,
            )
        )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self.control is not None and normalized_tag == self.control.tag:
            control = self.control
            label = _normalize_label("".join(control.text_parts)) or _normalize_label(
                control.accessible_label
            )
            if label or control.target_hint:
                parent_path = tuple(
                    item.li_label for item in self.stack if item.tag == "li" and item.li_label
                )
                fingerprint_source = "\x1f".join(
                    (
                        control.source_signal,
                        "/".join(control.stack_signature),
                        control.target_hint,
                        str(control.dom_order),
                    )
                )
                self.links.append(
                    _RawNavigationLink(
                        target_hint=control.target_hint,
                        label=label,
                        parent_path=parent_path,
                        source_signal=control.source_signal,
                        dom_order=control.dom_order,
                        dom_fingerprint=_short_hash(fingerprint_source),
                        source_kind=control.source_kind,
                        hidden=control.hidden,
                        excluded_navigation_region=control.excluded_navigation_region,
                        visual_card=control.visual_card,
                        side_effect_kind=control.side_effect_kind,
                        container_id=control.container_id,
                        exclusion_kind=control.exclusion_kind,
                        class_tokens=control.class_tokens,
                        aria_expanded=control.aria_expanded,
                        tag=control.tag,
                        dom_path=control.dom_path,
                        disabled=control.disabled,
                        inert_template=control.inert_template,
                        top_navigation_structural=control.top_navigation_structural,
                        within_header=control.within_header,
                    )
                )
            if label:
                nearest_li = next(
                    (item for item in reversed(self.stack) if item.tag == "li"),
                    None,
                )
                if nearest_li is not None and nearest_li.li_label is None:
                    nearest_li.li_label = label
            self.control = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == normalized_tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.control is not None:
            self.control.text_parts.append(data)
            return
        nearest_li = next(
            (
                item
                for item in reversed(self.stack)
                if item.tag == "li" and item.in_navigation and not item.hidden
            ),
            None,
        )
        if nearest_li is not None:
            nearest_li.direct_text_parts.append(data)

    def _promote_open_list_labels(self) -> None:
        for item in self.stack:
            if item.tag != "li" or item.li_label is not None:
                continue
            label = _normalize_label("".join(item.direct_text_parts))
            if label:
                item.li_label = label


def extract_navigation_candidates(
    html: str,
    *,
    base_url: str,
    source_capture_id: str | None = None,
    source_epoch: int = 0,
    include_blocked: bool = False,
    snapshot: Mapping[str, object] | None = None,
    parent_candidate: WebNavigationCandidate | None = None,
    parent_menu_entry_id: str | None = None,
    container_registry=None,
    require_runtime_hit_test: bool = False,
) -> tuple[WebNavigationCandidate, ...]:
    """提取多形态导航候选；风险事实保留，但只有安全同源项可执行。"""

    if not isinstance(html, str):
        raise TypeError("html 必须为字符串")
    base = urlsplit(base_url)
    if base.scheme.casefold() not in {"http", "https"} or not base.hostname:
        raise ValueError("base_url 必须为绝对 HTTP(S) URL")
    parser = _NavigationParser()
    parser.feed(html)
    parser.close()
    navigation_snapshot = navigation_snapshot_from_parser(
        parser,
        snapshot=snapshot,
        base_url=base_url,
        container_registry=container_registry,
        require_runtime_hit_test=require_runtime_hit_test,
    )
    assessment = assess_navigation_container(navigation_snapshot)
    dom_href_proven_container_ids = _dom_href_proven_container_ids(
        parser.links,
        navigation_snapshot["elements"],
        base_url=base_url,
    )
    candidates: list[WebNavigationCandidate] = []
    if isinstance(source_epoch, bool) or not isinstance(source_epoch, int) or source_epoch < 0:
        raise ValueError("source_epoch 必须为非负整数")
    occurrence_by_identity: dict[tuple[str, tuple[str, ...]], int] = {}
    site_origin = urlunsplit((base.scheme, base.netloc, "", "", ""))
    for raw, element in zip(parser.links, navigation_snapshot["elements"], strict=True):
        resolved, target_risk = _resolve_navigation_target(
            raw.target_hint,
            base_url=base_url,
        )
        display_label = str(element.get("text") or raw.label) or (
            _fallback_label(resolved) if resolved is not None else "navigation-control"
        )
        # 面包屑排除优先于 disabled 等审计分类，避免当前位置路径被保留为
        # blocked 候选并污染预算或父子血缘。
        if element.get("within_breadcrumb") is True:
            continue
        if element.get("top_navigation_position_proven") is not True and not include_blocked:
            continue
        entry_kind = navigation_entry_kind(element)
        runtime_evidence_trusted = navigation_runtime_evidence_is_trusted(element)
        runtime_hit_test_positive = navigation_hit_test_is_positive(element)
        stable_container_id = element["navigation_container_id"]
        direct_href_navigation = (
            resolved is not None
            and target_risk == "low"
            and entry_kind == "category"
            and element.get("top_navigation_position_proven") is True
            and not raw.excluded_navigation_region
            and not raw.visual_card
            and not raw.inert_template
            and _dom_href_confidence_is_trusted(element)
            and stable_container_id in dom_href_proven_container_ids
        )
        if (
            not direct_href_navigation
            and (not runtime_evidence_trusted or not runtime_hit_test_positive)
        ) and not include_blocked:
            continue
        dangerous = (
            raw.excluded_navigation_region
            or raw.visual_card
            or entry_kind != "category"
            or not _is_low_risk_navigation(
                display_label,
                resolved or raw.target_hint,
            )
        )
        if include_blocked:
            dangerous = dangerous or bool(
                re.search(
                    r"登录|注册|授权|提交|保存|删除|支付|购买|下载|login|signin|oauth|authorize|submit|delete|checkout|download",
                    display_label + " " + raw.target_hint,
                    re.IGNORECASE,
                )
            )
        if not direct_href_navigation and element["visible"] is not True:
            continue
        container_proven = (
            stable_container_id in assessment.proven_container_ids
            or stable_container_id in dom_href_proven_container_ids
        )
        container_review_reason = element.get("navigation_identity_review_reason")
        obstructed = navigation_is_obstructed(navigation_snapshot) or (
            not direct_href_navigation
            and (
                element.get("occluded") is True
                or element.get("obstructed") is True
                or not runtime_evidence_trusted
                or not runtime_hit_test_positive
            )
        )
        if not include_blocked and (
            dangerous
            or not container_proven
            or obstructed
            or target_risk in {"cross_origin", "dangerous"}
            or container_review_reason
        ):
            continue
        risk_level = (
            "identity_unproven"
            if container_review_reason
            else "obstructed"
            if obstructed
            else "advertisement"
            if entry_kind == "advertisement"
            else "cross_origin"
            if target_risk == "cross_origin"
            else "dangerous"
            if dangerous or raw.side_effect_kind
            else "unproven_navigation"
            if not container_proven
            else target_risk
        )
        actionability = (
            "blocked"
            if risk_level != "low"
            else "actionable"
            if resolved is not None
            else "requires_runtime_validation"
        )
        menu_path = (*raw.parent_path, display_label)
        target_identity = resolved or raw.target_hint or display_label
        occurrence_identity = (target_identity, menu_path)
        menu_position = occurrence_by_identity.get(occurrence_identity, 0) + 1
        occurrence_by_identity[occurrence_identity] = menu_position
        identity_source = "\x1f".join(
            (
                site_origin,
                source_capture_id or "unbound",
                f"epoch:{source_epoch}",
                f"web-nav-dom:{raw.dom_fingerprint}",
                "/".join(menu_path),
                target_identity,
                str(menu_position),
            )
        )
        candidates.append(
            WebNavigationCandidate(
                entry_id=f"web-nav-entry:{_short_hash(identity_source)}",
                label=display_label,
                url=resolved,
                menu_path=menu_path,
                dom_fingerprint=f"web-nav-dom:{raw.dom_fingerprint}",
                source_signal=raw.source_signal,
                page_type_key=f"nav-class:{_page_type_token(display_label, resolved)}",
                menu_position=menu_position,
                source_capture_id=source_capture_id,
                source_page_url=base_url,
                source_kind=raw.source_kind,
                target_hint=raw.target_hint or target_identity,
                source_epoch=source_epoch,
                visibility="visible" if element.get("visible") is True else "hidden",
                actionability=actionability,
                risk_level=risk_level,
                side_effect_kind=raw.side_effect_kind,
                navigation_container_id=stable_container_id,
                navigation_container_restore_seed=element.get("navigation_container_restore_seed"),
                identity_review_reason=container_review_reason,
                menu_depth=1,
                exploration_path=menu_path,
                navigation_evidence=(
                    "WEB_NAVIGATION_CONTAINER_PROVEN"
                    if container_proven
                    else "WEB_NAVIGATION_CONTAINER_UNPROVEN",
                    *(
                        (f"WEB_MENU_{entry_kind.upper()}_BLOCKED",)
                        if entry_kind != "category"
                        else ()
                    ),
                    *(("WEB_MENU_OBSTRUCTED",) if obstructed else ()),
                    *(("WEB_MENU_CROSS_ORIGIN_BLOCKED",) if target_risk == "cross_origin" else ()),
                    *(("WEB_NAVIGATION_EXPANDED",) if raw.aria_expanded == "true" else ()),
                    *(("WEB_NAVIGATION_COLLAPSED",) if raw.aria_expanded == "false" else ()),
                ),
            )
        )
    visible_identities = {
        (candidate.url or candidate.target_hint, candidate.label)
        for candidate in candidates
        if candidate.visibility == "visible"
    }
    deduplicated = [
        candidate
        for candidate in candidates
        if candidate.visibility == "visible"
        or (candidate.url or candidate.target_hint, candidate.label) not in visible_identities
    ]
    return tuple(
        inherit_navigation_lineage(
            candidate,
            parent_candidate=parent_candidate,
            parent_menu_entry_id=parent_menu_entry_id,
        )
        for candidate in deduplicated
    )


def navigation_snapshot_from_parser(
    parser,
    *,
    snapshot=None,
    base_url="",
    container_registry=None,
    require_runtime_hit_test=False,
):
    """HTML 提供完整容器范围，稀疏浏览器快照补充实际可见及遮挡事实。"""
    observed = snapshot or {}
    current_runtime_snapshot = require_runtime_hit_test or observed.get("schema_version") in {
        "ui-snapshot.v1",
        "ui-snapshot.v2",
    }
    matched_nodes = align_navigation_snapshot_nodes(
        parser.links,
        observed,
        base_url=base_url,
        target_hint=_target_hint,
    )
    elements = []
    for index, raw in enumerate(parser.links):
        element = {
            "tag": raw.tag,
            "text": raw.label,
            "href": raw.target_hint,
            "class_tokens": list(raw.class_tokens),
            "interactive": True,
            "visible": not raw.hidden,
            "within_web_navigation": True,
            "navigation_container_id": raw.container_id,
            "navigation_menu_path": list(raw.parent_path),
            "within_footer": raw.exclusion_kind == "footer",
            "within_breadcrumb": raw.exclusion_kind == "breadcrumb",
            "within_pagination": raw.exclusion_kind == "pagination",
            "is_advertisement": raw.exclusion_kind == "advertisement",
            "side_effect_kind": raw.side_effect_kind,
            "aria_expanded": raw.aria_expanded,
            "target_risk": _resolve_navigation_target(raw.target_hint, base_url=base_url)[1],
            "disabled": raw.disabled,
            "runtime_hit_test_required": current_runtime_snapshot,
        }
        item = matched_nodes.get(index)
        if item is not None:
            for key in (
                "visible",
                "occluded",
                "obstructed",
                "within_footer",
                "within_breadcrumb",
                "is_advertisement",
                "within_pagination",
                "web_navigation_disabled",
                "aria_disabled",
                "inert",
                "navigation_confidence",
                "hit_test",
            ):
                if key in item:
                    element[key] = item[key]
            element["disabled"] = raw.disabled or item.get("disabled") is True
            if navigation_is_outside_viewport(item, observed):
                element["visible"] = False
            element["text"] = " ".join(str(item.get("text") or raw.label).split())
            hit_test = item.get("hit_test")
            if isinstance(hit_test, Mapping):
                element["occluded"] = (
                    element.get("occluded") is True
                    or hit_test.get("center_hits_target") is False
                    or hit_test.get("visual_occlusion_detected") is True
                )
        element["visible"] = not raw.hidden and element["visible"] is True
        if observed.get("schema_version") in {"ui-snapshot.v1", "ui-snapshot.v2"} and item is None:
            # 生产稀疏快照仅采可见节点；缺失不是允许 HTML 自报可见的凭证。
            element["visible"] = False
        elements.append(element)
    top_container_ids = _top_navigation_container_ids(
        parser.links,
        matched_nodes,
        observed,
        current_runtime_snapshot=current_runtime_snapshot,
    )
    fully_hidden_top_container_ids = _fully_hidden_top_navigation_container_ids(
        parser.links,
        elements,
    )
    top_container_ids = frozenset((*top_container_ids, *fully_hidden_top_container_ids))
    for raw, element in zip(parser.links, elements, strict=True):
        fully_hidden = raw.container_id in fully_hidden_top_container_ids
        element["top_navigation_fully_hidden"] = fully_hidden
        element["top_navigation_position_proven"] = raw.container_id in top_container_ids
    registry = container_registry or WebNavigationContainerRegistry()
    container_ids, ambiguous_ids = resolve_navigation_container_identities(
        parser.container_descriptors, elements, registry=registry
    )
    for element in elements:
        element["navigation_container_id"] = container_ids.get(element["navigation_container_id"])
        element["navigation_container_restore_seed"] = registry.seed_for(
            element["navigation_container_id"]
        )
        if element["navigation_container_id"] in ambiguous_ids:
            element["navigation_identity_review_reason"] = (
                "WEB_NAVIGATION_CONTAINER_IDENTITY_AMBIGUOUS"
            )
    return {"elements": elements, "human_verification_present": navigation_is_obstructed(observed)}


def _dom_href_proven_container_ids(
    links,
    elements,
    *,
    base_url: str,
) -> frozenset[str]:
    """DOM 导航容器内两个以上安全 href 足以证明可直接导航。"""

    grouped_targets: dict[str, set[str]] = {}
    for raw, element in zip(links, elements, strict=True):
        resolved, target_risk = _resolve_navigation_target(raw.target_hint, base_url=base_url)
        label = str(element.get("text") or raw.label) or (
            _fallback_label(resolved) if resolved is not None else "navigation-control"
        )
        container_id = element.get("navigation_container_id")
        if (
            not isinstance(container_id, str)
            or not container_id
            or element.get("navigation_identity_review_reason")
            or navigation_entry_kind(element) != "category"
            or raw.excluded_navigation_region
            or raw.visual_card
            or raw.inert_template
            or element.get("top_navigation_position_proven") is not True
            or resolved is None
            or target_risk != "low"
            or raw.side_effect_kind
            or not _dom_href_confidence_is_trusted(element)
            or not _is_low_risk_navigation(label, resolved)
        ):
            continue
        grouped_targets.setdefault(container_id, set()).add(resolved)
    return frozenset(
        container_id for container_id, targets in grouped_targets.items() if len(targets) >= 2
    )


def _runtime_navigation_is_in_top_band(
    element: Mapping[str, object] | None,
    snapshot: Mapping[str, object],
) -> bool:
    """运行时仅接受视口顶部 35% 内的导航容器成员。"""

    viewport = snapshot.get("viewport")
    if not isinstance(element, Mapping):
        return not isinstance(viewport, Mapping)
    rect = element.get("rect")
    if not isinstance(rect, Mapping) or not isinstance(viewport, Mapping):
        # 缺少运行时几何时仍需通过解析出的顶层结构门；生产快照同时具备
        # viewport 与 rect，测试/历史证据则保持兼容且不伪造几何。
        return True
    if element.get("visible") is not True:
        return False
    values = tuple(rect.get(key) for key in ("x", "y", "width", "height")) + tuple(
        viewport.get(key) for key in ("width", "height")
    )
    if not all(
        isinstance(value, int | float) and not isinstance(value, bool) and isfinite(float(value))
        for value in values
    ):
        return False
    x, y, width, height, viewport_width, viewport_height = map(float, values)
    if width <= 0 or height <= 0 or viewport_width <= 0 or viewport_height <= 0:
        return False
    return bool(
        x + width > 0 and x < viewport_width and y >= 0 and y + height / 2 <= viewport_height * 0.35
    )


def _fully_hidden_top_navigation_container_ids(
    links: Sequence[_RawNavigationLink],
    elements: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    """整个顶层导航隐藏时仍以结构证明其 href 来源。"""

    grouped: dict[str, list[tuple[_RawNavigationLink, Mapping[str, object]]]] = {}
    for raw, element in zip(links, elements, strict=True):
        if raw.container_id:
            grouped.setdefault(raw.container_id, []).append((raw, element))
    return frozenset(
        container_id
        for container_id, members in grouped.items()
        if members
        and all(element.get("visible") is not True for _raw, element in members)
        and all(
            (raw.within_header or raw.top_navigation_structural)
            and not raw.excluded_navigation_region
            and not raw.visual_card
            for raw, _element in members
        )
        and any(
            raw.source_signal in {"semantic_nav", "role_navigation"} or raw.within_header
            for raw, _element in members
        )
    )


def _top_navigation_container_ids(
    links: Sequence[_RawNavigationLink],
    matched_nodes: Mapping[int, Mapping[str, object]],
    snapshot: Mapping[str, object],
    *,
    current_runtime_snapshot: bool,
) -> frozenset[str]:
    """证明顶部导航容器；任一可见成员证明位置后接纳容器内全部 href。"""

    grouped: dict[str, list[tuple[_RawNavigationLink, Mapping[str, object] | None]]] = {}
    for index, raw in enumerate(links):
        if raw.container_id:
            grouped.setdefault(raw.container_id, []).append((raw, matched_nodes.get(index)))

    proven: set[str] = set()
    for container_id, members in grouped.items():
        structural = [
            member
            for member in members
            if (
                member[0].within_header
                or member[0].top_navigation_structural
                or member[0].source_signal
                in {
                    "semantic_nav",
                    "role_navigation",
                    "class_or_id_contains_nav",
                    "navigation_attribute",
                }
            )
            and not member[0].excluded_navigation_region
            and not member[0].visual_card
        ]
        if not structural:
            continue
        if not current_runtime_snapshot:
            proven.add(container_id)
            continue
        if any(_runtime_navigation_is_in_top_band(item, snapshot) for _raw, item in structural):
            proven.add(container_id)
    return frozenset(proven)


def _dom_href_confidence_is_trusted(element: Mapping[str, object]) -> bool:
    """显式置信度仍执行 0.7 下限；DOM 未提供时由结构事实证明。"""

    confidence = element.get("navigation_confidence", 1.0)
    return (
        not isinstance(confidence, bool)
        and isinstance(confidence, int | float)
        and isfinite(float(confidence))
        and float(confidence) >= WEB_NAVIGATION_CONFIDENCE_FLOOR
    )


def navigation_snapshot_from_html(
    html,
    *,
    snapshot=None,
    base_url="",
    container_registry=None,
    require_runtime_hit_test=False,
):
    """供 Capture 策略与分类事实共用相同的导航范围。"""
    parser = _NavigationParser()
    parser.feed(html)
    parser.close()
    return navigation_snapshot_from_parser(
        parser,
        snapshot=snapshot,
        base_url=base_url,
        container_registry=container_registry,
        require_runtime_hit_test=require_runtime_hit_test,
    )


def bind_navigation_candidate_source(
    candidate: WebNavigationCandidate,
    *,
    source_capture_id: str,
    source_epoch: int,
) -> WebNavigationCandidate:
    """把候选绑定到实际可执行 Capture，并重算审计 ID。"""

    capture_id = source_capture_id.strip()
    if not capture_id:
        raise ValueError("source_capture_id 不能为空")
    identity_source = "\x1f".join(
        (
            _site_origin(candidate.url or candidate.target_hint),
            candidate.entry_id,
            capture_id,
            f"epoch:{source_epoch}",
            candidate.dom_fingerprint,
            candidate.target_hint,
        )
    )
    return WebNavigationCandidate(
        **{
            **candidate.__dict__,
            "entry_id": f"web-nav-entry:{_short_hash(identity_source)}",
            "source_capture_id": capture_id,
            "source_epoch": source_epoch,
        }
    )


# 兼容 Web V2 早期调用方；新代码使用不绑定具体 class 名的通用入口。
extract_navbar_navigation_candidates = extract_navigation_candidates


def _navigation_source_signal(
    tag: str,
    values: dict[str, str | None],
) -> str | None:
    if tag == "nav":
        return "semantic_nav"
    if str(values.get("role") or "").strip().casefold() == "navigation":
        return "role_navigation"
    class_or_id = " ".join(str(values.get(name) or "") for name in ("class", "id")).casefold()
    if "nav" in class_or_id:
        return "class_or_id_contains_nav"
    for name, value in values.items():
        if name.startswith("aria-") or name.startswith("data-"):
            normalized = str(value or "").casefold()
            if "nav" in normalized or (name in {"data-nav", "data-navigation"}):
                return "navigation_attribute"
    return None


def _navigation_source_kind(
    tag: str,
    values: dict[str, str | None],
    visual_card: bool,
) -> str | None:
    role = str(values.get("role") or "").strip().casefold()
    if visual_card:
        return "visual_card"
    if tag == "router-link" or values.get("data-route") is not None:
        return "spa_route"
    if values.get("onclick") is not None:
        return "js_control"
    if tag == "a" and values.get("href") is not None:
        return "anchor"
    if role == "link":
        return "semantic_link"
    if tag == "button" or role == "button":
        return "button"
    return None


def _target_hint(values: dict[str, str | None]) -> str:
    for name in ("href", "to", "data-route", "data-href", "data-url"):
        value = str(values.get(name) or "").strip()
        if value:
            return value[:500]
    onclick = str(values.get("onclick") or "").strip()
    match = re.search(
        r"(?i)(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)"
        r"|(?:navigate|push)\s*\(\s*['\"]([^'\"]+)",
        onclick,
    )
    if match is not None:
        return str(match.group(1) or match.group(2) or "")[:500]
    return ""


def _is_visual_card(tag: str, values: dict[str, str | None]) -> bool:
    if tag not in {"a", "article", "div", "li", "section"}:
        return False
    tokens = " ".join(str(values.get(name) or "") for name in ("class", "id", "role")).casefold()
    has_card_shape = any(marker in tokens for marker in ("card", "tile"))
    has_target = any(
        values.get(name) is not None
        for name in ("href", "onclick", "data-route", "data-href", "data-url")
    )
    return has_card_shape and has_target


def _attributes_hide_element(values: dict[str, str | None]) -> bool:
    if "hidden" in values or str(values.get("aria-hidden") or "").casefold() == "true":
        return True
    style = re.sub(r"\s+", "", str(values.get("style") or "").casefold())
    return "display:none" in style or "visibility:hidden" in style


def _excluded_navigation_region(tag: str, values: dict[str, str | None]) -> bool:
    role = str(values.get("role") or "").strip().casefold()
    if tag in {"article", "aside", "footer"} or role in {"search", "toolbar"}:
        return True
    tokens = _navigation_region_marker(values)
    return any(
        marker in tokens
        for marker in (
            "advert",
            "sponsor",
            "promotion",
            "promo-",
            "pagination",
            "pager",
            "breadcrumb",
            "bread-crumb",
            "footer",
            "search",
            "toolbar",
            "tool-bar",
        )
    )


def _navigation_region_marker(values: dict[str, str | None]) -> str:
    return " ".join(
        str(values.get(name) or "") for name in ("class", "id", "role", "aria-label")
    ).casefold()


def _resolve_navigation_target(
    target_hint: str,
    *,
    base_url: str,
) -> tuple[str | None, str]:
    if not target_hint:
        return None, "low"
    if target_hint.casefold().startswith("javascript:"):
        return None, "dangerous"
    try:
        target = urlsplit(urljoin(base_url, target_hint))
        base = urlsplit(base_url)
        if target.scheme.casefold() not in {"http", "https"} or not target.hostname:
            return None, "dangerous"
        if _origin(target) != _origin(base):
            return None, "cross_origin"
        return (
            urlunsplit(
                (target.scheme, target.netloc, target.path or "/", target.query, target.fragment)
            ),
            "low",
        )
    except ValueError:
        return None, "dangerous"


def _site_origin(value: str) -> str:
    try:
        return canonical_web_origin(value)
    except ValueError:
        return "unresolved-site"


def _origin(parts: object) -> tuple[str, str, int]:
    return web_origin_key(parts.geturl())


def canonical_navigation_identity(url: str, *, identity_policy=None) -> tuple[str, str, str, str]:
    """返回保留协议与 SPA 路由的业务目标身份。"""

    from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY

    parsed = urlsplit((identity_policy or DEFAULT_WEB_URL_IDENTITY_POLICY).canonical(url))
    return parsed.scheme, parsed.netloc, parsed.path, parsed.query + "#" + parsed.fragment


def catalog_navigation_url_matches(
    catalog_url: str, runtime_url: str, *, allow_http_upgrade: bool = False
) -> bool:
    """默认比较身份；仅目录访问匹配可明确允许 HTTP 默认端口升级。"""

    catalog_identity = canonical_navigation_identity(catalog_url)
    runtime_identity = canonical_navigation_identity(runtime_url)
    if catalog_identity[1:] != runtime_identity[1:]:
        return False
    catalog_scheme = catalog_identity[0]
    runtime_scheme = runtime_identity[0]
    return catalog_scheme == runtime_scheme or (
        allow_http_upgrade and catalog_scheme == "http" and runtime_scheme == "https"
    )


def normalized_menu_path(path: tuple[str, ...]) -> tuple[str, ...]:
    """归一化菜单层级文字，仅用于与已发布 Catalog 对账。"""

    return tuple(" ".join(item.split()).casefold() for item in path)


def _normalize_label(value: str) -> str:
    return " ".join(value.split())[:200]


def _fallback_label(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or "home"


def _page_type_token(label: str, url: str | None) -> str:
    normalized = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "-", label.casefold()).strip("-")
    return normalized[:100] or (
        _fallback_label(url).casefold()[:100] if url is not None else "navigation"
    )


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _is_low_risk_navigation(label: str, url: str) -> bool:
    value = f"{label} {urlsplit(url).path}".casefold()
    denied_markers = (
        "删除",
        "保存",
        "提交",
        "发布",
        "支付",
        "购买",
        "充值",
        "转账",
        "退出",
        "logout",
        "signout",
        "sign-out",
        "download",
        "upload",
    )
    return not any(marker in value for marker in denied_markers)


__all__ = [
    "WEB_NAVIGATION_SOURCE_SELECTOR",
    "WEB_NAVBAR_SOURCE_SELECTOR",
    "WebNavigationCandidate",
    "bind_navigation_candidate_source",
    "canonical_navigation_identity",
    "catalog_navigation_url_matches",
    "extract_navigation_candidates",
    "extract_navbar_navigation_candidates",
    "normalized_menu_path",
]
