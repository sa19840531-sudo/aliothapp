"""Web 搜索与活动辅助入口的只读发现和 href 直达编排。"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.web_ad_landing_evidence import (
    schedule_ready_web_business_page_ad_evidence,
)
from services.api.app.application.web_exploration_workflow import (
    RunWebWorkflowCommand,
    WebCaptureStage,
    WebSurfaceBootstrapper,
)
from services.api.app.application.web_navigation_safety import (
    WebNavigationSafetyError,
    validate_web_final_url,
)
from services.api.app.application.web_page_readiness import web_readiness_result_is_ready
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY

WebAuxiliaryNavigationKind = Literal[
    "search_href",
    "activity_href",
    "external_promotion",
]

_SEARCH_MARKERS = ("搜索", "搜寻", "查找", "search", "query")
_ACTIVITY_MARKERS = (
    "活动",
    "专题",
    "领取",
    "参与",
    "赛事",
    "campaign",
    "event",
    "promotion",
    "promo",
)
_DANGEROUS_MARKERS = (
    "登录",
    "注册",
    "支付",
    "付款",
    "充值",
    "下载",
    "login",
    "signin",
    "signup",
    "payment",
    "checkout",
    "download",
    "logout",
)
_EXCLUDED_REGION_KEYS = (
    "is_advertisement",
    "within_footer",
    "within_breadcrumb",
    "within_pagination",
)


@dataclass(frozen=True, kw_only=True, slots=True)
class WebAuxiliaryNavigationCandidate:
    """由可信 DOM Snapshot 签发的 Web 辅助入口。"""

    candidate_id: str
    source_capture_id: str
    kind: WebAuxiliaryNavigationKind
    label: str
    selector: str | None
    url: str
    confidence: float


@dataclass(frozen=True, kw_only=True, slots=True)
class WebAuxiliaryNavigationResult:
    status: str
    source_capture_id: str
    candidate_id: str | None = None
    phase_key: str | None = None
    probe_capture_id: str | None = None
    ready_capture_id: str | None = None
    page_type: str | None = None
    reason_code: str | None = None
    ad_landing_evidence: Mapping[str, object] | None = None


class _ExpectedAuxiliaryNavigationFailure(Exception):
    """把可预期的导航失败带出操作异常边界后再持久化。"""

    def __init__(self, *, reason_code: str, probe_capture_id: str | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.probe_capture_id = probe_capture_id


class WebAuxiliaryNavigationClaims(Protocol):
    """以 qc_surface_phase_run 为生产实现的一次性阶段领取端口。"""

    async def claim(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        phase_key: str,
        source_capture_id: str,
        candidate_source_capture_id: str,
        candidate_id: str,
    ) -> str | None: ...

    async def finish(
        self,
        *,
        phase_id: str,
        status: Literal["completed", "failed"],
        reason_code: str,
        result_capture_id: str | None,
        page_type: str | None,
    ) -> None: ...


def discover_web_auxiliary_navigation_candidates(
    snapshot: Mapping[str, object],
    *,
    origin: str,
) -> tuple[WebAuxiliaryNavigationCandidate, ...]:
    """只提取有有效 href 的搜索、同源活动和跨域推广入口。"""

    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot 必须为对象")
    source_capture_id = str(snapshot.get("capture_id") or "").strip()
    if not source_capture_id:
        raise ValueError("WEB_AUXILIARY_SOURCE_CAPTURE_MISSING")
    source_url = str(snapshot.get("url") or "").strip()
    try:
        validate_web_final_url(
            site_url=origin,
            requested_url=origin,
            final_url=source_url,
        )
    except WebNavigationSafetyError:
        # 声明的站点边界必须与可信来源 Snapshot 绑定，拒绝伪造 origin。
        return ()
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return ()
    candidates: list[WebAuxiliaryNavigationCandidate] = []
    occurrences: dict[tuple[str, str], int] = {}
    for raw in elements:
        if not isinstance(raw, Mapping) or not _element_is_available(raw):
            continue
        href = raw.get("href")
        if not isinstance(href, str) or not href.strip():
            # 用户约束：无 href 搜索图标、JS 按钮和活动按钮均不点击。
            continue
        label = _element_semantics(raw)
        target = _canonical_href(href, base_url=source_url)
        if target is None or _has_marker(f"{label} {target}", _DANGEROUS_MARKERS):
            continue
        target_hostname = str(urlsplit(target).hostname or "").casefold()
        origin_hostname = str(urlsplit(origin).hostname or "").casefold()
        same_origin = target_hostname == origin_hostname
        if same_origin:
            try:
                validate_web_final_url(
                    site_url=origin,
                    requested_url=origin,
                    final_url=target,
                )
            except WebNavigationSafetyError:
                continue
        advertised = any(raw.get(key) is True for key in _EXCLUDED_REGION_KEYS)
        if _has_marker(label + " " + urlsplit(target).path, _SEARCH_MARKERS):
            if advertised or not same_origin:
                continue
            kind: WebAuxiliaryNavigationKind = "search_href"
        elif _has_marker(label + " " + urlsplit(target).path, _ACTIVITY_MARKERS):
            if same_origin:
                if advertised:
                    continue
                kind = "activity_href"
            else:
                kind = "external_promotion"
        else:
            continue
        confidence = _confidence(raw)
        if confidence < 0.7:
            continue
        occurrence_key = (kind, target)
        occurrence = occurrences.get(occurrence_key, 0) + 1
        occurrences[occurrence_key] = occurrence
        identity = "\x1f".join((source_capture_id, kind, target, label, str(occurrence)))
        candidates.append(
            WebAuxiliaryNavigationCandidate(
                candidate_id=(
                    "web-auxiliary:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                ),
                source_capture_id=source_capture_id,
                kind=kind,
                label=label or kind,
                selector=(
                    str(raw.get("selector")).strip()
                    if isinstance(raw.get("selector"), str) and str(raw.get("selector")).strip()
                    else None
                ),
                url=target,
                confidence=confidence,
            )
        )
    return tuple(candidates)


def web_snapshot_has_open_search_surface(snapshot: Mapping[str, object]) -> bool:
    """只识别正文中已自然展开的搜索界面，不把全站顶栏表单当搜索页。"""

    elements = snapshot.get("elements") if isinstance(snapshot, Mapping) else None
    if not isinstance(elements, list):
        return False
    visible_body = [
        item
        for item in elements
        if isinstance(item, Mapping)
        and item.get("visible") is not False
        and item.get("disabled") is not True
        and not _is_global_or_ad_region(item)
    ]
    inputs = [item for item in visible_body if _is_search_input(item)]
    if not inputs:
        return False
    has_results = any(
        _has_marker(
            " ".join(
                (
                    _element_semantics(item),
                    _token_text(item.get("class_tokens")),
                    str(item.get("id") or ""),
                    str(item.get("role") or ""),
                )
            ),
            ("search-result", "search_result", "搜索结果", "query-result"),
        )
        for item in visible_body
    )
    if has_results:
        return True
    triggers = [
        item
        for item in visible_body
        if not _is_search_input(item)
        and item.get("interactive") is not False
        and str(item.get("tag") or "").casefold() in {"button", "a", "input"}
        and _has_marker(_element_semantics(item), _SEARCH_MARKERS)
    ]
    for search_input in inputs:
        container_id = _search_container_identity(search_input)
        if not container_id:
            continue
        for trigger in triggers:
            if _search_container_identity(trigger) == container_id and (
                _search_container_has_search_semantics(search_input)
                or _search_container_has_search_semantics(trigger)
            ):
                return True
    return False


class WebAuxiliaryNavigationExplorer:
    """零输入、零提交地分类自然搜索态或直达 href 辅助页面。"""

    def __init__(
        self,
        *,
        coordinator: object,
        snapshot_reader: object,
        capture_url_reader: object | None = None,
        readiness: object,
        ready_capture_reader: object,
        classifier: object,
        claims: WebAuxiliaryNavigationClaims | None = None,
        ad_landing_scheduler: object | None = None,
        identity_policy: object = DEFAULT_WEB_URL_IDENTITY_POLICY,
    ) -> None:
        self._coordinator = coordinator
        self._snapshot_reader = snapshot_reader
        self._capture_url_reader = capture_url_reader or snapshot_reader
        self._readiness = readiness
        self._ready_capture_reader = ready_capture_reader
        self._classifier = classifier
        self._claims = claims
        self._ad_landing_scheduler = ad_landing_scheduler
        self._identity_policy = identity_policy

    async def explore_all(
        self,
        command: RunWebWorkflowCommand,
        *,
        source_capture_id: str,
    ) -> tuple[WebAuxiliaryNavigationResult, ...]:
        """每类最多执行一个可信 href；每次动作后只从新的当前 Capture 继续。"""

        results: list[WebAuxiliaryNavigationResult] = []
        current_capture_id = source_capture_id

        initial_snapshot = await _read_snapshot(self._snapshot_reader, current_capture_id)
        initial_candidates = discover_web_auxiliary_navigation_candidates(
            initial_snapshot,
            origin=command.target_url,
        )
        external = next(
            (item for item in initial_candidates if item.kind == "external_promotion"),
            None,
        )
        if external is not None:
            results.append(
                await self.explore(
                    command,
                    source_capture_id=current_capture_id,
                    candidate=external,
                )
            )

        for kind in ("search_href", "activity_href"):
            current_snapshot = await _read_snapshot(
                self._snapshot_reader,
                current_capture_id,
            )
            current_candidates = discover_web_auxiliary_navigation_candidates(
                current_snapshot,
                origin=command.target_url,
            )
            if kind == "search_href" and web_snapshot_has_open_search_surface(current_snapshot):
                result = await self.explore(command, source_capture_id=current_capture_id)
            else:
                selected = next(
                    (item for item in current_candidates if item.kind == kind),
                    None,
                )
                if selected is None:
                    continue
                result = await self.explore(
                    command,
                    source_capture_id=current_capture_id,
                    candidate=selected,
                    _candidate_snapshot=current_snapshot,
                )
            results.append(result)
            navigated_capture_id = str(result.ready_capture_id or result.probe_capture_id or "")
            if navigated_capture_id:
                current_capture_id = navigated_capture_id

        if not results:
            return (
                WebAuxiliaryNavigationResult(
                    status="no_candidate",
                    source_capture_id=source_capture_id,
                    reason_code="WEB_AUXILIARY_CANDIDATE_NOT_FOUND",
                ),
            )
        return tuple(results)

    async def stage_candidates(self, command, *, source_capture_id):
        """阶段路径只读取可信候选；导航与分类交给独立持久游标推进。"""
        snapshot = await _read_snapshot(self._snapshot_reader, source_capture_id)
        return discover_web_auxiliary_navigation_candidates(snapshot, origin=command.target_url)

    async def stage_collection_candidates(self, command, *, source_capture_id):
        from services.api.app.application.web_collection_navigation import collection_entries

        snapshot = await _read_snapshot(self._snapshot_reader, source_capture_id)
        return collection_entries(snapshot, origin=command.target_url)

    async def stage_collection_navigate(self, command, *, source_capture_id,
                                       candidate_id, requested_url):
        from services.api.app.application.web_collection_navigation import (
            navigate_collection_entry,
            validate_collection_arrival,
        )

        snapshot = await _read_snapshot(self._snapshot_reader, source_capture_id)
        capture_id = await navigate_collection_entry(
            self._coordinator, command, snapshot=snapshot,
            candidate_id=candidate_id, requested_url=requested_url,
        )
        final_url = await _read_capture_url(self._capture_url_reader, capture_id=capture_id)
        validate_collection_arrival(command, requested_url=requested_url, final_url=final_url)
        return capture_id

    async def stage_navigate(self, command, *, source_capture_id, candidate_id, requested_url):
        """只执行已冻结辅助候选的 GOTO，不分类、不调用旧广告直开链。"""
        from services.api.app.application.web_run_stage_orchestrator import WebStageFailure

        candidates = await self.stage_candidates(command, source_capture_id=source_capture_id)
        selected = next((
            item for item in candidates
            if item.candidate_id == candidate_id and item.url == requested_url
            and item.kind in {"search_href", "activity_href"}
        ), None)
        if selected is None:
            raise WebStageFailure("WEB_AUXILIARY_CANDIDATE_STALE")
        if self._claims is None:
            raise WebStageFailure("WEB_AUXILIARY_PHASE_CLAIMS_REQUIRED")
        phase_key = (
            "web_search_discovery" if selected.kind == "search_href" else "web_activity_navigation"
        )
        phase_id = await self._claim(
            command, selected, phase_key=phase_key, source_capture_id=source_capture_id,
        )
        if phase_id is None:
            raise WebStageFailure("WEB_AUXILIARY_ALREADY_ATTEMPTED")
        result = await self._coordinator.coordinate(CaptureCoordinationCommand(
            agent_run_id=command.agent_run_id,
            intent=_navigation_intent(
                command, source_capture_id=source_capture_id,
                candidate=selected, phase_key=phase_key,
            ),
        ))
        capture_id = str(getattr(getattr(result, "after_capture", None), "capture_id", "") or "")
        if not capture_id:
            raise WebStageFailure("WEB_AUXILIARY_PROBE_MISSING")
        final_url = await _read_capture_url(self._capture_url_reader, capture_id=capture_id)
        try:
            validate_web_final_url(
                site_url=command.target_url, requested_url=requested_url, final_url=final_url,
            )
        except WebNavigationSafetyError as exc:
            raise WebStageFailure(exc.error_code) from exc
        return capture_id

    async def stage_finish(self, *, command, cursor, **kwargs):
        """辅助阶段终态复用原 claim，不影响广告子游标及菜单预算。"""
        finish = getattr(self._claims, "finish_stage", None)
        if callable(finish):
            await finish(command=command, cursor=cursor, **kwargs)
        else:
            await self._finish(phase_id=cursor.cursor_payload["surface_phase_run_id"], **kwargs)

    async def explore(
        self,
        command: RunWebWorkflowCommand,
        *,
        source_capture_id: str,
        candidate: WebAuxiliaryNavigationCandidate | None = None,
        _candidate_snapshot: Mapping[str, object] | None = None,
    ) -> WebAuxiliaryNavigationResult:
        snapshot = (
            _candidate_snapshot
            if _candidate_snapshot is not None
            else await _read_snapshot(self._snapshot_reader, source_capture_id)
        )
        if candidate is None and web_snapshot_has_open_search_surface(snapshot):
            ready = await self._ready_capture_reader.read(
                capture_id=source_capture_id,
                page_role="menu",
            )
            assessment = await self._classifier.classify(ready)
            page_type = str(getattr(assessment, "page_type", "") or "") or None
            ad_evidence = await self._schedule_business_page_ad(
                command,
                ready=ready,
                page_role="search",
                page_type=page_type,
            )
            return WebAuxiliaryNavigationResult(
                status="classified_existing_search_surface",
                source_capture_id=source_capture_id,
                ready_capture_id=source_capture_id,
                page_type=page_type,
                phase_key="web_search_discovery",
                ad_landing_evidence=ad_evidence,
            )
        current = discover_web_auxiliary_navigation_candidates(snapshot, origin=command.target_url)
        selected = candidate or (current[0] if current else None)
        if selected is None:
            return WebAuxiliaryNavigationResult(
                status="no_candidate",
                source_capture_id=source_capture_id,
                reason_code="WEB_AUXILIARY_CANDIDATE_NOT_FOUND",
            )
        matches = [item for item in current if item.candidate_id == selected.candidate_id]
        snapshot_capture_id = str(snapshot.get("capture_id") or "")
        if (
            len(matches) != 1
            or selected.source_capture_id != snapshot_capture_id
            or selected.source_capture_id != source_capture_id
        ):
            return WebAuxiliaryNavigationResult(
                status="candidate_stale",
                source_capture_id=source_capture_id,
                candidate_id=selected.candidate_id,
                reason_code="WEB_AUXILIARY_CANDIDATE_STALE",
            )
        selected = matches[0]
        if selected.kind == "external_promotion":
            # 首页/业务页广告由统一 stage 路径按 ready Capture 领取；辅助入口只
            # 留下候选事实，不能用另一套逻辑 ID 再次打开同一广告。
            return WebAuxiliaryNavigationResult(
                status="external_promotion_deferred",
                source_capture_id=source_capture_id,
                candidate_id=selected.candidate_id,
                reason_code="WEB_AD_STAGE_PATH_REQUIRED",
            )
        phase_key = (
            "web_search_discovery" if selected.kind == "search_href" else "web_activity_navigation"
        )
        if self._claims is None:
            return WebAuxiliaryNavigationResult(
                status="phase_claim_unavailable",
                source_capture_id=source_capture_id,
                candidate_id=selected.candidate_id,
                phase_key=phase_key,
                reason_code="WEB_AUXILIARY_PHASE_CLAIMS_REQUIRED",
            )
        phase_id = await self._claim(
            command,
            selected,
            phase_key=phase_key,
            source_capture_id=source_capture_id,
        )
        if phase_id is None:
            return WebAuxiliaryNavigationResult(
                status="already_attempted",
                source_capture_id=source_capture_id,
                candidate_id=selected.candidate_id,
                phase_key=phase_key,
                reason_code="WEB_AUXILIARY_ALREADY_ATTEMPTED",
            )
        intent = _navigation_intent(
            command,
            source_capture_id=source_capture_id,
            candidate=selected,
            phase_key=phase_key,
        )
        try:
            coordination = await self._coordinator.coordinate(
                CaptureCoordinationCommand(agent_run_id=command.agent_run_id, intent=intent)
            )
            probe_capture_id = str(
                getattr(getattr(coordination, "after_capture", None), "capture_id", "") or ""
            )
            if not probe_capture_id:
                raise _ExpectedAuxiliaryNavigationFailure(
                    reason_code="WEB_AUXILIARY_PROBE_MISSING",
                )
            try:
                final_url = await _read_capture_url(
                    self._capture_url_reader,
                    capture_id=probe_capture_id,
                )
                validate_web_final_url(
                    site_url=command.target_url,
                    requested_url=selected.url,
                    final_url=final_url,
                )
            except WebNavigationSafetyError as exc:
                raise _ExpectedAuxiliaryNavigationFailure(
                    reason_code=exc.error_code,
                    probe_capture_id=probe_capture_id,
                ) from exc
            except (AttributeError, KeyError, TypeError, ValueError):
                raise _ExpectedAuxiliaryNavigationFailure(
                    reason_code="WEB_AUXILIARY_FINAL_URL_UNAVAILABLE",
                    probe_capture_id=probe_capture_id,
                ) from None
            readiness = await self._readiness.prepare(
                source_capture_id=probe_capture_id,
                page_role="menu",
            )
            if not web_readiness_result_is_ready(readiness):
                raise _ExpectedAuxiliaryNavigationFailure(
                    reason_code="WEB_AUXILIARY_NOT_READY",
                    probe_capture_id=probe_capture_id,
                )
            ready_capture_id = str(readiness.ready_capture_id)
            ready = await self._ready_capture_reader.read(
                capture_id=ready_capture_id,
                page_role="menu",
            )
            assessment = await self._classifier.classify(ready)
            page_type = str(getattr(assessment, "page_type", "") or "") or None
            ad_evidence = await self._schedule_business_page_ad(
                command,
                ready=ready,
                page_role=("search" if phase_key == "web_search_discovery" else "activity"),
                page_type=page_type,
            )
        except _ExpectedAuxiliaryNavigationFailure as failure:
            return await self._finish_failure(
                phase_id,
                source_capture_id=source_capture_id,
                selected=selected,
                phase_key=phase_key,
                reason_code=failure.reason_code,
                probe_capture_id=failure.probe_capture_id,
            )
        except asyncio.CancelledError:
            await _finish_claim_after_cancellation(
                self._finish(
                    phase_id=phase_id,
                    status="failed",
                    reason_code="WEB_AUXILIARY_CANCELLED",
                    result_capture_id=None,
                    page_type=None,
                )
            )
            raise
        except Exception:
            return await self._finish_failure(
                phase_id,
                source_capture_id=source_capture_id,
                selected=selected,
                phase_key=phase_key,
                reason_code="WEB_AUXILIARY_NAVIGATION_FAILED",
            )
        expected_page_type = "search_page" if phase_key == "web_search_discovery" else "activity"
        if page_type != expected_page_type:
            return await self._finish_failure(
                phase_id,
                source_capture_id=source_capture_id,
                selected=selected,
                phase_key=phase_key,
                reason_code="WEB_AUXILIARY_PAGE_TYPE_MISMATCH",
                probe_capture_id=probe_capture_id,
                ready_capture_id=ready_capture_id,
                observed_page_type=page_type,
                ad_landing_evidence=ad_evidence,
            )
        try:
            await self._finish(
                phase_id=phase_id,
                status="completed",
                reason_code="WEB_AUXILIARY_CLASSIFIED",
                result_capture_id=ready_capture_id,
                page_type=page_type,
            )
        except asyncio.CancelledError:
            await _finish_claim_after_cancellation(
                self._finish(
                    phase_id=phase_id,
                    status="failed",
                    reason_code="WEB_AUXILIARY_CANCELLED",
                    result_capture_id=None,
                    page_type=None,
                )
            )
            raise
        return WebAuxiliaryNavigationResult(
            status="classified",
            source_capture_id=source_capture_id,
            candidate_id=selected.candidate_id,
            phase_key=phase_key,
            probe_capture_id=probe_capture_id,
            ready_capture_id=ready_capture_id,
            page_type=page_type,
            reason_code="WEB_AUXILIARY_CLASSIFIED",
            ad_landing_evidence=ad_evidence,
        )

    async def _schedule_business_page_ad(
        self,
        command: RunWebWorkflowCommand,
        *,
        ready: object,
        page_role: Literal["search", "activity"],
        page_type: str | None,
    ) -> Mapping[str, object] | None:
        """在离开辅助业务页前按同一 ready Capture 完成唯一广告尝试。"""

        if self._ad_landing_scheduler is None:
            return None
        try:
            result = await schedule_ready_web_business_page_ad_evidence(
                scheduler=self._ad_landing_scheduler,
                snapshot_reader=self._snapshot_reader,
                identity_policy=self._identity_policy,
                registered_domain=str(urlsplit(command.target_url).hostname or ""),
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                page_role=page_role,
                page_type=page_type,
                ready_capture=ready,
                evidence_capture_ids=(str(getattr(ready, "capture_id", "") or ""),),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return {
                "status": "open_failed",
                "fail_closed": True,
                "reason_code": "WEB_AD_EVIDENCE_SCHEDULING_FAILED",
            }
        to_dict = getattr(result, "to_dict", None)
        return to_dict() if callable(to_dict) else None

    async def _claim(
        self,
        command,
        selected,
        *,
        phase_key: str,
        source_capture_id: str,
    ) -> str | None:
        assert self._claims is not None
        return await self._claims.claim(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            session_id=command.session_id,
            phase_key=phase_key,
            source_capture_id=source_capture_id,
            candidate_source_capture_id=selected.source_capture_id,
            candidate_id=selected.candidate_id,
        )

    async def _finish(self, **kwargs) -> None:
        if self._claims is not None:
            await self._claims.finish(**kwargs)

    async def _finish_failure(
        self,
        phase_id: str,
        *,
        source_capture_id: str,
        selected: WebAuxiliaryNavigationCandidate,
        phase_key: str,
        reason_code: str,
        probe_capture_id: str | None = None,
        ready_capture_id: str | None = None,
        observed_page_type: str | None = None,
        ad_landing_evidence: Mapping[str, object] | None = None,
    ) -> WebAuxiliaryNavigationResult:
        try:
            await self._finish(
                phase_id=phase_id,
                status="failed",
                reason_code=reason_code,
                result_capture_id=ready_capture_id or probe_capture_id,
                page_type=None,
            )
        except asyncio.CancelledError:
            await _finish_claim_after_cancellation(
                self._finish(
                    phase_id=phase_id,
                    status="failed",
                    reason_code="WEB_AUXILIARY_CANCELLED",
                    result_capture_id=None,
                    page_type=None,
                )
            )
            raise
        return WebAuxiliaryNavigationResult(
            status="failed",
            source_capture_id=source_capture_id,
            candidate_id=selected.candidate_id,
            phase_key=phase_key,
            probe_capture_id=probe_capture_id,
            ready_capture_id=ready_capture_id,
            page_type=observed_page_type,
            reason_code=reason_code,
            ad_landing_evidence=ad_landing_evidence,
        )

    async def _schedule_external(
        self,
        command,
        *,
        snapshot,
        selected,
        phase_id,
        phase_key,
    ) -> WebAuxiliaryNavigationResult:
        if self._ad_landing_scheduler is None:
            return await self._finish_failure(
                phase_id,
                source_capture_id=selected.source_capture_id,
                selected=selected,
                phase_key=phase_key,
                reason_code="WEB_AD_EVIDENCE_SCHEDULER_MISSING",
            )
        try:
            ready = await self._ready_capture_reader.read(
                capture_id=selected.source_capture_id,
                page_role="menu",
            )
            ad_result = await schedule_ready_web_business_page_ad_evidence(
                scheduler=self._ad_landing_scheduler,
                snapshot_reader=self._snapshot_reader,
                identity_policy=self._identity_policy,
                registered_domain=str(urlsplit(command.target_url).hostname or ""),
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                page_role="menu",
                page_type="activity",
                ready_capture=ready,
                evidence_capture_ids=(selected.source_capture_id,),
            )
        except asyncio.CancelledError:
            await _finish_claim_after_cancellation(
                self._finish(
                    phase_id=phase_id,
                    status="failed",
                    reason_code="WEB_AUXILIARY_CANCELLED",
                    result_capture_id=None,
                    page_type=None,
                )
            )
            raise
        except Exception:
            return await self._finish_failure(
                phase_id,
                source_capture_id=selected.source_capture_id,
                selected=selected,
                phase_key=phase_key,
                reason_code="WEB_AD_EVIDENCE_SCHEDULING_FAILED",
            )
        reason_code = str(getattr(ad_result, "reason_code", "") or "") or (
            "WEB_AD_EVIDENCE_SCHEDULED"
        )
        try:
            await self._finish(
                phase_id=phase_id,
                status="failed",
                reason_code=reason_code,
                result_capture_id=None,
                page_type=None,
            )
        except asyncio.CancelledError:
            await _finish_claim_after_cancellation(
                self._finish(
                    phase_id=phase_id,
                    status="failed",
                    reason_code="WEB_AUXILIARY_CANCELLED",
                    result_capture_id=None,
                    page_type=None,
                )
            )
            raise
        payload = ad_result.to_dict() if callable(getattr(ad_result, "to_dict", None)) else {}
        return WebAuxiliaryNavigationResult(
            status="external_promotion_scheduled",
            source_capture_id=selected.source_capture_id,
            candidate_id=selected.candidate_id,
            phase_key=phase_key,
            reason_code=reason_code,
            ad_landing_evidence=payload,
        )


def web_auxiliary_semantic_target(kind: WebAuxiliaryNavigationKind) -> str:
    """为可信辅助入口生成不依赖页面定位字段的语义目标。"""

    if kind == "search_href":
        return "进入搜索页面"
    if kind == "activity_href":
        return "进入活动页面"
    raise ValueError("WEB_AUXILIARY_CANDIDATE_KIND_NOT_NAVIGABLE")


def _navigation_intent(command, *, source_capture_id, candidate, phase_key):
    intent_id = f"web-auxiliary:{command.agent_run_id}:{candidate.candidate_id}"
    return ActionIntent(
        task_id=command.task_id,
        session_id=command.session_id,
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=source_capture_id,
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=web_auxiliary_semantic_target(candidate.kind),
        purpose="通过当前 Playwright 页面直接打开 Web 辅助 href",
        capture_profile=WebSurfaceBootstrapper.capture_profile(),
        deadline=command.deadline,
        parameters={
            "target_mode": "web_search_discovery_candidate",
            "destination_url": candidate.url,
            "web_capture_stage": WebCaptureStage.PAGE_PROBE.value,
            "web_auxiliary_candidate_id": candidate.candidate_id,
            "web_auxiliary_source_capture_id": source_capture_id,
            "web_auxiliary_candidate_source_capture_id": candidate.source_capture_id,
            "web_auxiliary_origin": command.target_url,
            "web_auxiliary_kind": candidate.kind,
            "web_auxiliary_phase_key": phase_key,
            "clear_obstructions": False,
            "full_page": False,
        },
        risk_level=InteractionRiskLevel.LOW,
    )


async def _read_snapshot(reader: object, capture_id: str) -> Mapping[str, object]:
    method = getattr(reader, "read_snapshot", None)
    if callable(method):
        snapshot = await method(capture_id=capture_id)
    else:
        method = getattr(reader, "read_ui_snapshot", None)
        if not callable(method):
            raise TypeError("snapshot_reader 缺少可信 Snapshot 读取接口")
        snapshot = await method(page_capture_id=capture_id)
    if not isinstance(snapshot, Mapping):
        raise TypeError("可信 Snapshot 必须为对象")
    return snapshot


async def _read_capture_url(reader: object, *, capture_id: str) -> str:
    method = getattr(reader, "read_url", None)
    if not callable(method):
        raise TypeError("capture_url_reader 缺少可信 URL 读取接口")
    value = await method(capture_id=capture_id)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("可信 capture URL 缺失")
    return value.strip()


async def _finish_claim_after_cancellation(finish_operation) -> None:
    """尽力关闭已领取阶段，同时保留原始协程取消。"""

    task = asyncio.create_task(finish_operation)
    try:
        await asyncio.shield(task)
    except BaseException:  # noqa: BLE001 - 终态失败不得覆盖取消。
        pass


def _canonical_href(value: str, *, base_url: str) -> str | None:
    try:
        target = urljoin(base_url, value.strip())
        return DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(target)
    except (TypeError, ValueError):
        return None


def _element_is_available(element: Mapping[str, object]) -> bool:
    return (
        element.get("disabled") is not True
        and element.get("inert") is not True
        and str(element.get("aria_disabled") or "").casefold() != "true"
        and str(element.get("aria-disabled") or "").casefold() != "true"
        and element.get("interactive") is not False
    )


def _is_search_input(element: Mapping[str, object]) -> bool:
    if str(element.get("tag") or "").casefold() not in {"input", "textarea"}:
        return False
    input_type = str(element.get("type") or "").casefold()
    role = str(element.get("role") or "").casefold()
    return (
        input_type == "search"
        or role == "searchbox"
        or _has_marker(_element_semantics(element), _SEARCH_MARKERS)
    )


def _is_global_or_ad_region(element: Mapping[str, object]) -> bool:
    if any(
        element.get(key) is True
        for key in (
            "is_advertisement",
            "within_advertisement",
            "within_navigation",
            "within_web_navigation",
            "within_header",
            "within_footer",
            "within_breadcrumb",
        )
    ):
        return True
    ancestor_tokens = _token_text(element.get("ancestor_class_tokens"))
    return _has_marker(
        ancestor_tokens,
        (
            "header",
            "footer",
            "navbar",
            "nav-bar",
            "navigation",
            "breadcrumb",
            "advert",
            "banner",
        ),
    )


def _search_container_identity(element: Mapping[str, object]) -> str:
    parent_selector = str(element.get("parent_selector") or "").strip()
    return f"parent_selector:{parent_selector}" if parent_selector else ""


def _search_container_has_search_semantics(element: Mapping[str, object]) -> bool:
    return _has_marker(
        " ".join(
            (
                str(element.get("parent_selector") or ""),
                _token_text(element.get("parent_class_tokens")),
                _token_text(element.get("ancestor_class_tokens")),
            )
        ),
        _SEARCH_MARKERS,
    )


def _element_semantics(element: Mapping[str, object]) -> str:
    return " ".join(
        str(element.get(name) or "")
        for name in (
            "text",
            "aria_label",
            "title",
            "placeholder",
            "name",
            "id",
            "class",
        )
    ).strip()


def _token_text(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _has_marker(value: str, markers: tuple[str, ...]) -> bool:
    normalized = re.sub(r"\s+", " ", value).casefold()
    return any(marker.casefold() in normalized for marker in markers)


def _confidence(element: Mapping[str, object]) -> float:
    value = element.get("navigation_confidence", element.get("confidence", 1.0))
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    result = float(value)
    return result if math.isfinite(result) and 0 <= result <= 1 else 0.0


__all__ = [
    "WebAuxiliaryNavigationCandidate",
    "WebAuxiliaryNavigationExplorer",
    "WebAuxiliaryNavigationKind",
    "WebAuxiliaryNavigationResult",
    "discover_web_auxiliary_navigation_candidates",
    "web_auxiliary_semantic_target",
    "web_snapshot_has_open_search_surface",
]
