"""Web 首页与菜单业务页的广告落地证据调度。"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

_POPUP_MARKERS = ("popup", "pop-up", "pop_ad", "adspop", "modal-ad", "弹窗")
_BANNER_MARKERS = ("banner", "advert", "ad-", "ad_", "sponsor", "promotion", "推广", "广告")
_DANGEROUS_MARKERS = (
    "login",
    "signin",
    "sign-in",
    "register",
    "signup",
    "payment",
    "checkout",
    "download",
    "登录",
    "注册",
    "支付",
    "付款",
    "充值",
    "下载",
)
_AD_BUSINESS_PAGE_ROLES = frozenset({"home", "menu", "detail"})


@dataclass(frozen=True, slots=True)
class WebAdCandidate:
    """从可信 Capture 序列提取的只读广告链接。"""

    url: str
    source_kind: str
    source_capture_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WebAdBusinessPage:
    """一次已打开且就绪的逻辑业务页。"""

    task_id: str
    agent_run_id: str
    session_id: str
    logical_page_id: str
    registered_domain: str
    page_role: str
    page_type: str | None
    source_url: str
    ready_capture_id: str
    capture_snapshots: tuple[tuple[str, Mapping[str, object]], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class WebAdDirectCaptureCommand:
    task_id: str
    agent_run_id: str
    session_id: str
    logical_page_id: str
    source_capture_id: str
    source_url: str
    destination_url: str
    candidate_capture_id: str
    candidate_kind: str
    page_role: str
    page_type: str | None
    deadline: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WebAdLandingCaptureReceipt:
    capture_id: str
    landing_url: str
    artifact_ids: tuple[str, ...]
    artifact_refs: tuple[tuple[str, str], ...]
    source_restored: bool
    landing_reachable: bool = True
    reason_code: str | None = None
    requested_url: str | None = None
    page_type: str | None = "ad_page"


class WebAdLandingCapturePort(Protocol):
    async def capture(
        self,
        command: WebAdDirectCaptureCommand,
    ) -> WebAdLandingCaptureReceipt: ...


class WebAdLandingCaptureError(RuntimeError):
    """广告落地证据采集失败；异常明细不得进入业务结果。"""

    def __init__(self, *, reason_code: str, message: str, detail=None) -> None:
        from services.api.app.application.web_ad_failure_diagnostics import (
            safe_ad_failure_code,
            safe_ad_failure_detail,
        )

        super().__init__(message)
        self.reason_code = safe_ad_failure_code(reason_code)
        self.detail = safe_ad_failure_detail(detail)


@dataclass(frozen=True, slots=True, kw_only=True)
class WebAdLandingEvidenceResult:
    logical_page_id: str
    page_role: str
    page_type: str | None
    status: str
    fail_closed: bool
    reason_code: str | None = None
    capture_id: str | None = None
    source_url: str | None = None
    landing_url: str | None = None
    candidate_kind: str | None = None
    artifact_ids: tuple[str, ...] = ()
    artifact_refs: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_page_id": self.logical_page_id,
            "page_role": self.page_role,
            "page_type": self.page_type,
            "status": self.status,
            "fail_closed": self.fail_closed,
            "reason_code": self.reason_code,
            "capture_id": self.capture_id,
            "source_url": self.source_url,
            "landing_url": self.landing_url,
            "candidate_kind": self.candidate_kind,
            "artifact_ids": list(self.artifact_ids),
            "artifact_refs": dict(self.artifact_refs),
        }


class WebAdLandingEvidenceScheduler:
    """每个逻辑业务页最多执行一次广告证据尝试。"""

    def __init__(
        self,
        *,
        capture_port: WebAdLandingCapturePort,
    ) -> None:
        self._capture_port = capture_port
        self._results: dict[str, WebAdLandingEvidenceResult] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def attempt(self, page: WebAdBusinessPage) -> WebAdLandingEvidenceResult:
        async with self._locks.setdefault(page.logical_page_id, asyncio.Lock()):
            return await self._attempt_once(page)

    async def _attempt_once(self, page: WebAdBusinessPage) -> WebAdLandingEvidenceResult:
        existing = self._results.get(page.logical_page_id)
        if existing is not None:
            return existing
        if page.page_role not in _AD_BUSINESS_PAGE_ROLES or page.page_type in {
            "age_gate",
            "ad_page",
        }:
            result = WebAdLandingEvidenceResult(
                logical_page_id=page.logical_page_id,
                page_role=page.page_role,
                page_type=None,
                status="not_applicable",
                fail_closed=False,
                reason_code="WEB_AD_BUSINESS_PAGE_NOT_APPLICABLE",
                source_url=_url_without_query_or_fragment(page.source_url),
            )
            self._results[page.logical_page_id] = result
            return result
        candidate = select_web_ad_candidate(
            registered_domain=page.registered_domain,
            capture_snapshots=tuple(
                item for item in page.capture_snapshots if item[0] == page.ready_capture_id
            ),
        )
        if candidate is None:
            result = WebAdLandingEvidenceResult(
                logical_page_id=page.logical_page_id,
                page_role=page.page_role,
                page_type=None,
                status="no_candidate",
                fail_closed=True,
                reason_code="WEB_AD_CANDIDATE_NOT_FOUND",
                source_url=_url_without_query_or_fragment(page.source_url),
            )
            self._results[page.logical_page_id] = result
            return result
        try:
            receipt = await self._capture_port.capture(
                WebAdDirectCaptureCommand(
                    task_id=page.task_id,
                    agent_run_id=page.agent_run_id,
                    session_id=page.session_id,
                    logical_page_id=page.logical_page_id,
                    source_capture_id=page.ready_capture_id,
                    source_url=page.source_url,
                    destination_url=candidate.url,
                    candidate_capture_id=candidate.source_capture_id,
                    candidate_kind=candidate.source_kind,
                    page_role=page.page_role,
                    page_type=page.page_type,
                )
            )
        except WebAdLandingCaptureError as exc:
            status = (
                "return_failed"
                if exc.reason_code == "WEB_AD_LANDING_RETURN_FAILED"
                else "open_failed"
            )
            result = WebAdLandingEvidenceResult(
                logical_page_id=page.logical_page_id,
                page_role=page.page_role,
                page_type=None,
                status=status,
                fail_closed=True,
                reason_code=exc.reason_code,
                source_url=_url_without_query_or_fragment(page.source_url),
                candidate_kind=candidate.source_kind,
            )
        except Exception:  # noqa: BLE001 - 仅输出稳定诊断，不泄露 Driver 明细
            result = WebAdLandingEvidenceResult(
                logical_page_id=page.logical_page_id,
                page_role=page.page_role,
                page_type=None,
                status="open_failed",
                fail_closed=True,
                reason_code="WEB_AD_PAGE_OPEN_FAILED",
                source_url=_url_without_query_or_fragment(page.source_url),
                candidate_kind=candidate.source_kind,
            )
        else:
            if receipt.page_type == "ad_page" and receipt.source_restored:
                result = WebAdLandingEvidenceResult(
                    logical_page_id=page.logical_page_id,
                    page_role=page.page_role,
                    page_type=receipt.page_type,
                    status=("observed" if receipt.landing_reachable else "observed_unreachable"),
                    fail_closed=not receipt.landing_reachable,
                    reason_code=receipt.reason_code,
                    capture_id=receipt.capture_id,
                    source_url=_url_without_query_or_fragment(page.source_url),
                    landing_url=_url_without_query_or_fragment(receipt.landing_url),
                    candidate_kind=candidate.source_kind,
                    artifact_ids=receipt.artifact_ids,
                    artifact_refs=receipt.artifact_refs,
                )
            elif receipt.page_type == "ad_page":
                result = WebAdLandingEvidenceResult(
                    logical_page_id=page.logical_page_id,
                    page_role=page.page_role,
                    page_type="ad_page",
                    status="return_failed",
                    fail_closed=True,
                    reason_code="WEB_AD_LANDING_RETURN_FAILED",
                    capture_id=receipt.capture_id,
                    source_url=_url_without_query_or_fragment(page.source_url),
                    landing_url=_url_without_query_or_fragment(receipt.landing_url),
                    candidate_kind=candidate.source_kind,
                    artifact_ids=receipt.artifact_ids,
                    artifact_refs=receipt.artifact_refs,
                )
            else:
                result = WebAdLandingEvidenceResult(
                    logical_page_id=page.logical_page_id,
                    page_role=page.page_role,
                    page_type=None,
                    status="open_failed",
                    fail_closed=True,
                    reason_code=receipt.reason_code or "WEB_AD_LANDING_OPEN_EVIDENCE_INVALID",
                    capture_id=receipt.capture_id,
                    source_url=_url_without_query_or_fragment(page.source_url),
                    landing_url=_url_without_query_or_fragment(receipt.landing_url),
                    candidate_kind=candidate.source_kind,
                    artifact_ids=receipt.artifact_ids,
                    artifact_refs=receipt.artifact_refs,
                )
        self._results[page.logical_page_id] = result
        return result


async def schedule_ready_web_business_page_ad_evidence(
    *,
    scheduler: WebAdLandingEvidenceScheduler,
    snapshot_reader: object,
    identity_policy: object,
    registered_domain: str,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    page_role: str,
    page_type: str | None,
    ready_capture: object,
    evidence_capture_ids: Sequence[str],
) -> WebAdLandingEvidenceResult:
    """把完整就绪序列桥接为一次逻辑业务页广告尝试。"""

    ready_capture_id = str(getattr(ready_capture, "capture_id", "") or "")
    source_url = str(getattr(ready_capture, "url", "") or "")
    if not ready_capture_id or not source_url:
        raise ValueError("广告证据调度要求已就绪 Capture 与 URL")
    logical_id_factory = getattr(identity_policy, "logical_id", None)
    if not callable(logical_id_factory):
        raise TypeError("identity_policy 缺少 logical_id")
    read_snapshot = getattr(snapshot_reader, "read_snapshot", None)
    if not callable(read_snapshot):
        raise TypeError("snapshot_reader 缺少 read_snapshot")
    capture_ids = tuple(
        dict.fromkeys(
            [
                *(str(item) for item in evidence_capture_ids if str(item)),
                ready_capture_id,
            ]
        )
    )
    snapshots: list[tuple[str, Mapping[str, object]]] = []
    for capture_id in capture_ids:
        try:
            snapshot = await read_snapshot(capture_id=capture_id)
        except Exception:  # noqa: BLE001 - 单帧不可读不伪造，保留其余可信序列
            continue
        if isinstance(snapshot, Mapping):
            snapshots.append((capture_id, snapshot))
    return await scheduler.attempt(
        WebAdBusinessPage(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            logical_page_id=str(logical_id_factory(source_url)),
            registered_domain=registered_domain,
            page_role=page_role,
            page_type=page_type,
            source_url=source_url,
            ready_capture_id=ready_capture_id,
            capture_snapshots=tuple(snapshots),
        )
    )


def select_web_ad_candidate(
    *,
    registered_domain: str,
    capture_snapshots: Sequence[tuple[str, Mapping[str, object]]],
    excluded_urls: Sequence[str] = (),
) -> WebAdCandidate | None:
    """优先选择历史弹窗广告，其次选择最终页面横幅广告。"""

    from services.api.app.domain.page.web_ad_destination import canonical_ad_destination

    domain = _normalize_domain(registered_domain)
    excluded = set()
    for url in excluded_urls:
        try:
            excluded.add(canonical_ad_destination(url))
        except ValueError:
            # 历史无效链接不能阻断其他广告，也不放宽当前候选的地址校验。
            continue
    candidates: list[tuple[int, int, WebAdCandidate]] = []
    for capture_index, (capture_id, snapshot) in enumerate(capture_snapshots):
        if not isinstance(capture_id, str) or not capture_id.strip():
            continue
        elements = snapshot.get("elements")
        if not isinstance(elements, list):
            continue
        for element_index, raw in enumerate(elements):
            if not isinstance(raw, Mapping) or raw.get("visible") is False:
                continue
            href = _safe_external_ad_url(raw.get("href"), registered_domain=domain)
            if href is None:
                continue
            try:
                destination = canonical_ad_destination(href)
            except ValueError:
                continue
            if destination in excluded:
                continue
            semantic_parts: list[str] = []
            for key in (
                    "class_name",
                    "class",
                    "id",
                    "role",
                    "text",
                    "aria_label",
                    "alt",
                    "selector",
                    "ancestor_class_name",
                    "ancestor_id",
                    "class_tokens",
                    "parent_class_tokens",
                    "ancestor_class_tokens",
                ):
                value = raw.get(key)
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    semantic_parts.extend(str(item) for item in value if str(item))
                elif value:
                    semantic_parts.append(str(value))
            semantic_text = " ".join(semantic_parts).casefold()
            if any(marker.casefold() in semantic_text for marker in _DANGEROUS_MARKERS):
                continue
            source_kind = (
                "popup"
                if any(marker.casefold() in semantic_text for marker in _POPUP_MARKERS)
                else "banner"
                if raw.get("is_advertisement") is True
                or any(marker.casefold() in semantic_text for marker in _BANNER_MARKERS)
                else None
            )
            if source_kind is None:
                continue
            priority = 0 if source_kind == "popup" else 1
            candidates.append(
                (
                    priority,
                    capture_index * 10_000 + element_index,
                    WebAdCandidate(
                        url=href,
                        source_kind=source_kind,
                        source_capture_id=str(capture_id),
                    ),
                )
            )
    return min(candidates, default=(0, 0, None), key=lambda item: (item[0], item[1]))[2]


def _safe_external_ad_url(value: object, *, registered_domain: str) -> str | None:
    from services.api.app.application.web_ad_outbound_policy import safe_ad_url

    value = safe_ad_url(value)
    if value is None:
        return None
    parsed = urlsplit(value.strip())
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or not _is_domain_ad_host(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or hostname == registered_domain
        or hostname.endswith("." + registered_domain)
    ):
        return None
    try:
        _ = parsed.port
    except ValueError:
        return None
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _is_domain_ad_host(hostname: str) -> bool:
    """广告候选只接受域名，排除标准 IP 与浏览器可识别的数字地址。"""
    from services.api.app.domain.page.web_origin import canonical_web_host

    try:
        hostname = canonical_web_host(hostname).rstrip(".")
    except ValueError:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        # 浏览器会将整数、缩写、八进制及十六进制 IPv4 当作地址而非域名。
        last_label = hostname.rsplit(".", 1)[-1]
        return not (
            last_label.isdecimal()
            or (
                last_label.startswith("0x")
                and all(char in "0123456789abcdef" for char in last_label[2:])
            )
        )
    return False


def _normalize_domain(value: str) -> str:
    domain = str(value or "").strip().rstrip(".").casefold()
    if not domain or "/" in domain or ":" in domain:
        raise ValueError("registered_domain 必须是规范域名")
    return domain


def _url_without_query_or_fragment(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
