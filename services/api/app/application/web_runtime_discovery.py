"""目录外导航候选的稳定审计身份与低风险授权判定。"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.domain.page.web_origin import (
    canonical_web_host,
    canonical_web_origin,
    web_origin_key,
    web_origin_matches,
)
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


class UntrustedWebCaptureError(ValueError):
    """Capture 不满足动态发现可信边界。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class CanonicalSiteScope:
    """根导航完成后冻结的站点 origin。"""

    requested_origin: str
    canonical_site_origin: str
    redirect_chain: tuple[str, ...]

    @classmethod
    def bind(
        cls,
        requested_url: str,
        redirect_chain: tuple[str, ...] | list[str],
    ) -> "CanonicalSiteScope":
        try:
            requested_origin = canonical_web_origin(requested_url)
            chain = tuple(redirect_chain)
            origins = tuple(canonical_web_origin(url) for url in (requested_url, *chain))
            keys = tuple(web_origin_key(origin) for origin in origins)
        except (TypeError, ValueError) as exc:
            raise UntrustedWebCaptureError("WEB_CANONICAL_SITE_SCOPE_INVALID") from exc
        if not chain:
            chain = (requested_url,)
        for previous, current in zip(keys, keys[1:], strict=False):
            if not _root_redirect_is_canonical(previous, current):
                raise UntrustedWebCaptureError("WEB_CANONICAL_SITE_SCOPE_INVALID")
        return cls(
            requested_origin=requested_origin,
            canonical_site_origin=origins[-1],
            redirect_chain=chain,
        )


def _root_redirect_is_canonical(
    source: tuple[str, str, int],
    target: tuple[str, str, int],
) -> bool:
    source_scheme, source_host, source_port = source
    target_scheme, target_host, target_port = target
    if _apex_www_host(source_host) != _apex_www_host(target_host):
        return False
    if source_host != target_host and {source_host, target_host} != {
        _apex_www_host(source_host),
        f"www.{_apex_www_host(source_host)}",
    }:
        return False
    if source_scheme == target_scheme:
        return source_port == target_port
    return (
        source_scheme == "http"
        and source_port == 80
        and target_scheme == "https"
        and target_port == 443
    )


def _apex_www_host(host: str) -> str:
    normalized = canonical_web_host(host)
    return normalized[4:] if normalized.startswith("www.") else normalized


@dataclass(frozen=True, slots=True, kw_only=True)
class WebCapture:
    """href 发现所需的最小可信 Capture 快照。"""

    capture_id: str
    session_id: str
    finalization_epoch: int
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...] = ()
    current: bool
    ready: bool
    capture_state_fingerprint: str
    depth: int
    anchors: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedWebCaptureAuthority:
    """按冻结 Session、epoch 与 canonical origin 验签发现来源。"""

    site_scope: CanonicalSiteScope
    session_id: str
    finalization_epoch: int
    root_capture_id: str | None = None

    def assert_discoverable(self, capture: WebCapture) -> None:
        if capture.session_id != self.session_id:
            raise UntrustedWebCaptureError("WEB_DISCOVERY_SOURCE_SESSION_UNTRUSTED")
        if capture.finalization_epoch != self.finalization_epoch:
            raise UntrustedWebCaptureError("WEB_DISCOVERY_SOURCE_EPOCH_UNTRUSTED")
        if capture.current is not True:
            raise UntrustedWebCaptureError("WEB_DISCOVERY_SOURCE_NOT_CURRENT")
        if capture.ready is not True:
            raise UntrustedWebCaptureError("WEB_DISCOVERY_SOURCE_NOT_READY")
        try:
            final_origin = canonical_web_origin(capture.final_url)
        except ValueError as exc:
            raise UntrustedWebCaptureError(
                "WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"
            ) from exc
        if final_origin != self.site_scope.canonical_site_origin:
            raise UntrustedWebCaptureError("WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED")
        if self._is_root_binding_capture(capture):
            return
        try:
            observed_origins = tuple(
                canonical_web_origin(url)
                for url in (capture.requested_url, *capture.redirect_chain)
            )
        except ValueError as exc:
            raise UntrustedWebCaptureError(
                "WEB_DISCOVERY_REQUEST_ORIGIN_UNTRUSTED"
            ) from exc
        if any(
            origin != self.site_scope.canonical_site_origin
            for origin in observed_origins
        ):
            raise UntrustedWebCaptureError("WEB_DISCOVERY_REQUEST_ORIGIN_UNTRUSTED")

    def _is_root_binding_capture(self, capture: WebCapture) -> bool:
        """根导航是冻结作用域时唯一可复用 HTTP 升级证明的 Capture。"""
        if (
            not self.root_capture_id
            or capture.capture_id != self.root_capture_id
            or capture.depth != 0
            or not capture.redirect_chain
        ):
            return False
        try:
            requested_origin = canonical_web_origin(capture.requested_url)
        except ValueError:
            return False
        return (
            requested_origin == self.site_scope.requested_origin
            and tuple(capture.redirect_chain) == self.site_scope.redirect_chain
            and capture.final_url == self.site_scope.redirect_chain[-1]
        )


