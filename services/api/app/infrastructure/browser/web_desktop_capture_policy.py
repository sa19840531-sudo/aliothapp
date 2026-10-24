"""Web-only 桌面采集策略：直达首页、逐页清障、导航发现与无损压缩。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any
from urllib.parse import urljoin

from PIL import Image

from services.api.app.application.web_assessment_evidence import (
    validate_web_full_page_supplemental_png,
)
from services.api.app.application.web_desktop_navigation import (
    WEB_NAVIGATION_SOURCE_SELECTOR,
    WebNavigationCandidate,
    bind_navigation_candidate_source,
    extract_navigation_candidates,
    isolate_navigation_candidate_identity,
    navigation_snapshot_from_html,
)
from services.api.app.application.web_exploration_workflow import WebCaptureStage
from services.api.app.application.web_menu_lineage_validation import (
    persisted_menu_lineages_are_valid,
)
from services.api.app.application.web_navigation_container_identity import (
    WebNavigationContainerRegistry,
)
from services.api.app.application.web_navigation_lineage import (
    reconcile_navigation_lineages,
)
from services.api.app.application.web_navigation_recognition import assess_navigation_container
from services.api.app.application.web_readiness_contracts import (
    DEFAULT_WEB_READINESS_PROFILE,
    WebReadinessProfile,
)
from services.api.app.domain.exploration.web_run_policy_snapshot import (
    WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_BYTES,
)
from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
)


@dataclass(frozen=True, slots=True)
class _DeferredNavigationCapture:
    """等待可信 Capture authority 放行的原始导航观察。"""

    dom: str
    base_url: str
    snapshot: Mapping[str, object]
    source_epoch: int
    parent: WebNavigationCandidate | None
    parent_entry_id: str | None
    web_capture_stage: str | None
    candidates: tuple[WebNavigationCandidate, ...]
    capture_id: str | None = None


class WebDesktopCapturePolicyBrowser:
    """只包装 Web V2 Browser，绝不改变 H5/App 的采集默认值。"""

    def __init__(
        self,
        *,
        browser: object,
        readiness_profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE,
        identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY,
        include_blocked_navigation: bool = False,
    ) -> None:
        self._browser = browser
        self._readiness_profile = readiness_profile
        self._identity_policy = identity_policy
        self._include_blocked_navigation = include_blocked_navigation
        self._navigation_by_session: dict[str, tuple[WebNavigationCandidate, ...]] = {}
        self._latest_navigation_identities: dict[str, tuple[tuple[object, ...], ...]] = {}
        self._latest_navigation_page_url: dict[str, str] = {}
        self._navigation_discovery_capture_ids: dict[str, dict[tuple[object, ...], str]] = {}
        self._navigation_action_capture_ids: dict[str, dict[tuple[object, ...], str]] = {}
        self._navigation_epoch_by_session: dict[str, int] = {}
        self._navigation_parent_by_session: dict[str, tuple[WebNavigationCandidate, str]] = {}
        self._navigation_container_registry_by_session: dict[
            str, WebNavigationContainerRegistry
        ] = {}
        self._navigation_recovery_error_by_session: dict[str, str] = {}
        self._defer_navigation_discovery = False
        self._deferred_navigation_by_session: dict[str, _DeferredNavigationCapture] = {}
        self._latest_bound_capture_id_by_session: dict[str, str] = {}

    def defer_navigation_discovery_until_authorized(self) -> None:
        """生产组合先验签持久 Capture，再允许解析 DOM 导航。"""
        self._defer_navigation_discovery = True

    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        enforced = replace(
            _enforce_web_desktop_request(request), web_readiness_profile=self._readiness_profile
        )
        result = await self._browser.capture(enforced)
        return self._apply_result_policy(enforced, result)

    async def capture_with_timeout(
        self,
        request: PageCaptureRequest,
        *,
        timeout_seconds: float,
    ) -> PageCaptureResult:
        enforced = replace(
            _enforce_web_desktop_request(request), web_readiness_profile=self._readiness_profile
        )
        capture_with_timeout = getattr(self._browser, "capture_with_timeout", None)
        if callable(capture_with_timeout):
            result = await capture_with_timeout(
                enforced,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = await asyncio.wait_for(
                self._browser.capture(enforced),
                timeout=timeout_seconds,
            )
        return self._apply_result_policy(enforced, result)

    async def close_session(self, session_id: str) -> None:
        self._navigation_by_session.pop(session_id, None)
        self._latest_navigation_identities.pop(session_id, None)
        self._latest_navigation_page_url.pop(session_id, None)
        self._navigation_discovery_capture_ids.pop(session_id, None)
        self._navigation_action_capture_ids.pop(session_id, None)
        self._navigation_epoch_by_session.pop(session_id, None)
        self._navigation_parent_by_session.pop(session_id, None)
        self._navigation_container_registry_by_session.pop(session_id, None)
        self._navigation_recovery_error_by_session.pop(session_id, None)
        self._deferred_navigation_by_session.pop(session_id, None)
        self._latest_bound_capture_id_by_session.pop(session_id, None)
        await self._browser.close_session(session_id)

    def set_navigation_parent(
        self,
        session_id: str,
        candidate: WebNavigationCandidate,
        *,
        parent_menu_entry_id: str,
    ) -> None:
        """下一批 Capture 在密封前继承当前菜单动作父链，连续可见控件保持原父链。"""
        self._navigation_parent_by_session[session_id] = (candidate, parent_menu_entry_id)

    def restore_navigation_state(
        self,
        session_id: str,
        *,
        candidates: tuple[WebNavigationCandidate, ...],
        parent_candidate: WebNavigationCandidate,
        parent_menu_entry_id: str,
    ) -> str | None:
        """在恢复后的首次 Capture 前注入持久父链与匿名容器种子。"""
        identities = {item.logical_target_identity for item in candidates}
        rows = [
            {
                **candidate.to_dict(),
                "menu_entry_id": "discovery:" + candidate.logical_target_identity,
                "source_kind": "runtime_discovered",
            }
            for candidate in candidates
        ]
        reason = None
        if (
            not candidates
            or parent_candidate.logical_target_identity not in identities
            or parent_menu_entry_id != "discovery:" + parent_candidate.logical_target_identity
            or not persisted_menu_lineages_are_valid(rows)
        ):
            reason = "WEB_MENU_LINEAGE_UNAVAILABLE"
        registry = WebNavigationContainerRegistry()
        if reason is None:
            for candidate in candidates:
                seed = candidate.navigation_container_restore_seed
                if (
                    not isinstance(seed, Mapping)
                    or seed.get("identity") != candidate.navigation_container_id
                ):
                    reason = "WEB_MENU_LINEAGE_UNAVAILABLE"
                    break
                reason = registry.restore(seed)
                if reason is not None:
                    break
        if reason is not None:
            self._navigation_recovery_error_by_session[session_id] = reason
            self._navigation_by_session[session_id] = tuple(
                replace(
                    candidate,
                    actionability="blocked",
                    identity_review_reason=reason,
                )
                for candidate in candidates
            )
            return reason
        self._navigation_recovery_error_by_session.pop(session_id, None)
        self._navigation_container_registry_by_session[session_id] = registry
        self._navigation_by_session[session_id] = tuple(candidates)
        self._navigation_parent_by_session[session_id] = (
            parent_candidate,
            parent_menu_entry_id,
        )
        return None

    def reject_navigation_restore(self, session_id: str, *, reason: str) -> None:
        """无法唯一冷恢复时，阻断该 Session 后续全部导航候选。"""
        if reason not in {
            "WEB_MENU_LINEAGE_UNAVAILABLE",
            "WEB_MENU_LINEAGE_AMBIGUOUS",
        }:
            raise ValueError("Web 菜单恢复原因不受支持")
        self._navigation_recovery_error_by_session[session_id] = reason

    def navigation_candidates(
        self,
        session_id: str,
    ) -> tuple[WebNavigationCandidate, ...]:
        discovery_ids = self._navigation_discovery_capture_ids.get(session_id, {})
        action_ids = self._navigation_action_capture_ids.get(session_id, {})
        projected: list[WebNavigationCandidate] = []
        for candidate in self._navigation_by_session.get(session_id, ()):
            identity = _navigation_identity(
                candidate,
                identity_policy=self._identity_policy,
                preserve_unresolved_sources=self._include_blocked_navigation,
            )
            source_capture_id = action_ids.get(identity)
            bound = (
                bind_navigation_candidate_source(
                    candidate,
                    source_capture_id=source_capture_id,
                    source_epoch=candidate.source_epoch,
                )
                if source_capture_id is not None
                else candidate
            )
            projected.append(
                replace(
                    bound,
                    discovery_capture_id=discovery_ids.get(identity),
                )
            )
        return tuple(projected)

    def authorized_navigation_candidates(
        self,
        session_id: str,
        *,
        capture_id: str,
        page_role: str,
    ) -> tuple[WebNavigationCandidate, ...]:
        """只为已由上层 authority 放行且与观察绑定一致的 Capture 物化。"""
        self._materialize_deferred_navigation(
            session_id,
            authorized_capture_id=capture_id,
            authorized_page_role=page_role,
        )
        return self.navigation_candidates(session_id)

    def bind_latest_navigation_capture(
        self,
        session_id: str,
        capture_id: str,
    ) -> None:
        """保留首次发现事实，同时把可执行来源刷新为最新会话 Capture。"""

        if not isinstance(capture_id, str) or not capture_id.strip():
            raise ValueError("capture_id 不能为空")
        bound_capture_id = capture_id.strip()
        self._latest_bound_capture_id_by_session[session_id] = bound_capture_id
        deferred = self._deferred_navigation_by_session.get(session_id)
        if deferred is not None and deferred.capture_id is None:
            self._deferred_navigation_by_session[session_id] = replace(
                deferred,
                capture_id=bound_capture_id,
            )
        discovery_bindings = self._navigation_discovery_capture_ids.setdefault(session_id, {})
        action_bindings = self._navigation_action_capture_ids.setdefault(session_id, {})
        for identity in self._latest_navigation_identities.get(session_id, ()):
            discovery_bindings.setdefault(identity, capture_id.strip())
            action_bindings[identity] = capture_id.strip()

    def _apply_result_policy(
        self,
        request: PageCaptureRequest,
        result: PageCaptureResult,
    ) -> PageCaptureResult:
        if (result.final_url or "").partition("#")[0].casefold() == "about:blank":
            # 空白首帧只保留动作审计；浏览器适配器意外返回的截图也不能落库。
            result = replace(
                result,
                artifacts=[item for item in result.artifacts if item.artifact_type != "screenshot"],
            )
        dom = next(
            (
                str(artifact.content)
                for artifact in result.artifacts
                if artifact.artifact_type == "dom" and isinstance(artifact.content, str)
            ),
            "",
        )
        base_url = result.final_url or request.url
        snapshot = next(
            (
                artifact.content
                for artifact in result.artifacts
                if artifact.artifact_type == "ui_snapshot" and isinstance(artifact.content, dict)
            ),
            {},
        )
        bootstrap_blank_surface = (
            request.bootstrap_blank_surface and base_url is None
        ) or (base_url or "").partition("#")[0].casefold() == "about:blank"
        source_epoch = 0
        if request.session_id is not None:
            source_epoch = self._navigation_epoch_by_session.get(request.session_id, 0) + 1
            self._navigation_epoch_by_session[request.session_id] = source_epoch
        parent, parent_entry_id = self._navigation_parent_by_session.get(
            request.session_id, (None, None)
        )
        container_registry = (
            self._navigation_container_registry_by_session.setdefault(
                request.session_id, WebNavigationContainerRegistry()
            )
            if request.session_id is not None
            else WebNavigationContainerRegistry()
        )
        deferred_navigation = bool(
            self._defer_navigation_discovery
            and request.session_id is not None
            and dom
            and isinstance(base_url, str)
            and not bootstrap_blank_surface
        )
        deferred_observation = self._deferred_navigation_by_session.get(request.session_id)
        preserve_deferred_navigation = bool(
            self._defer_navigation_discovery
            and request.session_id is not None
            and not bootstrap_blank_surface
            and request.web_capture_stage
            in {
                "web_full_page_supplemental",
                "web_primary_content_evidence",
            }
            and deferred_observation is not None
            and _canonical_page_url(
                deferred_observation.base_url,
                identity_policy=self._identity_policy,
            )
            == _canonical_page_url(base_url, identity_policy=self._identity_policy)
        )
        if (
            self._defer_navigation_discovery
            and request.session_id is not None
            and not preserve_deferred_navigation
        ):
            # 新 Capture 无论是否包含 DOM，都使上一观察失效，避免旧页跨帧重放。
            # 长图和正文补图是同一 ready 页面上的补充证据，不得清空尚待授权的顶部 href。
            self._deferred_navigation_by_session.pop(request.session_id, None)
        candidates = (
            extract_navigation_candidates(
                dom,
                base_url=base_url,
                source_epoch=source_epoch,
                include_blocked=self._include_blocked_navigation,
                snapshot=snapshot,
                parent_candidate=parent,
                parent_menu_entry_id=parent_entry_id,
                container_registry=container_registry,
                require_runtime_hit_test=True,
            )
            if (
                dom
                and isinstance(base_url, str)
                and not bootstrap_blank_surface
            )
            else ()
        )
        candidates = reconcile_navigation_lineages(
            candidates,
            history=self._navigation_by_session.get(request.session_id, ()),
            parent_candidate=parent,
        )
        recovery_error = self._navigation_recovery_error_by_session.get(request.session_id)
        if recovery_error is not None:
            candidates = tuple(
                replace(
                    candidate,
                    actionability="blocked",
                    identity_review_reason=recovery_error,
                )
                for candidate in candidates
            )
        candidates = tuple(
            isolate_navigation_candidate_identity(
                candidate,
                identity_policy=self._identity_policy,
            )
            for candidate in candidates
        )
        if deferred_navigation and not preserve_deferred_navigation:
            self._deferred_navigation_by_session[request.session_id] = _DeferredNavigationCapture(
                dom=dom,
                base_url=base_url,
                snapshot=snapshot,
                source_epoch=source_epoch,
                parent=parent,
                parent_entry_id=parent_entry_id,
                web_capture_stage=request.web_capture_stage,
                candidates=candidates,
            )
        # 延迟的是执行授权，不是同帧 DOM 与命中证据构成的分类事实。
        navigation_assessment = assess_navigation_container(
            navigation_snapshot_from_html(
                dom,
                snapshot=snapshot,
                base_url=base_url or "",
                container_registry=container_registry,
                require_runtime_hit_test=True,
            )
        )
        if request.session_id is not None and not deferred_navigation:
            latest_identities = tuple(
                _navigation_identity(
                    candidate,
                    identity_policy=self._identity_policy,
                    preserve_unresolved_sources=self._include_blocked_navigation,
                )
                for candidate in candidates
            )
            current_page_url = _canonical_page_url(
                base_url,
                identity_policy=self._identity_policy,
            )
            previous_page_url = self._latest_navigation_page_url.get(request.session_id)
            # 主体补图会滚动到正文区域，当前截图里可以没有导航；它仍是同一
            # 权威页面的新 Capture，必须沿用上一帧导航身份刷新动作来源。
            preserve_primary_identities = bool(
                not latest_identities
                and request.web_capture_stage == "web_primary_content_evidence"
                and current_page_url
                and current_page_url == previous_page_url
            )
            if not preserve_primary_identities:
                self._latest_navigation_identities[request.session_id] = latest_identities
                if current_page_url:
                    self._latest_navigation_page_url[request.session_id] = current_page_url
            self._navigation_by_session[request.session_id] = _merge_navigation_candidates(
                self._navigation_by_session.get(request.session_id, ()),
                candidates,
                identity_policy=self._identity_policy,
                preserve_unresolved_sources=self._include_blocked_navigation,
            )
        artifacts = [
            _apply_artifact_policy(
                artifact,
                candidates=candidates,
                web_capture_stage=request.web_capture_stage,
                identity_policy=self._identity_policy,
                base_url=base_url,
                navigation_assessment=navigation_assessment,
            )
            for artifact in result.artifacts
        ]
        artifacts = _synchronize_page_coverage_manifests(artifacts)
        return replace(result, artifacts=artifacts)

    def _materialize_deferred_navigation(
        self,
        session_id: str,
        *,
        authorized_capture_id: str,
        authorized_page_role: str,
    ) -> None:
        observation = self._deferred_navigation_by_session.pop(session_id, None)
        if observation is None:
            return
        if (
            observation.web_capture_stage != WebCaptureStage.PAGE_READY_BASELINE.value
            or authorized_page_role not in {"home", "menu", "detail"}
            or observation.capture_id != authorized_capture_id
            or self._latest_bound_capture_id_by_session.get(session_id) != authorized_capture_id
        ):
            return
        candidates = observation.candidates
        latest_identities = tuple(
            _navigation_identity(
                candidate,
                identity_policy=self._identity_policy,
                preserve_unresolved_sources=self._include_blocked_navigation,
            )
            for candidate in candidates
        )
        current_page_url = _canonical_page_url(
            observation.base_url,
            identity_policy=self._identity_policy,
        )
        previous_page_url = self._latest_navigation_page_url.get(session_id)
        preserve_primary_identities = bool(
            not latest_identities
            and observation.web_capture_stage == "web_primary_content_evidence"
            and current_page_url
            and current_page_url == previous_page_url
        )
        if not preserve_primary_identities:
            self._latest_navigation_identities[session_id] = latest_identities
            if current_page_url:
                self._latest_navigation_page_url[session_id] = current_page_url
        self._navigation_by_session[session_id] = _merge_navigation_candidates(
            self._navigation_by_session.get(session_id, ()),
            candidates,
            identity_policy=self._identity_policy,
            preserve_unresolved_sources=self._include_blocked_navigation,
        )
        capture_id = self._latest_bound_capture_id_by_session.get(session_id)
        if capture_id is not None:
            discovery = self._navigation_discovery_capture_ids.setdefault(session_id, {})
            actions = self._navigation_action_capture_ids.setdefault(session_id, {})
            for identity in latest_identities:
                discovery.setdefault(identity, capture_id)
                actions[identity] = capture_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


class WebScreenshotContractError(RuntimeError):
    """Web V2 截图不是严格的 1920×1080 PNG。"""

    error_code = "WEB_SCREENSHOT_CONTRACT_INVALID"


def _enforce_web_desktop_request(request: PageCaptureRequest) -> PageCaptureRequest:
    # 广告直开只观察一次隔离页面，不能让通用清障链再次播放原 outbound 动作。
    if request.trigger_action == "direct_ad_outbound" or request.web_capture_stage in {
        "web_entry_probe",
        "web_page_probe",
        "web_page_ready_baseline",
    }:
        clear_obstructions = False
    else:
        clear_obstructions = request.clear_obstructions or request.mode in {
            PageCaptureMode.GOTO,
            PageCaptureMode.SINGLE_TRIGGER,
        }
    return replace(
        request,
        full_page=False,
        capture_screenshot=(
            request.capture_screenshot
            and not request.bootstrap_blank_surface
            and (request.url or "").partition("#")[0].casefold() != "about:blank"
        ),
        clear_obstructions=clear_obstructions,
        startup_policy="direct_home",
    )


def _apply_artifact_policy(
    artifact: PageCaptureArtifact,
    *,
    candidates: tuple[WebNavigationCandidate, ...],
    web_capture_stage: str | None,
    identity_policy=DEFAULT_WEB_URL_IDENTITY_POLICY,
    base_url: str | None = None,
    navigation_assessment=None,
) -> PageCaptureArtifact:
    if artifact.artifact_type == "screenshot":
        if (
            isinstance(artifact.content, dict)
            and artifact.content.get("purpose") == "web_scrolled_viewport"
        ):
            return replace(
                artifact,
                content=_preserve_screenshot_content(artifact.content),
            )
        if (
            web_capture_stage == "web_full_page_supplemental"
            and isinstance(artifact.content, dict)
            and artifact.content.get("purpose") == "web_direct_full_page"
        ):
            return replace(
                artifact,
                content=_preserve_direct_full_page_screenshot_content(artifact.content),
            )
        return replace(
            artifact,
            content=_compress_screenshot_content(artifact.content),
        )
    if artifact.artifact_type == "ui_snapshot" and isinstance(artifact.content, dict):
        content = dict(artifact.content)
        reviews = []
        elements = content.get("elements")
        for element in elements if isinstance(elements, list) else ():
            if not isinstance(element, dict) or not isinstance(element.get("href"), str):
                continue
            try:
                identity_policy.canonical(urljoin(base_url or "", element["href"]))
            except ValueError as exc:
                if str(exc) == "WEB_POLICY_VERSION_UNSUPPORTED":
                    raise
                reviews.append(
                    {
                        "href": element["href"],
                        "selector": element.get("selector"),
                        "reason_code": (
                            "WEB_URL_HASH_NEEDS_REVIEW"
                            if str(exc) == "WEB_URL_HASH_NEEDS_REVIEW"
                            else "WEB_URL_IDENTITY_INVALID"
                        ),
                        # Writer 分配 ID 前，用产物所属 Capture 表达不可变来源。
                        "source": "containing_capture",
                    }
                )
        if reviews:
            content["web_url_identity_reviews"] = reviews
        if web_capture_stage is not None:
            content["web_capture_stage"] = web_capture_stage
        content["web_navigation"] = {
            "schema_version": "web-navigation.v1",
            "source_selector": WEB_NAVIGATION_SOURCE_SELECTOR,
            "candidates": [candidate.to_dict() for candidate in candidates],
            "structure_facts": navigation_assessment.facts.to_dict()
            if navigation_assessment
            else {},
            "proven_container_ids": list(navigation_assessment.proven_container_ids)
            if navigation_assessment
            else [],
            "reason_codes": list(navigation_assessment.reason_codes)
            if navigation_assessment
            else [],
        }
        return replace(artifact, content=content)
    return artifact


def _compress_screenshot_content(content: object) -> object:
    if not isinstance(content, dict):
        raise WebScreenshotContractError("Web 截图内容必须为对象")
    if content.get("sequence") is True:
        frames = content.get("frames")
        if not isinstance(frames, list) or not frames:
            raise WebScreenshotContractError("Web 截图序列必须包含 frames")
        if _contains_encoded_image_payload(
            {key: value for key, value in content.items() if key != "frames"}
        ):
            raise WebScreenshotContractError("Web 截图序列 PNG 只能位于 frames")
        indexes: list[int] = []
        compressed_frames: list[dict[str, object]] = []
        for frame in frames:
            if not isinstance(frame, dict):
                raise WebScreenshotContractError("Web 截图序列帧必须为对象")
            index = frame.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise WebScreenshotContractError("Web 截图序列帧 index 无效")
            indexes.append(index)
            compressed_frame, image_count = _compress_screenshot_node(frame)
            if image_count != 1:
                raise WebScreenshotContractError("Web 每个序列帧必须恰好包含一张 PNG")
            compressed_frames.append(compressed_frame)
        if len(indexes) != len(set(indexes)):
            raise WebScreenshotContractError("Web 截图序列帧 index 重复")
        return {
            **content,
            "schema_version": "screenshot-sequence.v1",
            "frames": compressed_frames,
        }
    compressed, image_count = _compress_screenshot_node(content)
    if image_count != 1:
        raise WebScreenshotContractError("Web 截图产物必须恰好包含一张 PNG")
    return compressed


def _synchronize_page_coverage_manifests(
    artifacts: list[PageCaptureArtifact],
) -> list[PageCaptureArtifact]:
    """仅同步仍会经过无损重编码的旧分段截图清单。"""

    screenshots: dict[str, list[tuple[int | None, bytes]]] = {}
    for artifact in artifacts:
        if artifact.artifact_type != "screenshot" or not isinstance(artifact.content, Mapping):
            continue
        purpose = artifact.content.get("purpose")
        encoded = artifact.content.get("data")
        if not isinstance(purpose, str) or not isinstance(encoded, str):
            continue
        try:
            payload = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        segment_index = artifact.content.get("segment_index")
        screenshots.setdefault(purpose, []).append(
            (
                segment_index
                if isinstance(segment_index, int) and not isinstance(segment_index, bool)
                else None,
                payload,
            )
        )

    synchronized: list[PageCaptureArtifact] = []
    for artifact in artifacts:
        if artifact.artifact_type != "page_coverage" or not isinstance(
            artifact.content, Mapping
        ):
            synchronized.append(artifact)
            continue
        content = dict(artifact.content)
        protocol = content.get("protocol_version")
        if protocol == "web-full-page-segment-manifest.v1":
            payloads = screenshots.get("web_full_page_segment", [])
            by_index = {index: payload for index, payload in payloads if index is not None}
            segments = content.get("segments")
            if isinstance(segments, list) and len(by_index) == len(segments):
                updated_segments: list[object] = []
                for entry in segments:
                    if not isinstance(entry, Mapping):
                        updated_segments.append(entry)
                        continue
                    updated = dict(entry)
                    payload = by_index.get(updated.get("index"))
                    if payload is not None:
                        updated.update(_coverage_payload_digest(payload))
                    updated_segments.append(updated)
                content["segments"] = updated_segments
        synchronized.append(replace(artifact, content=content))
    return synchronized


def _coverage_payload_digest(payload: bytes) -> dict[str, object]:
    return {
        "size_bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def _contains_encoded_image_payload(value: object) -> bool:
    if isinstance(value, dict):
        if "data" in value or "encoding" in value:
            return True
        return any(_contains_encoded_image_payload(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_encoded_image_payload(item) for item in value)
    return False


def _compress_screenshot_node(content: dict[str, object]) -> tuple[dict[str, object], int]:
    copied = dict(content)
    encoded = copied.get("data")
    if "data" in copied or "encoding" in copied:
        if copied.get("encoding") != "base64" or not isinstance(encoded, str):
            raise WebScreenshotContractError("Web 截图必须使用 base64 编码")
        try:
            original = base64.b64decode(encoded, validate=True)
            compressed, size = _compress_png_without_resize(original)
        except (ValueError, OSError) as exc:
            raise WebScreenshotContractError(str(exc)) from exc
        if size != (1920, 1080):
            raise WebScreenshotContractError(
                f"Web 截图尺寸必须为 1920×1080，实际为 {size[0]}×{size[1]}"
            )
        copied["data"] = base64.b64encode(compressed).decode("ascii")
        copied["compression"] = "png_lossless_optimize"
        copied["pixel_width"], copied["pixel_height"] = size
        copied["original_size_bytes"] = len(original)
        copied["compressed_size_bytes"] = len(compressed)
        return copied, 1
    image_count = 0
    for key, value in tuple(copied.items()):
        if isinstance(value, dict):
            copied[key], child_count = _compress_screenshot_node(value)
            image_count += child_count
        elif isinstance(value, list):
            values: list[object] = []
            for item in value:
                if isinstance(item, dict):
                    compressed_item, child_count = _compress_screenshot_node(item)
                    values.append(compressed_item)
                    image_count += child_count
                else:
                    values.append(item)
            copied[key] = values
    return copied, image_count


def _preserve_screenshot_content(content: dict[str, object]) -> dict[str, object]:
    """校验滚动补图，但保留 Playwright 返回的原始 PNG 字节。"""

    encoded = content.get("data")
    if content.get("encoding") != "base64" or not isinstance(encoded, str):
        raise WebScreenshotContractError("Web 截图必须使用 base64 编码")
    try:
        original = base64.b64decode(encoded, validate=True)
        size = _inspect_png_size(original)
    except (ValueError, OSError) as exc:
        raise WebScreenshotContractError(str(exc)) from exc
    if size != (1920, 1080):
        raise WebScreenshotContractError(
            f"Web 截图尺寸必须为 1920×1080，实际为 {size[0]}×{size[1]}"
        )
    return dict(content)


def _preserve_direct_full_page_screenshot_content(
    content: dict[str, object],
) -> dict[str, object]:
    """按长图合同校验原始 PNG，不压缩或改写整页像素。"""

    encoded = content.get("data")
    if (
        content.get("full_page") is not True
        or content.get("encoding") != "base64"
        or not isinstance(encoded, str)
        or len(encoded) > 4 * ((WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_BYTES + 2) // 3)
    ):
        raise WebScreenshotContractError("Web 整页截图编码或体积无效")
    try:
        validate_web_full_page_supplemental_png(base64.b64decode(encoded, validate=True))
    except (RuntimeError, ValueError) as exc:
        raise WebScreenshotContractError(str(exc)) from exc
    return dict(content)


def _inspect_png_size(png: bytes) -> tuple[int, int]:
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("截图不是有效 PNG")
    with Image.open(BytesIO(png)) as image:
        image.load()
        return image.size


def _compress_png_without_resize(png: bytes) -> tuple[bytes, tuple[int, int]]:
    size = _inspect_png_size(png)
    with Image.open(BytesIO(png)) as image:
        output = BytesIO()
        image.save(output, format="PNG", optimize=True, compress_level=9)
    compressed = output.getvalue()
    return (compressed if len(compressed) < len(png) else png), size


def _merge_navigation_candidates(
    existing: tuple[WebNavigationCandidate, ...],
    discovered: tuple[WebNavigationCandidate, ...],
    *,
    identity_policy=DEFAULT_WEB_URL_IDENTITY_POLICY,
    preserve_unresolved_sources=False,
) -> tuple[WebNavigationCandidate, ...]:
    """按目标与完整菜单路径累计；重复发现刷新到最新 epoch。"""

    merged = list(existing)
    positions = {
        _navigation_identity(
            item,
            identity_policy=identity_policy,
            preserve_unresolved_sources=preserve_unresolved_sources,
        ): index
        for index, item in enumerate(existing)
    }
    for candidate in discovered:
        identity = _navigation_identity(
            candidate,
            identity_policy=identity_policy,
            preserve_unresolved_sources=preserve_unresolved_sources,
        )
        position = positions.get(identity)
        if position is not None:
            merged[position] = candidate
        else:
            positions[identity] = len(merged)
            merged.append(candidate)
    return tuple(merged)


def _navigation_identity(
    candidate: WebNavigationCandidate,
    *,
    identity_policy=DEFAULT_WEB_URL_IDENTITY_POLICY,
    preserve_unresolved_sources=False,
) -> tuple[object, ...]:
    return (candidate.logical_target_identity,)


def _canonical_page_url(
    value: object,
    *,
    identity_policy: WebUrlIdentityPolicy,
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return identity_policy.canonical(value)
    except ValueError:
        return None


__all__ = [
    "WebDesktopCapturePolicyBrowser",
    "WebScreenshotContractError",
]
