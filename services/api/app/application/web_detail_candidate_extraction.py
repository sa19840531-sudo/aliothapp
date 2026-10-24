"""从 Web 无遮挡 Snapshot 提取同源主详情链接。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

from services.api.app.application.web_recommendation_structure import is_web_recommendation_node
from services.api.app.domain.exploration import WEB_LIST_PAGE_TYPES
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY

_BLOCKED_STRUCTURE_MARKERS = (
    "pagination",
    "pager",
    "breadcrumb",
    "advert",
    "banner",
    "sponsor",
    "login",
    "register",
    "download",
    "footer",
    "legal",
    "privacy",
    "terms",
    "address-list",
    "modal",
    "dialog",
    "popup",
    "overlay",
)
_BLOCKED_LEAF_PATTERN = re.compile(
    r"(?:^|[-_/\s.])(tag|tags|meta|category|categories|taxonomy|filter|sort|address)"
    r"(?:$|[-_/\s.])",
    re.IGNORECASE,
)
_BLOCKED_TEXTS = frozenset(
    {
        "更多",
        "more",
        "上一页",
        "下一页",
        "next",
        "previous",
        "登录",
        "注册",
        "隐私政策",
        "用户协议",
    }
)
_HOME_ALIAS_PATHS = frozenset({"/", "/home", "/index", "/index.html"})


@dataclass(frozen=True, kw_only=True)
class WebDetailCandidate:
    """绑定当前列表 Capture 的详情候选；URL 和 selector 仅在可信侧保存。"""

    candidate_id: str
    source_capture_id: str
    canonical_url: str
    expected_page_type: Literal["video_detail", "content_detail"]
    evidence: tuple[str, ...]
    text: str
    selector: str
    rect: dict[str, float]
    is_media_card: bool
    is_video_specific_card: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "expected_page_type": self.expected_page_type,
            "evidence": list(self.evidence),
            "text": self.text,
            "rect": dict(self.rect),
        }


def detail_candidates_from_ready_snapshot(
    snapshot: Mapping[str, object],
    *,
    list_page_type: str,
    origin: str,
    current_url: str | None = None,
    identity_policy=DEFAULT_WEB_URL_IDENTITY_POLICY,
    review_diagnostics: list[dict[str, object]] | None = None,
) -> tuple[WebDetailCandidate, ...]:
    """从同源重复卡片 HTML 锚点提取主详情，不要求视口中心命中。"""

    if list_page_type not in WEB_LIST_PAGE_TYPES:
        raise ValueError("list_page_type 必须为 Web 列表类型")
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot 必须为对象")
    capture_id = str(snapshot.get("capture_id") or "").strip()
    if not capture_id:
        raise ValueError("ready snapshot 缺少 capture_id")
    origin_parts = _absolute_http_url(origin, field_name="origin")
    current_parts = _absolute_http_url(
        current_url or str(snapshot.get("url") or "") or origin,
        field_name="current_url",
    )
    if not _same_web_origin(current_parts, origin_parts):
        raise ValueError("current_url 必须与 origin 同源")
    expected_page_type: Literal["video_detail", "content_detail"] = (
        "video_detail" if list_page_type == "video_list" else "content_detail"
    )
    raw_elements = snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return ()
    selected: dict[str, tuple[int, WebDetailCandidate]] = {}
    for raw in raw_elements:
        if not isinstance(raw, Mapping):
            continue
        href = str(raw.get("href") or "").strip()
        selector = str(raw.get("selector") or "").strip()
        if not href or not selector:
            continue
        try:
            resolved = _absolute_http_url(
                urljoin(current_parts.geturl(), href),
                field_name="href",
            )
        except ValueError:
            continue
        if not _same_web_origin(resolved, origin_parts):
            continue
        try:
            canonical_url = identity_policy.canonical(resolved.geturl())
        except ValueError as exc:
            if str(exc) == "WEB_POLICY_VERSION_UNSUPPORTED":
                raise
            if review_diagnostics is not None:
                review_diagnostics.append(
                    {
                        "href": href,
                        "source_capture_id": capture_id,
                        "reason_code": (
                            "WEB_URL_HASH_NEEDS_REVIEW"
                            if str(exc) == "WEB_URL_HASH_NEEDS_REVIEW"
                            else "WEB_URL_IDENTITY_INVALID"
                        ),
                    }
                )
            continue
        if not is_web_primary_detail_anchor(raw):
            continue
        if _same_web_document(
            resolved, current_parts, identity_policy=identity_policy
        ) or _is_home_alias(resolved):
            continue
        if _unsafe_detail_link(raw, resolved):
            continue
        rect = _valid_rect(raw.get("rect"))
        if rect is None:
            continue
        text = " ".join(str(raw.get("text") or "").split())[:300]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "canonical_url": canonical_url,
                    "expected_page_type": expected_page_type,
                    "structure_signature": snapshot.get("structure_signature"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        candidate = WebDetailCandidate(
            candidate_id=f"web-candidate:{fingerprint}",
            source_capture_id=capture_id,
            canonical_url=canonical_url,
            expected_page_type=expected_page_type,
            evidence=(
                "当前无遮挡 Capture",
                "同源重复卡片 HTML 主链接",
                "无需视口中心命中",
            ),
            text=text,
            selector=selector,
            rect=rect,
            is_media_card=web_primary_detail_anchor_is_media(raw),
            is_video_specific_card=web_primary_detail_anchor_has_video_structure(raw),
        )
        score = _primary_anchor_score(raw, text)
        existing = selected.get(canonical_url)
        if existing is None or score > existing[0]:
            selected[canonical_url] = (score, candidate)
    return tuple(item[1] for item in selected.values())


def validate_web_detail_origin(origin: str) -> None:
    """验证详情提取所用站点 origin。"""

    _absolute_http_url(origin, field_name="origin")


def is_web_primary_detail_anchor(raw: Mapping[str, object]) -> bool:
    """判断 Snapshot 元素是否为重复正文卡片的主详情锚点。"""

    repeated_size = raw.get("repeat_group_size")
    repeated = raw.get("within_repeated_structure") is True or (
        isinstance(repeated_size, int)
        and not isinstance(repeated_size, bool)
        and repeated_size >= 2
    )
    is_anchor = str(raw.get("tag") or "").casefold() == "a"
    is_link = str(raw.get("role") or "").casefold() == "link"
    within_web_navigation = raw.get("within_web_navigation")
    within_navigation = (
        within_web_navigation is True
        if isinstance(within_web_navigation, bool)
        else raw.get("within_navigation") is True
    )
    trusted = (
        (is_anchor or is_link)
        and raw.get("interactive") is True
        and raw.get("visible") is True
        and repeated
        and not within_navigation
        and not str(raw.get("web_obstruction_layer") or "").strip()
        and not any(
            raw.get(flag) is True
            for flag in (
                "within_pagination",
                "within_breadcrumb",
                "within_footer",
                "is_advertisement",
            )
        )
    )
    if not trusted:
        return False
    href = str(raw.get("href") or "").strip()
    if not href:
        return False
    parts = urlsplit(href)
    path = _business_path(parts)
    return not _raw_detail_link_noise(raw, path=path)


def web_primary_detail_anchor_is_media(raw: Mapping[str, object]) -> bool:
    """返回主详情卡片是否具有通用媒体/剧集封面结构。"""

    tokens = {
        item.casefold()
        for key in ("class_tokens", "ancestor_class_tokens")
        for item in (raw.get(key) if isinstance(raw.get(key), list) else [])
        if isinstance(item, str)
    }
    # 选择器只抽取完整 id/class，不读取属性值，禁止 non-video-card 等包含词。
    selector = re.sub(r"\[[^\]]*\]", "", str(raw.get("selector") or ""))
    tokens.update(token.casefold() for token in re.findall(r"[.#]([a-zA-Z_][\w-]*)", selector))
    return any(
        re.fullmatch(
            r"(?:video|movie|media|drama|poster|thumbnail|thumb|cover)"
            r"(?:[-_](?:card|list|grid|item|img|image|box|link|title|wrapper|wrap))*",
            token,
        ) or (
            not set(re.split(r"[-_]", token)) & {"non", "not", "no"}
            and re.fullmatch(
                r"(?:[a-z][a-z0-9]*-)*(?:video|movie|media|drama)-card"
                r"__(?:body|title|link|cover|poster|thumb|thumbnail|image)",
                token,
            )
        )
        for token in tokens
    )


def web_primary_detail_anchor_has_video_structure(raw: Mapping[str, object]) -> bool:
    """只接受可信详情卡自身或所属卡的明确视频类名，不把通用封面当视频。"""
    tokens = {
        item.casefold()
        for key in ("class_tokens", "ancestor_class_tokens")
        for item in (raw.get(key) if isinstance(raw.get(key), list) else [])
        if isinstance(item, str)
    }
    selector = re.sub(r"\[[^\]]*\]", "", str(raw.get("selector") or ""))
    tokens.update(token.casefold() for token in re.findall(r"[.#]([a-zA-Z_][\w-]*)", selector))
    return any(
        token == "longvideocard"
        or re.fullmatch(
            r"(?:video|movie|film|clip)(?:[-_](?:card|item|tile|box|cover|poster|"
            r"thumb|thumbnail|img|image|link|wrapper|wrap))+",
            token,
        )
        for token in tokens
    )


def web_card_anchor_is_auxiliary(raw: Mapping[str, object]) -> bool:
    """卡片标签、筛选、广告等辅助链接不能增加卡片主目标数量。"""
    # 评论区中的作者、分类、上下篇导航不是主列表卡片；按完整类名词段匹配。
    for field in ("class_tokens", "ancestor_class_tokens"):
        # 嵌套在评论容器中的推荐组件仍计入次要推荐，不能连同辅助链接一起抹掉。
        if is_web_recommendation_node(raw):
            break
        values = raw.get(field)
        if isinstance(values, (list, tuple)) and any(
            isinstance(value, str)
            and re.search(r"(?:^|[-_])(?:comments|comment-section|comment-list)(?:$|[-_])",
                          value.casefold())
            for value in values
        ):
            return True
    try:
        path = _business_path(urlsplit(str(raw.get("href") or "")))
    except ValueError:
        return True
    return _raw_detail_link_noise(raw, path=path)


def _unsafe_detail_link(raw: Mapping[str, object], resolved: object) -> bool:
    text = " ".join(str(raw.get("text") or "").casefold().split())
    path = _business_path(resolved)
    if _raw_detail_link_noise(raw, path=path) or text in _BLOCKED_TEXTS:
        return True
    return any(marker in text for marker in ("支付", "购买", "备用网址"))


def _raw_detail_link_noise(raw: Mapping[str, object], *, path: str) -> bool:
    text = " ".join(str(raw.get("text") or "").casefold().split())
    facts = " ".join(
        str(value)
        for value in (
            raw.get("selector"),
            raw.get("class_tokens"),
            raw.get("ancestor_class_tokens"),
            raw.get("rel"),
            raw.get("web_obstruction_layer_kind"),
            path,
        )
    ).casefold()
    selector = str(raw.get("selector") or "")
    leaf_selector = re.split(r"[>+~]", selector)[-1]
    leaf_facts = " ".join(
        str(value)
        for value in (
            leaf_selector,
            raw.get("class_tokens"),
            raw.get("rel"),
            path,
        )
    ).casefold()
    return (
        path in {"", "/"}
        or text in _BLOCKED_TEXTS
        or any(marker in facts for marker in _BLOCKED_STRUCTURE_MARKERS)
        or _BLOCKED_LEAF_PATTERN.search(leaf_facts) is not None
        or any(marker in text for marker in ("支付", "购买", "备用网址"))
    )


def _primary_anchor_score(raw: Mapping[str, object], text: str) -> int:
    facts = " ".join(
        str(value)
        for value in (
            raw.get("selector"),
            raw.get("class_tokens"),
            raw.get("ancestor_class_tokens"),
        )
    ).casefold()
    score = 30 if text else 0
    if any(marker in facts for marker in ("title", "headline", "name")):
        score += 20
    if any(marker in facts for marker in ("cover", "poster", "thumb", "image")):
        score += 10
    if raw.get("web_detail_priority_candidate") is True:
        score += 5
    return score


def _valid_rect(value: object) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    rect: dict[str, float] = {}
    for name in ("x", "y", "width", "height"):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return None
        rect[name] = float(raw)
    if rect["width"] <= 0 or rect["height"] <= 0:
        return None
    return rect


def _absolute_http_url(value: str, *, field_name: str):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} 不是合法 URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{field_name} 必须为无凭据 HTTP(S) URL")
    return parsed


def _origin_identity(parts: object) -> tuple[str, str, int]:
    scheme = str(getattr(parts, "scheme", "")).casefold()
    port = getattr(parts, "port", None)
    return (
        scheme,
        str(getattr(parts, "hostname", "")).casefold(),
        int(port or (443 if scheme == "https" else 80)),
    )


def _same_web_origin(candidate: object, origin: object) -> bool:
    if _origin_identity(candidate) == _origin_identity(origin):
        return True
    candidate_scheme, candidate_host, candidate_port = _origin_identity(candidate)
    origin_scheme, origin_host, origin_port = _origin_identity(origin)
    return (
        origin_scheme == "http"
        and origin_port == 80
        and candidate_scheme == "https"
        and candidate_port == 443
        and candidate_host == origin_host
    )


def _same_web_document(
    candidate: object, current: object, *, identity_policy=DEFAULT_WEB_URL_IDENTITY_POLICY
) -> bool:
    return identity_policy.canonical(candidate.geturl()) == identity_policy.canonical(
        current.geturl()
    )


def _is_home_alias(candidate: object) -> bool:
    path = _business_path(candidate).rstrip("/") or "/"
    return (
        path in _HOME_ALIAS_PATHS
        and not getattr(candidate, "query", "")
        and not getattr(candidate, "fragment", "").startswith(("/", "!/"))
    )


def _business_path(parts: object) -> str:
    fragment = str(getattr(parts, "fragment", ""))
    if fragment.startswith(("/", "!/")):
        return str(urlsplit(fragment.removeprefix("!")).path or "/").casefold()
    return str(getattr(parts, "path", "") or "/").casefold()


__all__ = [
    "WebDetailCandidate",
    "detail_candidates_from_ready_snapshot",
    "is_web_primary_detail_anchor",
    "validate_web_detail_origin",
    "web_primary_detail_anchor_is_media",
    "web_card_anchor_is_auxiliary",
]