@dataclass(frozen=True, kw_only=True)
class WebRuntimeMenuEntry:
    """运行时授权入口，类型独立于发布目录，绝不伪造静态白名单身份。"""

    entry_id: str
    menu_path: tuple[str, ...]
    canonical_url: str | None
    candidate: WebNavigationCandidate
    classification_hint: object | None = None
    source_kind: str = "runtime_discovered"

    @property
    def parent_menu_entry_id(self):
        return self.candidate.parent_menu_entry_id

    @property
    def menu_depth(self):
        return self.candidate.menu_depth

    @property
    def exploration_path(self):
        return self.candidate.exploration_path

    @property
    def logical_target_identity(self):
        return self.candidate.logical_target_identity

    @classmethod
    def from_candidate(cls, candidate: WebNavigationCandidate) -> "WebRuntimeMenuEntry":
        return cls(
            entry_id=discovery_entry_id(candidate),
            menu_path=candidate.menu_path,
            canonical_url=DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(candidate.url)
            if candidate.url
            else None,
            candidate=candidate,
        )


@dataclass(frozen=True, slots=True)
class RuntimeHrefClassification:
    code: str
    resolved_url: str | None = None


def classify_runtime_href(
    base_url: str,
    href: str,
    *,
    site_origin: str | None = None,
) -> RuntimeHrefClassification:
    """按可信页面最终 URL 解析 href，并只授权 canonical 同源导航。"""
    raw_href = str(href).strip()
    if not raw_href or raw_href.startswith("#"):
        return RuntimeHrefClassification(code="invalid_href")
    safety_origin = site_origin or base_url
    if site_origin is not None:
        try:
            if not web_origin_matches(base_url, safety_origin):
                return RuntimeHrefClassification(code="source_origin_untrusted")
        except ValueError:
            return RuntimeHrefClassification(code="source_origin_untrusted")
    try:
        resolved = urljoin(base_url, raw_href)
        parsed = urlsplit(resolved)
    except ValueError:
        return RuntimeHrefClassification(code="invalid_href")
    if parsed.scheme.casefold() not in {"http", "https"}:
        return RuntimeHrefClassification(code="unsafe_scheme")
    if not parsed.netloc:
        return RuntimeHrefClassification(code="invalid_href")
    try:
        same_origin = web_origin_matches(resolved, safety_origin)
    except ValueError:
        return RuntimeHrefClassification(code="invalid_href")
    if not same_origin:
        return RuntimeHrefClassification(code="cross_origin_blocked", resolved_url=resolved)
    return RuntimeHrefClassification(code="actionable", resolved_url=resolved)


def discovery_entry_id(candidate: WebNavigationCandidate) -> str:
    return "discovery:" + candidate.logical_target_identity


def discovery_safety_reason(candidate: WebNavigationCandidate, *, site_origin: str) -> str | None:
    """风险看当前可见语义与目标，不接受候选自报 low 作为唯一证明。"""
    effective_origin = candidate.frontier_site_origin or site_origin
    if candidate.identity_review_reason:
        return "WEB_DISCOVERY_IDENTITY_UNTRUSTED"
    if candidate.risk_level == "cross_origin":
        return "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
    if candidate.side_effect_kind:
        return (
            "WEB_DISCOVERY_DOWNLOAD_BLOCKED"
            if candidate.side_effect_kind == "download"
            else "WEB_DISCOVERY_UNAUTHORIZED_ACTION_BLOCKED"
        )
    text = unquote(
        " ".join(
            (*candidate.menu_path, candidate.label, candidate.url or "", candidate.target_hint)
        )
    ).casefold()
    for reason, pattern in (
        ("LOGIN", r"登录|登入|注册|账号授权|sign.?in|log.?in|log.?out|sign.?up|oauth|authorize"),
        ("PAYMENT", r"支付|付款|充值|购买|下单|结算|订阅|pay|checkout|purchase|subscribe"),
        (
            "DOWNLOAD",
            r"下载|安装|升级|download|install|\.apk(?:$|[?#])|\.exe(?:$|[?#])|\.zip(?:$|[?#])",
        ),
        (
            "UNAUTHORIZED_ACTION",
            r"删除|提交|发布|保存|确认授权|delete|submit|publish|permission|grant|unsubscribe",
        ),
    ):
        if re.search(pattern, text):
            return "WEB_DISCOVERY_" + reason + "_BLOCKED"
    if candidate.url is None:
        return "WEB_DISCOVERY_DIRECT_URL_REQUIRED"
    if (
        candidate.risk_level != "low"
        or candidate.actionability == "blocked"
    ):
        return "WEB_DISCOVERY_UNAUTHORIZED_ACTION"
    if candidate.url:
        try:
            classification = classify_runtime_href(
                candidate.source_page_url or effective_origin,
                candidate.url,
                site_origin=effective_origin,
            )
            if classification.code == "source_origin_untrusted":
                return "WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"
            if classification.code != "actionable":
                return "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
            DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(candidate.url)
        except ValueError:
            return "WEB_DISCOVERY_IDENTITY_UNTRUSTED"
    return None
