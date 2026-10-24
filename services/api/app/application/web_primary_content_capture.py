"""Web 非广告主体目标选择与有界滚动补证。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.web_article_body_structure import is_web_article_body_node
from services.api.app.application.web_interaction_facts import (
    web_capture_content_facts,
    web_command_accepted,
    web_incomplete_interaction,
    web_receipt_allows_arrival,
    web_single_action_plan,
    web_target_exposed,
)
from services.api.app.application.web_interaction_precision import (
    WebInteractionEvidenceMode,
    WebInteractionOutcome,
    WebInteractionPostcondition,
    assess_web_interaction_outcome,
)
from services.api.app.application.web_logical_page import (
    canonical_web_logical_page_url,
    web_logical_page_id,
)
from services.api.app.application.web_obstruction_geometry import (
    WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP,
    web_is_ordinary_overlay,
    web_ordinary_overlay_subject_overlap,
)
from services.api.app.application.web_page_readiness import WebObstructionAssessmentPort
from services.api.app.application.web_primary_content_exposure import web_primary_content_exposed
from services.api.app.application.web_viewport_geometry import viewport_rectangles_coverage
from services.api.app.domain.exploration import build_web_obstruction_candidates
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)

WEB_PRIMARY_CONTENT_CAPTURE_PURPOSE = "web_primary_content_evidence"
WEB_PRIMARY_CONTENT_MAX_SCROLLS = 2
WEB_PRIMARY_CONTENT_MAX_AD_COVERAGE = 0.35
WEB_PRIMARY_CONTENT_ORDINARY_OVERLAY_REVIEW_OVERLAP = WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP
_WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED = "WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED"


@dataclass(frozen=True, kw_only=True)
class WebPrimaryContentTarget:
    candidate_id: str
    dom_fingerprint: str
    selector: str
    expected_page_type: str
    viewport_ad_coverage: float | None
    target_score: float


@dataclass(frozen=True, kw_only=True)
class WebPrimaryContentCaptureResult:
    status: str
    parent_capture_id: str
    evidence_capture_id: str | None
    logical_page_id: str
    target_candidate_id: str | None
    scroll_count: int
    before_ad_coverage: float | None
    after_ad_coverage: float | None
    reason_code: str | None = None
    interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()
    attempts: tuple[dict[str, object], ...] = ()
    reused: bool = False
    original_scroll_count: int | None = None
    diagnostic_type: str | None = None


class WebPrimaryContentEvidenceSource(Protocol):
    async def read_ready(self, *, capture_id: str, page_role: str) -> object: ...

    async def read_snapshot(self, *, capture_id: str) -> Mapping[str, object]: ...

    async def read_url(self, *, capture_id: str) -> str: ...


class WebPrimaryContentMetadataWriter(Protocol):
    async def mark_primary_content_capture(self, **kwargs: object) -> None: ...


class WebPrimaryContentCaptureService:
    """只对密封主体候选滚动，最多两次，并保留首屏 Capture 为父证据。"""

    def __init__(
        self,
        *,
        coordinator: object,
        evidence_source: WebPrimaryContentEvidenceSource,
        metadata_writer: WebPrimaryContentMetadataWriter | None = None,
        obstruction_assessor: WebObstructionAssessmentPort | None = None,
        identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY,
    ) -> None:
        self._coordinator = coordinator
        self._evidence_source = evidence_source
        self._metadata_writer = metadata_writer
        self._obstruction_assessor = obstruction_assessor
        self._identity_policy = identity_policy

    async def capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        page_url: str,
        page_type: str,
        deadline: datetime,
    ) -> WebPrimaryContentCaptureResult:
        outcomes: list[WebInteractionOutcome] = []
        attempts: list[dict[str, object]] = []
        try:
            result = await self._capture(
                interaction_outcomes=outcomes,
                attempts=attempts,
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=session_id,
                capture_id=capture_id,
                page_url=page_url,
                page_type=page_type,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 - 保留已发生动作，不把补图异常吞成无解释 None
            result = WebPrimaryContentCaptureResult(
                status="failed",
                parent_capture_id=capture_id,
                evidence_capture_id=None,
                logical_page_id=web_logical_page_id(
                    page_url, identity_policy=self._identity_policy
                ),
                target_candidate_id=None,
                scroll_count=len(attempts),
                before_ad_coverage=None,
                after_ad_coverage=None,
                reason_code=exc.error_code
                if isinstance(exc, CaptureCoordinationError)
                else "WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
                diagnostic_type=type(exc).__name__,
            )
        return replace(
            result,
            interaction_outcomes=tuple(outcomes),
            attempts=tuple(attempts),
            scroll_count=sum(outcome.command_accepted for outcome in outcomes),
        )

    async def _capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        page_url: str,
        page_type: str,
        deadline: datetime,
        interaction_outcomes: list[WebInteractionOutcome],
        attempts: list[dict[str, object]],
    ) -> WebPrimaryContentCaptureResult:
        canonical_url = canonical_web_logical_page_url(
            page_url, identity_policy=self._identity_policy
        )
        logical_page_id = web_logical_page_id(canonical_url, identity_policy=self._identity_policy)
        parent_capture_id = capture_id
        current_capture_id = capture_id
        before_snapshot = await self._evidence_source.read_snapshot(capture_id=capture_id)
        before_ad_coverage = viewport_ad_coverage(before_snapshot)
        after_ad_coverage: float | None = None
        selected_candidate_id: str | None = None

        initial_targets = build_web_primary_content_targets(
            capture_id=capture_id,
            ui_snapshot=before_snapshot,
            expected_page_type=page_type,
        )
        reuse_home_baseline = (
            page_type == "home"
            and initial_targets
            and before_ad_coverage is not None
            and before_ad_coverage <= WEB_PRIMARY_CONTENT_MAX_AD_COVERAGE
            and _post_scroll_block_reason(before_snapshot) is None
            and web_target_exposed(before_snapshot, selector=initial_targets[0].selector)
        )
        reuse_content_baseline = (
            page_type in {"content_list", "content_detail"}
            and initial_targets
            and before_ad_coverage is not None
            and before_ad_coverage <= WEB_PRIMARY_CONTENT_MAX_AD_COVERAGE
            and _post_scroll_block_reason(before_snapshot) is None
            and web_primary_content_exposed(
                before_snapshot, selector=initial_targets[0].selector, page_type=page_type
            )
            and await self._current_content_baseline_is_trusted(
                capture_id=capture_id, canonical_url=canonical_url, page_type=page_type,
            )
        )
        if reuse_home_baseline or reuse_content_baseline:
            # 主体已经位于可信首屏时直接复用；无位移的 scrollIntoView 不能被误判为视口阻断。
            return WebPrimaryContentCaptureResult(
                status="captured",
                parent_capture_id=parent_capture_id,
                evidence_capture_id=parent_capture_id,
                logical_page_id=logical_page_id,
                target_candidate_id=initial_targets[0].candidate_id,
                scroll_count=0,
                before_ad_coverage=before_ad_coverage,
                after_ad_coverage=before_ad_coverage,
                reused=True,
                original_scroll_count=0,
            )

        for attempt in range(1, WEB_PRIMARY_CONTENT_MAX_SCROLLS + 1):
            action_source_capture_id = current_capture_id
            snapshot = await self._evidence_source.read_snapshot(
                capture_id=action_source_capture_id
            )
            targets = build_web_primary_content_targets(
                capture_id=current_capture_id,
                ui_snapshot=snapshot,
                expected_page_type=page_type,
            )
            if not targets:
                return WebPrimaryContentCaptureResult(
                    status="blocked",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=None,
                    scroll_count=attempt - 1,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=after_ad_coverage,
                    reason_code="WEB_PRIMARY_CONTENT_TARGET_MISSING",
                )
            target = targets[0]
            selected_candidate_id = target.candidate_id
            intent = _primary_content_intent(
                task_id=task_id,
                session_id=session_id,
                source_capture_id=action_source_capture_id,
                target=target,
                logical_page_id=logical_page_id,
                deadline=deadline,
                attempt=attempt,
            )
            coordinate = getattr(self._coordinator, "coordinate", None)
            if not callable(coordinate):
                raise TypeError("Web 主体补证 coordinator 必须提供 coordinate")
            try:
                result = await coordinate(
                    CaptureCoordinationCommand(agent_run_id=agent_run_id, intent=intent)
                )
            except CaptureCoordinationError as exc:
                attempts.append(
                    _primary_attempt(
                        exc, source_capture_id=action_source_capture_id, attempt=attempt
                    )
                )
                interaction_outcomes.append(
                    web_incomplete_interaction(
                        exc.interaction,
                        getattr(exc.after_capture, "capture_id", None),
                        "primary_capture_failed",
                    )
                )
                raise
            attempts.append(
                _primary_attempt(
                    result, source_capture_id=action_source_capture_id, attempt=attempt
                )
            )
            after_capture = getattr(result, "after_capture", None)
            after_capture_id = str(getattr(after_capture, "capture_id", "") or "")
            before_capture = getattr(result, "before_capture", None)
            before_id = getattr(before_capture, "capture_id", None)
            action_id = getattr(before_capture, "action_execution_id", None)
            parent_chain_valid = bool(
                before_id
                and action_id
                and before_id != action_source_capture_id
                and getattr(before_capture, "parent_capture_id", None) == action_source_capture_id
                and getattr(after_capture, "parent_capture_id", None) == before_id
                and getattr(after_capture, "action_execution_id", None) == action_id
            )
            interaction_outcomes.append(
                web_incomplete_interaction(
                    getattr(result, "interaction", None),
                    after_capture_id or None,
                    "after_observation_pending",
                )
            )
            if not after_capture_id:
                return WebPrimaryContentCaptureResult(
                    status="failed",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=None,
                    reason_code="WEB_PRIMARY_CONTENT_CAPTURE_MISSING",
                )
            after_url = await self._evidence_source.read_url(capture_id=after_capture_id)
            if (
                canonical_web_logical_page_url(after_url, identity_policy=self._identity_policy)
                != canonical_url
            ):
                return WebPrimaryContentCaptureResult(
                    status="blocked",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=None,
                    reason_code="WEB_PRIMARY_CONTENT_URL_CHANGED",
                )
            after_snapshot = await self._evidence_source.read_snapshot(capture_id=after_capture_id)
            ready = None
            ready_facts = None
            try:
                ready = await self._evidence_source.read_ready(
                    capture_id=after_capture_id,
                    page_role=(
                        "home"
                        if page_type == "home"
                        else "detail"
                        if page_type.endswith("_detail")
                        else "menu"
                    ),
                )
                ready_facts = ready.interaction_facts
            except (AttributeError, RuntimeError):
                # 不稳定、旧帧和缺少读取能力均保留动作事实，不升级可信补证。
                pass
            rebound_targets = build_web_primary_content_targets(
                capture_id=after_capture_id,
                ui_snapshot=after_snapshot,
                expected_page_type=page_type,
            )
            target_matches = any(
                item.candidate_id == target.candidate_id for item in rebound_targets
            )
            exposed = target_matches and web_primary_content_exposed(
                after_snapshot, selector=target.selector, page_type=page_type
            )
            after_ad_coverage = viewport_ad_coverage(after_snapshot)
            (
                block_reason,
                model_cleared_ordinary_overlay,
            ) = await self._post_scroll_obstruction_decision(
                capture_id=after_capture_id,
                ui_snapshot=after_snapshot,
                ready_capture=ready,
                page_role=(
                    "home"
                    if page_type == "home"
                    else "detail"
                    if page_type.endswith("_detail")
                    else "menu"
                ),
            )
            outcome = assess_web_interaction_outcome(
                plan=web_single_action_plan(intent, WebInteractionPostcondition.TARGET_VISIBLE),
                command_accepted=web_command_accepted(getattr(result, "interaction", None)),
                before_url=page_url,
                after_url=after_url,
                before_content_identity=web_capture_content_facts(
                    snapshot, url=page_url
                ).content_identity,
                after_content_identity=ready_facts.content_identity if ready_facts else None,
                effect_observed=(
                    after_ad_coverage != viewport_ad_coverage(snapshot)
                    or snapshot.get("viewport") != after_snapshot.get("viewport")
                    or any(
                        item.get("selector") == target.selector
                        and item.get("rect")
                        != next(
                            (
                                old.get("rect")
                                for old in snapshot.get("elements", [])
                                if old.get("selector") == target.selector
                            ),
                            None,
                        )
                        for item in after_snapshot.get("elements", [])
                        if isinstance(item, Mapping)
                    )
                ),
                postcondition_satisfied=exposed,
                after_capture_id=after_capture_id,
                readiness_accepted=bool(ready_facts and ready_facts.readiness_accepted),
                identity_verified=bool(
                    ready_facts
                    and ready_facts.identity_verified
                    and target_matches
                    and parent_chain_valid
                    and after_capture_id != action_source_capture_id
                    and web_receipt_allows_arrival(getattr(result, "interaction", None))
                ),
                business_layer_visible=bool(
                    ready_facts
                    and (ready_facts.business_layer_visible or model_cleared_ordinary_overlay)
                    and block_reason is None
                ),
                **({"evidence_mode": ready_facts.evidence_mode} if ready_facts else {}),
            )
            interaction_outcomes[-1] = outcome
            if block_reason is not None:
                return WebPrimaryContentCaptureResult(
                    status="blocked",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=viewport_ad_coverage(after_snapshot),
                    reason_code=block_reason,
                )
            after_ad_coverage = viewport_ad_coverage(after_snapshot)
            current_capture_id = after_capture_id
            if (
                not ready_facts
                or not ready_facts.readiness_accepted
                or not ready_facts.identity_verified
                or not parent_chain_valid
                or not web_receipt_allows_arrival(getattr(result, "interaction", None))
            ):
                return WebPrimaryContentCaptureResult(
                    status="blocked",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=after_ad_coverage,
                    reason_code="WEB_PRIMARY_CONTENT_AFTER_UNTRUSTED",
                )
            if after_ad_coverage is None:
                return WebPrimaryContentCaptureResult(
                    status="blocked",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=None,
                    reason_code="WEB_PRIMARY_CONTENT_GEOMETRY_UNKNOWN",
                )
            if not target_matches:
                return WebPrimaryContentCaptureResult(
                    status="blocked",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=None,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=after_ad_coverage,
                    reason_code="WEB_PRIMARY_CONTENT_TARGET_DRIFTED",
                )
            if (
                after_ad_coverage <= WEB_PRIMARY_CONTENT_MAX_AD_COVERAGE
                and outcome.transition_arrived
            ):
                if self._metadata_writer is not None:
                    await self._metadata_writer.mark_primary_content_capture(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        session_id=session_id,
                        capture_id=after_capture_id,
                        parent_capture_id=parent_capture_id,
                        direct_parent_capture_id=str(
                            getattr(getattr(result, "before_capture", None), "capture_id", "") or ""
                        ),
                        logical_page_id=logical_page_id,
                        capture_purpose=WEB_PRIMARY_CONTENT_CAPTURE_PURPOSE,
                        target_candidate_id=selected_candidate_id,
                        scroll_count=attempt,
                        before_ad_coverage=before_ad_coverage,
                        after_ad_coverage=after_ad_coverage,
                    )
                return WebPrimaryContentCaptureResult(
                    status="captured",
                    parent_capture_id=parent_capture_id,
                    evidence_capture_id=after_capture_id,
                    logical_page_id=logical_page_id,
                    target_candidate_id=selected_candidate_id,
                    scroll_count=attempt,
                    before_ad_coverage=before_ad_coverage,
                    after_ad_coverage=after_ad_coverage,
                )

        return WebPrimaryContentCaptureResult(
            status="blocked",
            parent_capture_id=parent_capture_id,
            evidence_capture_id=None,
            logical_page_id=logical_page_id,
            target_candidate_id=selected_candidate_id,
            scroll_count=WEB_PRIMARY_CONTENT_MAX_SCROLLS,
            before_ad_coverage=before_ad_coverage,
            after_ad_coverage=after_ad_coverage,
            reason_code="WEB_PRIMARY_CONTENT_VIEWPORT_BLOCKED",
        )

    async def _current_content_baseline_is_trusted(
        self, *, capture_id: str, canonical_url: str, page_type: str,
    ) -> bool:
        """复用当前自然内容帧；由既有 read_ready 复核 current、谱系与就绪准入。"""
        try:
            ready = await self._evidence_source.read_ready(
                capture_id=capture_id,
                page_role="detail" if page_type == "content_detail" else "menu",
            )
            facts = ready.interaction_facts
            return bool(
                ready.capture_id == capture_id
                and canonical_web_logical_page_url(
                    ready.url, identity_policy=self._identity_policy,
                ) == canonical_url
                and canonical_web_logical_page_url(
                    facts.url, identity_policy=self._identity_policy,
                ) == canonical_url
                and facts.evidence_mode is WebInteractionEvidenceMode.NATURAL
                and facts.readiness_accepted is True
                and facts.identity_verified is True
                and facts.business_layer_visible is True
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # 缺少当前可信帧不得复用，继续原有有界动作与严格后态校验。
            return False

    async def _post_scroll_obstruction_decision(
        self,
        *,
        capture_id: str,
        ui_snapshot: Mapping[str, object],
        ready_capture: object,
        page_role: str,
    ) -> tuple[str | None, bool]:
        """普通浮层达到硬阈值后复用现有模型端口；特殊安全门始终确定性阻断。"""

        deterministic_reason = _post_scroll_block_reason(ui_snapshot)
        if deterministic_reason != _WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED:
            return (
                deterministic_reason,
                deterministic_reason is None and _has_only_benign_ordinary_overlays(ui_snapshot),
            )
        if self._obstruction_assessor is None:
            return _WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED, False
        screenshot_artifact_id = str(getattr(ready_capture, "screenshot_artifact_id", "") or "")
        dom_artifact_id = str(getattr(ready_capture, "dom_artifact_id", "") or "")
        if not screenshot_artifact_id or not dom_artifact_id:
            return _WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED, False
        candidates = build_web_obstruction_candidates(
            capture_id=capture_id,
            ui_snapshot=ui_snapshot,
        )
        try:
            assessment = await self._obstruction_assessor.assess_obstruction(
                screenshot_artifact_id=screenshot_artifact_id,
                dom_artifact_id=dom_artifact_id,
                candidates=tuple(item.to_public_dict() for item in candidates),
                page_role=page_role,
            )
        except Exception:  # noqa: BLE001 - 模型不可用时保持 fail-closed
            return _WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED, False
        if assessment.human_verification_present:
            return "WEB_PRIMARY_CONTENT_HUMAN_VERIFICATION_BLOCKED", False
        if assessment.age_gate_present:
            return "WEB_PRIMARY_CONTENT_AGE_GATE_BLOCKED", False
        if assessment.obstruction_present:
            candidate_ids = {item.candidate_id for item in candidates}
            if assessment.dismiss_candidate_id not in candidate_ids:
                return _WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED, False
            return "WEB_PRIMARY_CONTENT_OBSTRUCTION_APPEARED", False
        return None, True


def build_web_primary_content_targets(
    *,
    capture_id: str,
    ui_snapshot: Mapping[str, object],
    expected_page_type: str,
) -> tuple[WebPrimaryContentTarget, ...]:
    """从可信 DOM 快照选择业务主体，排除广告、导航、页脚和分页。"""

    if expected_page_type not in {
        "home",
        "video_list",
        "video_detail",
        "content_list",
        "content_detail",
        "activity",
        "search_page",
    }:
        return ()
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return ()
    eligible: list[tuple[float, str, str, str]] = []
    list_groups: Counter[tuple[str, str]] = Counter()
    raw_candidates: list[tuple[Mapping[str, object], str, str, tuple[str, str]]] = []
    for raw in elements:
        if not isinstance(raw, Mapping) or not _is_primary_content_element(raw):
            continue
        selector = str(raw.get("selector") or "").strip()
        if not selector:
            continue
        fingerprint = _element_fingerprint(raw)
        group_name = str(
            raw.get("repeated_group_fingerprint")
            or raw.get("parent_dom_fingerprint")
            or raw.get("parent_selector")
            or ""
        ).strip()
        # 卡片内的链接与正文是不同种类节点，不能相互计数后冒充重复卡片。
        group = (group_name, str(raw.get("tag") or "").casefold())
        raw_candidates.append((raw, selector, fingerprint, group))
        if group_name:
            list_groups[group] += 1

    ad_coverage = viewport_ad_coverage(ui_snapshot)
    for raw, selector, fingerprint, group in raw_candidates:
        if expected_page_type == "home" or expected_page_type.endswith("_list"):
            repeated = bool(raw.get("is_repeated_card")) or (
                bool(group[0]) and list_groups[group] >= 2
            )
            if not repeated:
                continue
            score = 70.0 + min(float(list_groups.get(group, 0)), 10.0)
        elif expected_page_type == "content_detail":
            if not _is_content_detail_element(raw):
                continue
            score = 90.0
        elif expected_page_type in {"activity", "search_page"}:
            tag = str(raw.get("tag") or "").casefold()
            own_tokens = set(raw.get("class_tokens") or [])
            if expected_page_type == "activity":
                matched = bool(
                    own_tokens & {"activity", "campaign", "activity-landing", "campaign-landing"}
                )
            else:
                matched = (
                    raw.get("role") == "search"
                    or tag == "form"
                    or bool(own_tokens & {"search-results", "query-results"})
                )
            if not matched:
                continue
            score = 90.0
        else:
            if not _is_video_detail_element(raw):
                continue
            score = 95.0
        if raw.get("contains_primary_media") is True:
            score += 3.0
        candidate_id = (
            "web-primary-content:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "dom_fingerprint": fingerprint,
                        "selector": selector,
                        "expected_page_type": expected_page_type,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        eligible.append((score, candidate_id, fingerprint, selector))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    return tuple(
        WebPrimaryContentTarget(
            candidate_id=candidate_id,
            dom_fingerprint=fingerprint,
            selector=selector,
            expected_page_type=expected_page_type,
            viewport_ad_coverage=ad_coverage,
            target_score=score,
        )
        for score, candidate_id, fingerprint, selector in eligible
    )


def resolve_web_primary_content_selector(
    *,
    capture_id: str,
    ui_snapshot: Mapping[str, object],
    expected_page_type: str,
    candidate_id: str,
) -> tuple[str, WebPrimaryContentTarget] | None:
    """平台层从同一快照重建密封候选并取回 selector。"""

    targets = build_web_primary_content_targets(
        capture_id=capture_id,
        ui_snapshot=ui_snapshot,
        expected_page_type=expected_page_type,
    )
    selected = next((item for item in targets if item.candidate_id == candidate_id), None)
    if selected is None:
        return None
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return None
    for raw in elements:
        if (
            isinstance(raw, Mapping)
            and _is_primary_content_element(raw)
            and _element_fingerprint(raw) == selected.dom_fingerprint
            and str(raw.get("selector") or "").strip() == selected.selector
        ):
            selector = str(raw.get("selector") or "").strip()
            if selector:
                return selector, selected
    return None


def viewport_ad_coverage(ui_snapshot: Mapping[str, object]) -> float | None:
    """仅按当前快照可验证几何计算，不信任任意预计算覆盖率。"""
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return None
    rectangles = []
    for raw in elements:
        if (
            not isinstance(raw, Mapping)
            or not _is_advertisement(raw)
            or raw.get("visible") is False
        ):
            continue
        if raw.get("visible") is not True:
            return None
        rectangles.append(raw.get("rect"))
    return viewport_rectangles_coverage(ui_snapshot.get("viewport"), rectangles)


def _post_scroll_block_reason(ui_snapshot: Mapping[str, object]) -> str | None:
    """滚动后新出现的安全阻断必须停止补图，不能伪装成干净主体证据。"""

    if ui_snapshot.get("human_verification_present") is True:
        return "WEB_PRIMARY_CONTENT_HUMAN_VERIFICATION_BLOCKED"
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return None
    for raw in elements:
        if not isinstance(raw, Mapping) or raw.get("visible") is not True:
            continue
        if raw.get("web_human_verification_present") is True:
            return "WEB_PRIMARY_CONTENT_HUMAN_VERIFICATION_BLOCKED"
        if str(raw.get("web_obstruction_layer_kind") or "").casefold() == "age_gate":
            return "WEB_PRIMARY_CONTENT_AGE_GATE_BLOCKED"
        if raw.get(
            "web_obstruction_layer_structural"
        ) is True and not _is_benign_ordinary_local_overlay(ui_snapshot, raw):
            if _is_ordinary_overlay(raw):
                return _WEB_PRIMARY_CONTENT_OBSTRUCTION_REVIEW_REQUIRED
            return "WEB_PRIMARY_CONTENT_OBSTRUCTION_APPEARED"
    return None


def _is_benign_ordinary_local_overlay(
    ui_snapshot: Mapping[str, object],
    element: Mapping[str, object],
) -> bool:
    """普通浮层与可信主体交叠不足 30% 时不得阻断。"""

    overlap = web_ordinary_overlay_subject_overlap(ui_snapshot, element)
    return bool(
        _is_ordinary_overlay(element)
        and overlap is not None
        and overlap < WEB_PRIMARY_CONTENT_ORDINARY_OVERLAY_REVIEW_OVERLAP
    )


def _has_only_benign_ordinary_overlays(ui_snapshot: Mapping[str, object]) -> bool:
    # 主体补证保持原结构浮层合同；新增只读 census 不能授予业务层可见性。
    if ui_snapshot.get("human_verification_present") is True:
        return False
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return False
    structural = [
        item for item in elements
        if isinstance(item, Mapping) and item.get("visible") is True
        and item.get("web_obstruction_layer_structural") is True
    ]
    return bool(structural) and all(
        _is_benign_ordinary_local_overlay(ui_snapshot, item) for item in structural
    )


def _is_ordinary_overlay(element: Mapping[str, object]) -> bool:
    return web_is_ordinary_overlay(element)


def _ordinary_overlay_subject_overlap(
    ui_snapshot: Mapping[str, object],
    element: Mapping[str, object],
) -> float | None:
    return web_ordinary_overlay_subject_overlap(ui_snapshot, element)


def _primary_content_intent(
    *,
    task_id: str,
    session_id: str,
    source_capture_id: str,
    target: WebPrimaryContentTarget,
    logical_page_id: str,
    deadline: datetime,
    attempt: int,
) -> ActionIntent:
    intent_id = f"web-primary-content:{source_capture_id}:{attempt}"
    return ActionIntent(
        task_id=task_id,
        session_id=session_id,
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=source_capture_id,
        action_type=InteractionOperation.SCROLL,
        semantic_target="滚动到非广告主体内容",
        purpose="滚动到非广告业务主体并生成补充截图",
        parameters={
            "target_mode": "web_primary_content_candidate",
            "web_primary_content_candidate_id": target.candidate_id,
            "web_primary_content_discovery_capture_id": source_capture_id,
            "web_primary_content_source_capture_id": source_capture_id,
            "expected_page_type": target.expected_page_type,
            "capture_purpose": WEB_PRIMARY_CONTENT_CAPTURE_PURPOSE,
            "web_capture_stage": WEB_PRIMARY_CONTENT_CAPTURE_PURPOSE,
            "logical_page_id": logical_page_id,
            "scroll_block": "center",
        },
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfile(
            profile_id="web-primary-content.v1",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=(
                    "screenshot",
                    "dom",
                    "visible_text",
                    "ui_snapshot",
                    "action_trace",
                    "har",
                    "media_state",
                ),
            ),
        ),
        deadline=deadline,
    )


def _primary_attempt(result: object, *, source_capture_id: str, attempt: int) -> dict[str, object]:
    """实际协调器回执引用，失败同样保留；不复制或伪造父 Capture。"""
    before = getattr(result, "before_capture", None)
    after = getattr(result, "after_capture", None)
    return {
        "attempt": attempt,
        "source_capture_id": source_capture_id,
        "before_capture_id": getattr(before, "capture_id", None),
        "after_capture_id": getattr(after, "capture_id", None),
        "action_execution_id": getattr(after, "action_execution_id", None)
        or getattr(before, "action_execution_id", None),
        "stage": str(getattr(result, "stage", "after_capture")),
        "error_code": getattr(result, "error_code", None),
    }


def _is_primary_content_element(element: Mapping[str, object]) -> bool:
    if element.get("visible") is not True or _is_advertisement(element):
        return False
    tag = str(element.get("tag") or "").casefold()
    tokens = _element_tokens(element)
    return not (
        tag == "iframe"
        or element.get("within_web_navigation") is True
        or any(
            element.get(flag) is True
            for flag in (
                "within_footer",
                "within_breadcrumb",
                "within_pagination",
            )
        )
        or any(marker in tokens for marker in ("footer", "pagination", "pager", "breadcrumb"))
    )


def _is_advertisement(element: Mapping[str, object]) -> bool:
    return element.get("is_advertisement") is True or any(
        marker in _element_tokens(element)
        for marker in ("advertisement", "sponsor", "sponsored", "promotion", "promo-banner")
    )


def _is_content_detail_element(element: Mapping[str, object]) -> bool:
    tag = str(element.get("tag") or "").casefold()
    tokens = _element_tokens(element)
    return tag == "main" or is_web_article_body_node(element) or any(
        marker in tokens
        for marker in (
            "article-body",
            "post-content",
            "entry-content",
            "image-gallery",
            "photo-gallery",
            "image-viewer",
            "photo-viewer",
            "lightbox",
        )
    )


def _is_video_detail_element(element: Mapping[str, object]) -> bool:
    tag = str(element.get("tag") or "").casefold()
    src = element.get("src")
    return (
        (tag == "video" or (tag == "source" and element.get("media_parent_tag") == "video"))
        and isinstance(src, str)
        and bool(src.strip())
    )


def _element_tokens(element: Mapping[str, object]) -> str:
    return " ".join(
        str(value).casefold()
        for value in (
            element.get("selector"),
            element.get("class_tokens"),
            element.get("ancestor_class_tokens"),
            element.get("role"),
        )
    )


def _element_fingerprint(element: Mapping[str, object]) -> str:
    existing = str(element.get("dom_fingerprint") or "").strip()
    if existing:
        return existing
    material: dict[str, Any] = {
        "selector": element.get("selector"),
        "tag": element.get("tag"),
        "class_tokens": element.get("class_tokens"),
    }
    # 祖先的滚动/浮动控件状态不是主体身份；区域与广告限制仍由候选筛选逐帧复验。
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


__all__ = [
    "WEB_PRIMARY_CONTENT_CAPTURE_PURPOSE",
    "WebPrimaryContentCaptureResult",
    "WebPrimaryContentCaptureService",
    "WebPrimaryContentTarget",
    "build_web_primary_content_targets",
    "resolve_web_primary_content_selector",
    "viewport_ad_coverage",
]
