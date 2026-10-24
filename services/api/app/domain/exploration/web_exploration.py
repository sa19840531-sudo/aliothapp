"""Web V2 阻断候选的密封身份与只读公开投影。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

WEB_LIST_PAGE_TYPES = frozenset({"video_list", "content_list"})
WEB_DETAIL_PAGE_TYPES = frozenset({"video_detail", "content_detail"})
WEB_VIDEO_PAGE_TYPES = frozenset({"video_list", "video_detail"})
_MAX_OBSTRUCTION_RESOURCE_SUMMARIES = 32


class WebMenuTerminalStatus(StrEnum):
    """一个 Catalog 或运行时菜单身份的不可逆审计终态。"""

    HOME_BASELINE = "home_baseline"
    DETAIL_CAPTURED = "detail_captured"
    LOGICAL_TARGET_REUSED = "logical_target_reused"
    MENU_PAGE_CAPTURED = "menu_page_captured"
    NO_DETAIL_CANDIDATE = "no_detail_candidate"
    POPUP_BLOCKED = "popup_blocked"
    HUMAN_VERIFICATION_BLOCKED = "human_verification_blocked"
    UNSAFE_NAVIGATION = "unsafe_navigation"
    PAGE_TYPE_UNCLASSIFIED = "page_type_unclassified"
    DETAIL_SELECTION_UNTRUSTED = "detail_selection_untrusted"
    DETAIL_PAGE_TYPE_MISMATCH = "detail_page_type_mismatch"
    NAVIGATION_FAILED = "navigation_failed"
    CROSS_ORIGIN_BLOCKED = "cross_origin_blocked"
    MENU_LIMIT_SKIPPED = "menu_limit_skipped"
    ALLOWLIST_DRIFTED = "allowlist_drifted"
    RUNTIME_DISCOVERED_UNREVIEWED = "runtime_discovered_unreviewed"

    @property
    def is_terminal(self) -> bool:
        return True


class WebExplorationValidationError(ValueError):
    """Web 探索候选快照不满足安全合同。"""


@dataclass(frozen=True, kw_only=True)
class WebInteractionCandidate:
    """绑定源 Capture 的可信交互候选；selector 不得进入 LLM 输入。"""

    candidate_id: str
    capture_id: str
    capability: str
    text: str
    tag_name: str
    role: str | None
    rect: dict[str, float]
    selector: str
    effective_z_index: int
    visible: bool
    hit_test_passed: bool

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_id",
            "capture_id",
            "capability",
            "tag_name",
            "selector",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise WebExplorationValidationError(f"{field_name} 不能为空")
        if not self.candidate_id.startswith("web-candidate:"):
            raise WebExplorationValidationError("candidate_id 前缀无效")
        object.__setattr__(self, "rect", _validated_rect(self.rect))
        if isinstance(self.effective_z_index, bool) or not isinstance(
            self.effective_z_index, int
        ):
            raise WebExplorationValidationError("effective_z_index 必须为整数")
        if not isinstance(self.visible, bool) or not isinstance(
            self.hit_test_passed, bool
        ):
            raise WebExplorationValidationError("候选可见性和 hit-test 必须为布尔值")

    def to_public_dict(self) -> dict[str, object]:
        """返回可交给判定模型的只读字段，明确排除 selector。"""

        return {
            "candidate_id": self.candidate_id,
            "capability": self.capability,
            "text": self.text,
            "tag_name": self.tag_name,
            "role": self.role,
            "rect": dict(self.rect),
            "effective_z_index": self.effective_z_index,
            "visible": self.visible,
            "hit_test_passed": self.hit_test_passed,
        }

    def to_trusted_dict(self) -> dict[str, object]:
        """返回仅供受信 resolver 使用的完整快照。"""

        return {
            **self.to_public_dict(),
            "capture_id": self.capture_id,
            "selector": self.selector,
        }


def build_web_obstruction_candidates(
    *,
    capture_id: str,
    ui_snapshot: Mapping[str, object],
) -> tuple[WebInteractionCandidate, ...]:
    """从可信 UI Snapshot 生成结构可信、可安全关闭的阻断候选。"""

    if not isinstance(capture_id, str) or not capture_id.strip():
        raise WebExplorationValidationError("capture_id 不能为空")
    if not isinstance(ui_snapshot, Mapping):
        raise WebExplorationValidationError("ui_snapshot 必须为对象")
    raw_elements = ui_snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return ()
    selected: dict[
        tuple[object, ...], tuple[int, WebInteractionCandidate]
    ] = {}
    for raw_element in raw_elements:
        if not isinstance(raw_element, Mapping):
            continue
        effective_z_index = _integer(raw_element.get("effective_z_index"))
        hit_test = raw_element.get("hit_test")
        close_basis = str(
            raw_element.get("web_obstruction_close_basis") or ""
        ).strip().casefold()
        own_class_tokens = {
            str(value).strip().casefold()
            for value in raw_element.get("class_tokens") or []
            if str(value).strip()
        }
        delegated_close_control = bool(
            own_class_tokens & {"on-popup-close", "on-dialog-close"}
        )
        sealed_own_close_control = (
            close_basis == "own_close_token"
            and raw_element.get("web_obstruction_close_control_unique") is True
            and raw_element.get("web_obstruction_close_hit_safe") is True
        )
        structural_close = (
            raw_element.get("web_obstruction_layer_structural") is True
            and raw_element.get("web_obstruction_close_semantics") is True
            and raw_element.get("within_web_navigation") is not True
        )
        actionable = (
            raw_element.get("interactive") is True
            or str(raw_element.get("role") or "").casefold() == "button"
            or str(raw_element.get("tag") or "").casefold() in {"a", "button"}
            or delegated_close_control
            or sealed_own_close_control
        )
        if (
            effective_z_index is None
            or raw_element.get("visible") is not True
            or raw_element.get("within_web_navigation") is True
            or not isinstance(hit_test, Mapping)
            or hit_test.get("center_hits_target") is not True
            or not structural_close
            or not actionable
        ):
            continue
        selector = str(raw_element.get("selector") or "").strip()
        if not selector:
            continue
        try:
            rect = _validated_rect(raw_element.get("rect"))
        except WebExplorationValidationError:
            continue
        if rect["width"] <= 0 or rect["height"] <= 0:
            continue
        if _human_verification_layer_present(raw_element, raw_elements):
            continue
        text = _candidate_semantic_text(raw_element)
        tag_name = str(raw_element.get("tag") or "").strip().casefold()
        role_value = str(raw_element.get("role") or "").strip().casefold()
        role = role_value or None
        if not text and (
            close_basis in {"delegated_wrapper_self", "own_close_token"}
            and raw_element.get("web_obstruction_close_control_unique") is True
            and raw_element.get("web_obstruction_close_hit_safe") is True
            and tag_name != "a"
            and role_value != "link"
            and not str(raw_element.get("href") or "").strip()
            and str(raw_element.get("web_obstruction_layer_kind") or "").casefold()
            != "age_gate"
        ):
            text = "关闭"
        if not text:
            continue
        dom_fingerprint = _dom_fingerprint(raw_element)
        candidate_hash = hashlib.sha256(
            json.dumps(
                {
                    "dom_fingerprint": dom_fingerprint,
                    "rect": rect,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        capability = _candidate_capability(
            raw_element,
            text,
            registration_layer_present=_registration_layer_present(
                raw_element,
                raw_elements,
            ),
        )
        if capability == "web_obstruction_interaction":
            continue
        candidate = WebInteractionCandidate(
            candidate_id=f"web-candidate:{candidate_hash}",
            capture_id=capture_id,
            capability=capability,
            text=text,
            tag_name=tag_name,
            role=role,
            rect=rect,
            selector=selector,
            effective_z_index=effective_z_index,
            visible=True,
            hit_test_passed=True,
        )
        identity = _obstruction_candidate_identity(raw_element, rect, selector)
        rank = _obstruction_close_rank(raw_element)
        existing = selected.get(identity)
        if existing is None or rank > existing[0]:
            selected[identity] = (rank, candidate)
    return tuple(item[1] for item in selected.values())


def web_obstruction_state_fingerprint(
    ui_snapshot: Mapping[str, object],
) -> str | None:
    """哈希当前结构化阻断层的可见内容，用于区分同控件轮换弹层。"""

    raw_elements = ui_snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return None
    facts: list[dict[str, object]] = []
    for raw_element in raw_elements:
        if (
            not isinstance(raw_element, Mapping)
            or raw_element.get("visible") is not True
            or raw_element.get("web_obstruction_layer_structural") is not True
        ):
            continue
        layer = str(raw_element.get("web_obstruction_layer") or "").strip()
        if not layer:
            continue
        facts.append(
            {
                "layer": layer,
                "selector": str(raw_element.get("selector") or "")[:500],
                "tag": str(raw_element.get("tag") or "")[:40],
                "text": str(raw_element.get("text") or "")[:300],
                "aria_label": str(raw_element.get("aria_label") or "")[:300],
                "alt": str(raw_element.get("alt") or "")[:300],
                "title": str(raw_element.get("title") or "")[:300],
                "src": str(raw_element.get("src") or "")[:1000],
                "href": str(raw_element.get("href") or "")[:1000],
                "class_tokens": list(raw_element.get("class_tokens") or [])[:16],
                "rect": raw_element.get("rect"),
            }
        )
    if not facts:
        return None
    facts.sort(
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    resource_facts = _obstruction_resource_facts(
        ui_snapshot,
        layers={str(item["layer"]) for item in facts},
    )
    return hashlib.sha256(
        json.dumps(
            {
                "layers": facts,
                "resource_summaries": resource_facts,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _obstruction_resource_facts(
    ui_snapshot: Mapping[str, object],
    *,
    layers: set[str],
) -> list[dict[str, str]]:
    """只保留阻断层内已脱敏的资源哈希，并稳定去重、排序和限量。"""

    raw_summaries = ui_snapshot.get("resource_summaries")
    if not isinstance(raw_summaries, list) or not layers:
        return []
    facts: set[tuple[str, str, str]] = set()
    for raw_summary in raw_summaries:
        if not isinstance(raw_summary, Mapping):
            continue
        selector = str(raw_summary.get("selector") or "").strip()[:500]
        attribute = str(raw_summary.get("attribute") or "").strip().casefold()
        digest = str(raw_summary.get("sha256") or "").strip().casefold()
        if (
            attribute != "src"
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not any(_selector_belongs_to_layer(selector, layer) for layer in layers)
        ):
            continue
        facts.add((selector, attribute, digest))
    return [
        {"selector": selector, "attribute": attribute, "sha256": digest}
        for selector, attribute, digest in sorted(facts)[
            :_MAX_OBSTRUCTION_RESOURCE_SUMMARIES
        ]
    ]


def _selector_belongs_to_layer(selector: str, layer: str) -> bool:
    if not selector or not layer:
        return False
    return selector == layer or selector.startswith(f"{layer} ")


def _candidate_semantic_text(element: Mapping[str, object]) -> str:
    """按可访问语义优先级提取候选文字，供模型与运行时执行同一套复验。"""

    for field_name in ("text", "aria_label", "alt"):
        value = element.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return ""


def _candidate_capability(
    element: Mapping[str, object],
    text: str,
    *,
    registration_layer_present: bool = False,
) -> str:
    normalized = "".join(text.casefold().split())
    structural = " ".join(
        str(value).casefold()
        for value in (
            element.get("selector"),
            element.get("class_tokens"),
            element.get("ancestor_class_tokens"),
        )
    )
    adult_markers = ("18", "十八", "成年")
    direct_entry_markers = ("进入", "继续", "enter", "continue", "yes")
    confirmation_markers = ("确认", "同意", "confirm", "agree")
    age_gate_structure = any(
        marker in structural for marker in ("age-gate", "age_gate", "adult-gate", "18-gate")
    )
    registration_context = registration_layer_present or any(
        marker in f"{normalized} {structural}"
        for marker in (
            "登录",
            "注册",
            "用户名",
            "密码",
            "login",
            "register",
            "sign-in",
            "signin",
            "sign-up",
            "signup",
        )
    )
    forbidden_action_context = any(
        marker in f"{normalized} {structural}"
        for marker in (
            "验证码",
            "滑块",
            "captcha",
            "支付",
            "付款",
            "购买",
            "充值",
            "pay",
            "purchase",
            "下载",
            "download",
        )
    )
    adult_semantics = any(marker in normalized for marker in adult_markers)
    entry_semantics = any(marker in normalized for marker in direct_entry_markers) or (
        age_gate_structure
        and any(marker in normalized for marker in confirmation_markers)
    )
    if adult_semantics and (registration_context or forbidden_action_context):
        return "web_obstruction_interaction"
    if adult_semantics and entry_semantics:
        return "web_age_gate_confirm"
    if age_gate_structure and adult_semantics:
        return "web_obstruction_interaction"
    if element.get("web_obstruction_close_semantics") is True:
        return "web_obstruction_dismiss"
    if any(
        marker in normalized
        for marker in ("关闭", "取消", "稍后", "我知道了", "跳过", "close", "×")
    ):
        return "web_obstruction_dismiss"
    return "web_obstruction_interaction"


def _obstruction_candidate_identity(
    element: Mapping[str, object],
    rect: Mapping[str, float],
    selector: str,
) -> tuple[object, ...]:
    """同一阻断层同一点击区域只保留语义最强的关闭控件。"""

    layer = str(element.get("web_obstruction_layer") or "").strip()
    if not layer:
        return ("selector", selector)
    hit_test = element.get("hit_test")
    center_target = (
        str(hit_test.get("center_target") or "").strip()
        if isinstance(hit_test, Mapping)
        else ""
    )
    if (
        center_target
        and element.get("web_obstruction_close_control_unique") is True
        and element.get("web_obstruction_close_hit_safe") is True
    ):
        return ("layer-hit-target", layer, center_target)
    return (
        "layer-rect",
        layer,
        *(round(rect[name], 2) for name in ("x", "y", "width", "height")),
    )


def _obstruction_close_rank(element: Mapping[str, object]) -> int:
    """优先委托事件包装器，避免重复点击其子图片。"""

    own_tokens = {
        str(value).strip().casefold()
        for value in element.get("class_tokens") or []
        if str(value).strip()
    }
    if own_tokens & {"on-popup-close", "on-dialog-close"}:
        return 30
    if any("close" in token or "dismiss" in token for token in own_tokens):
        return 20
    return 10


def _registration_layer_present(
    candidate: Mapping[str, object],
    elements: list[object],
) -> bool:
    """只识别与当前候选同一阻断层中的登录/注册结构。"""

    markers = (
        "登录",
        "注册",
        "用户名",
        "密码",
        "login",
        "register",
        "sign-in",
        "signin",
        "sign-up",
        "signup",
    )
    for raw in elements:
        if not isinstance(raw, Mapping) or raw is candidate:
            continue
        effective_z_index = _integer(raw.get("effective_z_index"))
        if (
            effective_z_index is None
            or raw.get("visible") is not True
            or not _same_obstruction_layer(candidate, raw)
        ):
            continue
        if str(raw.get("type") or "").casefold() == "password":
            return True
        facts = " ".join(
            str(raw.get(name) or "").casefold()
            for name in (
                "text",
                "placeholder",
                "aria_label",
                "name",
                "selector",
                "class_tokens",
                "ancestor_class_tokens",
            )
        )
        if any(marker in facts for marker in markers):
            return True
    return False


def _human_verification_layer_present(
    candidate: Mapping[str, object],
    elements: list[object],
) -> bool:
    """同一阻断层出现人机验证事实时，不签发任何自动点击候选。"""

    markers = (
        "验证码",
        "人机验证",
        "滑块验证",
        "captcha",
        "hcaptcha",
        "recaptcha",
        "turnstile",
        "verification code",
        "human verification",
        "not a robot",
        "notarobot",
    )
    if candidate.get("web_human_verification_present") is True:
        return True
    for raw in elements:
        if (
            not isinstance(raw, Mapping)
            or raw.get("visible") is not True
            or _integer(raw.get("effective_z_index")) is None
            or not _same_obstruction_layer(candidate, raw)
        ):
            continue
        facts = " ".join(
            str(raw.get(name) or "").casefold()
            for name in (
                "text",
                "placeholder",
                "aria_label",
                "alt",
                "title",
                "src",
                "data_sitekey",
                "name",
                "selector",
                "class_tokens",
                "ancestor_class_tokens",
            )
        )
        if any(marker in facts for marker in markers):
            return True
    return False


def _same_obstruction_layer(
    candidate: Mapping[str, object],
    sibling: Mapping[str, object],
) -> bool:
    """用 Web Snapshot 的最近阻断容器身份绑定同层元素，并兼容旧证据。"""

    candidate_layer = str(candidate.get("web_obstruction_layer") or "").strip()
    sibling_layer = str(sibling.get("web_obstruction_layer") or "").strip()
    if "web_obstruction_layer" in candidate or "web_obstruction_layer" in sibling:
        return bool(candidate_layer and candidate_layer == sibling_layer)

    candidate_selector_root = _selector_root(candidate.get("selector"))
    sibling_selector_root = _selector_root(sibling.get("selector"))
    if candidate_selector_root and candidate_selector_root == sibling_selector_root:
        return True

    candidate_tokens = _obstruction_tokens(candidate)
    sibling_tokens = _obstruction_tokens(sibling)
    return bool(candidate_tokens & sibling_tokens)


def _selector_root(value: object) -> str | None:
    selector = str(value or "").strip().casefold()
    if not selector:
        return None
    root = selector.split(maxsplit=1)[0]
    return root if root.startswith(("#", ".")) else None


def _obstruction_tokens(element: Mapping[str, object]) -> set[str]:
    markers = ("modal", "dialog", "popup", "overlay", "gate", "panel", "form")
    tokens: set[str] = set()
    for name in ("class_tokens", "ancestor_class_tokens"):
        values = element.get(name)
        if not isinstance(values, list):
            continue
        tokens.update(
            token
            for value in values
            if (token := str(value).strip().casefold())
            and any(marker in token for marker in markers)
        )
    return tokens


def _dom_fingerprint(element: Mapping[str, Any]) -> str:
    facts = {
        "selector": str(element.get("selector") or ""),
        "tag": str(element.get("tag") or ""),
        "role": str(element.get("role") or ""),
        "text": str(element.get("text") or "")[:160],
        "class_tokens": list(element.get("class_tokens") or [])[:12],
        "ancestor_class_tokens": list(element.get("ancestor_class_tokens") or [])[:16],
        "effective_z_index": _integer(element.get("effective_z_index")),
    }
    return hashlib.sha256(
        json.dumps(
            facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _validated_rect(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise WebExplorationValidationError("rect 必须为对象")
    rect: dict[str, float] = {}
    for name in ("x", "y", "width", "height"):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise WebExplorationValidationError(f"rect.{name} 必须为数值")
        rect[name] = float(raw)
    return rect


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    integer = int(value)
    return integer if integer == value else None


__all__ = [
    "WEB_DETAIL_PAGE_TYPES",
    "WEB_LIST_PAGE_TYPES",
    "WEB_VIDEO_PAGE_TYPES",
    "WebMenuTerminalStatus",
    "WebExplorationValidationError",
    "WebInteractionCandidate",
    "build_web_obstruction_candidates",
    "web_obstruction_state_fingerprint",
]
