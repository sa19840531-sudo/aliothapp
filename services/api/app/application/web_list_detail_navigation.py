"""从无遮挡列表 DOM 选择并采集一层可信详情。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.web_assessment_errors import (
    WebAssessmentPermissionDeniedError,
)
from services.api.app.application.web_assessment_evidence import (
    sanitize_web_model_reason,
)
from services.api.app.application.web_desktop_navigation import (
    catalog_navigation_url_matches,
)
from services.api.app.application.web_detail_candidate_extraction import (
    WebDetailCandidate,
    detail_candidates_from_ready_snapshot,
    validate_web_detail_origin,
)
from services.api.app.application.web_detail_selection_diagnostics import (
    detail_selection_failure_diagnostic,
)
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerPort,
)
from services.api.app.application.web_exploration_workflow import (
    RunWebWorkflowCommand,
    WebCaptureStage,
    web_capture_profile,
)
from services.api.app.application.web_interaction_facts import (
    web_capture_content_facts,
    web_command_accepted,
    web_incomplete_interaction,
    web_receipt_allows_arrival,
)
from services.api.app.application.web_interaction_precision import (
    WebInteractionOutcome,
    WebInteractionPlan,
    WebInteractionPostcondition,
    WebTargetBindingStatus,
    WebTargetObservation,
    assess_web_interaction_outcome,
)
from services.api.app.application.web_navigation_safety import (
    WebCaptureUrlReader,
    WebNavigationSafetyError,
    validate_web_final_url,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_readiness import (
    WebPageReadinessResult,
    web_readiness_result_is_ready,
)
from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
from services.api.app.application.web_subject_classification_audit import (
    with_detail_navigation_source,
)
from services.api.app.domain.exploration import (
    WEB_DETAIL_PAGE_TYPES,
    WebMenuTerminalStatus,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page import (
    WebPageClassificationHint,
    WebSiteMenuEntry,
)
from services.api.app.domain.protocol import require_aware_datetime

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class WebDetailSelectionAssessment:
    selected_candidate_id: str
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.selected_candidate_id, str) or not (
            candidate_id := self.selected_candidate_id.strip()
        ):
            raise ValueError("selected_candidate_id 不能为空")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0 <= float(self.confidence) <= 1
        ):
            raise ValueError("confidence 必须在 0 到 1 之间")
        reason = sanitize_web_model_reason(self.reason)
        object.__setattr__(self, "selected_candidate_id", candidate_id)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "reason", reason)


class WebDetailSelectionPort(Protocol):
    async def select_detail_candidate(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        candidates: tuple[dict[str, object], ...],
        list_page_type: str,
    ) -> WebDetailSelectionAssessment: ...


@dataclass(frozen=True, kw_only=True)
class WebListDetailEvidence:
    task_id: str
    agent_run_id: str
    session_id: str
    ready_capture_id: str
    screenshot_artifact_id: str
    dom_artifact_id: str
    origin: str
    current_url: str
    ui_snapshot: Mapping[str, object]
    deadline: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "ready_capture_id",
            "screenshot_artifact_id",
            "dom_artifact_id",
            "origin",
            "current_url",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
        validate_web_detail_origin(self.origin)
        validate_web_detail_origin(self.current_url)
        if not isinstance(self.ui_snapshot, Mapping):
            raise ValueError("ui_snapshot 必须为对象")
        if str(self.ui_snapshot.get("capture_id") or "") != self.ready_capture_id:
            raise ValueError("ui_snapshot 必须绑定 ready_capture_id")
        require_aware_datetime(self.deadline, "deadline")


class WebDetailCoordinator(Protocol):
    async def coordinate(self, command: CaptureCoordinationCommand) -> object: ...


class WebDetailReadiness(Protocol):
    async def prepare(
        self,
        *,
        source_capture_id: str,
        page_role: str,
    ) -> WebPageReadinessResult: ...


class WebReadyCaptureReader(Protocol):
    async def read(
        self,
        *,
        capture_id: str,
        page_role: str,
    ) -> WebReadyPageCapture: ...


class WebDetailTargetObservationReader(Protocol):
    async def read_detail_target_observation(
        self,
        *,
        capture_id: str,
        candidate: WebDetailCandidate,
        list_page_type: str,
        origin: str,
        current_url: str,
        previous_scroll_offset: float,
    ) -> WebTargetObservation: ...


class WebDetailClassifier(Protocol):
    async def classify(self, capture: WebReadyPageCapture) -> WebPageAssessment: ...


@dataclass(frozen=True, kw_only=True)
class WebListDetailResult:
    status: WebMenuTerminalStatus
    detail_capture_id: str | None
    page_type: str | None
    error_code: str | None = None
    selected_candidate_id: str | None = None
    detail_candidate_ids: tuple[str, ...] = ()
    detail_selection_confidence: float | None = None
    detail_selection_reason: str | None = None
    detail_selection_diagnostic: Mapping[str, object] | None = None
    probe_capture_id: str | None = None
    readiness_evidence_capture_ids: tuple[str, ...] = ()
    dismissed_layers: tuple[str, ...] = ()
    detail_action_count: int = 0
    detail_intent_id: str | None = None
    detail_action_execution_id: str | None = None
    diagnostic_type: str | None = None
    diagnostic_message: str | None = None
    interaction_outcome: WebInteractionOutcome | None = None
    detail_scroll_count: int = 0
    target_binding_status: str | None = None
    reused_capture_id: str | None = None
    reused_logical_page_id: str | None = None
    scroll_interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()
    readiness_interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.detail_action_count, bool)
            or not isinstance(self.detail_action_count, int)
            or not 0 <= self.detail_action_count <= 1
        ):
            raise ValueError("detail_action_count 必须为 0 或 1")
        if (
            isinstance(self.detail_scroll_count, bool)
            or not isinstance(self.detail_scroll_count, int)
            or not 0 <= self.detail_scroll_count <= 20
        ):
            raise ValueError("detail_scroll_count 必须在 0 到 20 之间")


class WebListDetailNavigator:
    """选择一个可信卡片，完成详情探测、清障和对应详情类型复验。"""

    def __init__(
        self,
        *,
        selector: WebDetailSelectionPort,
        coordinator: WebDetailCoordinator,
        readiness: WebDetailReadiness,
        ready_capture_reader: WebReadyCaptureReader,
        classifier: WebDetailClassifier,
        capture_url_reader: WebCaptureUrlReader,
        budget_ledger: WebExplorationBudgetLedgerPort | None = None,
        target_observation_reader: WebDetailTargetObservationReader | None = None,
        minimum_confidence: float = 0.7,
        max_scroll_steps: int = 3,
        logical_target_repository: object | None = None,
        identity_policy=None,
    ) -> None:
        self._selector = selector
        self._logical_target_repository = logical_target_repository
        from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY

        self._identity_policy = identity_policy or DEFAULT_WEB_URL_IDENTITY_POLICY
        self._coordinator = coordinator
        self._readiness = readiness
        self._ready_capture_reader = ready_capture_reader
        self._classifier = classifier
        self._capture_url_reader = capture_url_reader
        self._budget_ledger = budget_ledger
        self._target_observation_reader = target_observation_reader
        self._minimum_confidence = minimum_confidence
        if (
            isinstance(max_scroll_steps, bool)
            or not isinstance(max_scroll_steps, int)
            or not 0 <= max_scroll_steps <= 20
        ):
            raise ValueError("max_scroll_steps 必须在 0 到 20 之间")
        self._max_scroll_steps = max_scroll_steps

    async def capture_one(
        self,
        evidence: WebListDetailEvidence,
        *,
        list_page_type: str,
        command: RunWebWorkflowCommand | None = None,
        entry: WebSiteMenuEntry | WebRuntimeMenuEntry | None = None,
    ) -> WebListDetailResult:
        candidates = detail_candidates_from_ready_snapshot(
            evidence.ui_snapshot,
            list_page_type=list_page_type,
            origin=evidence.origin,
            current_url=evidence.current_url,
            identity_policy=self._identity_policy,
        )
        if not candidates:
            return WebListDetailResult(
                status=WebMenuTerminalStatus.NO_DETAIL_CANDIDATE,
                detail_capture_id=None,
                page_type=None,
                error_code="WEB_DETAIL_CANDIDATE_MISSING",
            )
        candidate_ids = tuple(item.candidate_id for item in candidates)
        try:
            assessment = await self._selector.select_detail_candidate(
                screenshot_artifact_id=evidence.screenshot_artifact_id,
                dom_artifact_id=evidence.dom_artifact_id,
                candidates=tuple(item.to_public_dict() for item in candidates),
                list_page_type=list_page_type,
            )
        except WebAssessmentPermissionDeniedError as exc:
            if exc.assessment_kind != "web_detail_selection":
                raise
            reason = "WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED"
            return _detail_failed(
                reason,
                status=WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
                detail_candidate_ids=candidate_ids,
                detail_selection_reason=reason,
                detail_selection_diagnostic=detail_selection_failure_diagnostic(
                    exc,
                    source_capture_id=evidence.ready_capture_id,
                    screenshot_artifact_id=evidence.screenshot_artifact_id,
                    dom_artifact_id=evidence.dom_artifact_id,
                    candidate_ids=candidate_ids,
                ),
            )
        except (TypeError, ValueError) as exc:
            return _detail_failed(
                "WEB_DETAIL_SELECTION_FAILED",
                status=WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
                detail_candidate_ids=candidate_ids,
                detail_selection_reason="详情候选结构化判定失败",
                detail_selection_diagnostic=detail_selection_failure_diagnostic(
                    exc,
                    source_capture_id=evidence.ready_capture_id,
                    screenshot_artifact_id=evidence.screenshot_artifact_id,
                    dom_artifact_id=evidence.dom_artifact_id,
                    candidate_ids=candidate_ids,
                ),
            )
        candidate = next(
            (item for item in candidates if item.candidate_id == assessment.selected_candidate_id),
            None,
        )
        if candidate is None or assessment.confidence < self._minimum_confidence:
            return _detail_failed(
                "WEB_DETAIL_SELECTION_UNTRUSTED",
                status=WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
                selected_candidate_id=assessment.selected_candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
            )
        if self._logical_target_repository is not None:
            known = await self._logical_target_repository.find(
                task_id=evidence.task_id,
                agent_run_id=evidence.agent_run_id,
                session_id=evidence.session_id,
                source_capture_id=evidence.ready_capture_id,
                url=candidate.canonical_url,
            )
            if known is not None:
                if known["state"] != "materialized":
                    raise ValueError("WEB_LOGICAL_TARGET_INCOMPLETE")
                return WebListDetailResult(
                    status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
                    detail_capture_id=None,
                    page_type=known["assessment"].get("page_type"),
                    selected_candidate_id=candidate.candidate_id,
                    detail_candidate_ids=candidate_ids,
                    reused_capture_id=known["assessment_capture_id"],
                    reused_logical_page_id=known["logical_page_id"],
                )
        if (
            self._budget_ledger is None
            or not isinstance(command, RunWebWorkflowCommand)
            or not isinstance(entry, WebSiteMenuEntry | WebRuntimeMenuEntry)
        ):
            raise RuntimeError("Web 详情预算账本、command 与 entry 必须同时配置")
        try:
            reservation = await self._budget_ledger.reserve_detail(
                command=command,
                entry=entry,
                source_capture_id=evidence.ready_capture_id,
                candidate_id=candidate.candidate_id,
                destination_url=candidate.canonical_url,
            )
        except Exception:  # noqa: BLE001 - reservation 漂移必须失败关闭且禁止发动作
            return _detail_failed(
                "WEB_BUDGET_RESERVED_NEEDS_REVIEW",
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                detail_action_count=1,
            )
        if reservation is None or reservation.already_reserved:
            return _detail_failed(
                "WEB_BUDGET_RESERVED_NEEDS_REVIEW",
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                detail_action_count=1,
                detail_intent_id=(reservation.intent_id if reservation is not None else None),
            )
        try:
            navigation = await self._navigate_candidate(
                evidence,
                candidate,
                list_page_type,
                intent_id=reservation.intent_id,
                navigation_depth=_detail_navigation_depth(entry),
            )
        except WebDetailTargetBindingError as exc:
            return _detail_failed(
                "WEB_DETAIL_TARGET_BINDING_FAILED",
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                probe_capture_id=exc.capture_id,
                detail_action_count=0,
                detail_intent_id=reservation.intent_id,
                detail_scroll_count=exc.scroll_count,
                scroll_interaction_outcomes=exc.scroll_outcomes,
                target_binding_status=exc.status.value,
            )
        except CaptureCoordinationError as exc:
            return _detail_failed(
                "WEB_DETAIL_NAVIGATION_FAILED",
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                probe_capture_id=(str(getattr(exc.after_capture, "capture_id", "") or "") or None),
                detail_action_count=1,
                detail_intent_id=reservation.intent_id,
                detail_action_execution_id=_action_execution_id(exc.interaction),
                interaction_outcome=web_incomplete_interaction(
                    exc.interaction,
                    str(getattr(exc.after_capture, "capture_id", "") or "") or None,
                    exc.error_code,
                ),
            )
        except Exception:  # noqa: BLE001 - reservation 已消费，禁止再次发详情动作
            return _detail_failed(
                "WEB_DETAIL_NAVIGATION_FAILED",
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                detail_action_count=1,
                detail_intent_id=reservation.intent_id,
            )
        probe_capture_id = navigation.probe_capture_id
        detail_action_execution_id = navigation.action_execution_id
        interaction_outcome = web_incomplete_interaction(
            navigation.receipt, probe_capture_id, "after_observation_pending"
        )

        def post_navigation_failed(
            error_code: str,
            readiness_result: WebPageReadinessResult | None = None,
            diagnostic_type: str | None = None,
            diagnostic_message: str | None = None,
        ) -> WebListDetailResult:
            return _detail_failed(
                error_code,
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                probe_capture_id=probe_capture_id,
                detail_capture_id=(readiness_result.ready_capture_id if readiness_result else None),
                readiness_evidence_capture_ids=(
                    readiness_result.evidence_capture_ids if readiness_result else ()
                ),
                dismissed_layers=(readiness_result.dismissed_layers if readiness_result else ()),
                readiness_interaction_outcomes=(
                    readiness_result.interaction_outcomes if readiness_result else ()
                ),
                detail_action_count=1,
                detail_intent_id=reservation.intent_id,
                detail_action_execution_id=detail_action_execution_id,
                diagnostic_type=diagnostic_type,
                diagnostic_message=diagnostic_message,
                interaction_outcome=interaction_outcome,
                detail_scroll_count=navigation.scroll_count,
                scroll_interaction_outcomes=navigation.scroll_outcomes,
                target_binding_status=navigation.target_binding_status.value,
            )

        try:
            final_url = await self._capture_url_reader.read_url(capture_id=probe_capture_id)
            after_content_identity = None
            read_snapshot = getattr(self._capture_url_reader, "read_snapshot", None)
            if callable(read_snapshot):
                after_content_identity = web_capture_content_facts(
                    await read_snapshot(capture_id=probe_capture_id),
                    url=final_url,
                ).content_identity
        except Exception as exc:  # noqa: BLE001 - Navigate 已完成，失败账本必须保留一次动作
            return _detail_stage_failed(
                error_code="WEB_DETAIL_URL_READ_FAILED",
                stage="url_read",
                exc=exc,
                build_result=post_navigation_failed,
            )
        try:
            validate_web_final_url(
                site_url=evidence.origin,
                requested_url=candidate.canonical_url,
                final_url=final_url,
            )
        except WebNavigationSafetyError as exc:
            return WebListDetailResult(
                status=WebMenuTerminalStatus.UNSAFE_NAVIGATION,
                detail_capture_id=None,
                page_type=None,
                error_code=exc.error_code,
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                probe_capture_id=probe_capture_id,
                detail_action_count=1,
                detail_intent_id=reservation.intent_id,
                detail_action_execution_id=detail_action_execution_id,
                interaction_outcome=interaction_outcome,
            )
        target_arrived = catalog_navigation_url_matches(
            candidate.canonical_url,
            final_url,
        )
        interaction_outcome = assess_web_interaction_outcome(
            plan=navigation.plan,
            command_accepted=web_command_accepted(navigation.receipt),
            before_url=evidence.current_url,
            after_url=final_url,
            before_content_identity=str(evidence.ui_snapshot.get("structure_signature") or "")
            or None,
            after_content_identity=after_content_identity,
            postcondition_satisfied=target_arrived,
            after_capture_id=probe_capture_id,
            readiness_accepted=False,
            identity_verified=False,
            business_layer_visible=False,
        )
        try:
            readiness = await self._readiness.prepare(
                source_capture_id=probe_capture_id,
                page_role="detail",
            )
        except Exception as exc:  # noqa: BLE001 - Navigate 已完成，失败账本必须保留一次动作
            return _detail_stage_failed(
                error_code="WEB_DETAIL_READINESS_FAILED",
                stage="readiness",
                exc=exc,
                build_result=post_navigation_failed,
            )
        if not web_readiness_result_is_ready(readiness):
            return WebListDetailResult(
                status=(
                    WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED
                    if readiness.status == "human_verification_blocked"
                    else WebMenuTerminalStatus.POPUP_BLOCKED
                    if readiness.status == "popup_blocked"
                    else WebMenuTerminalStatus.NAVIGATION_FAILED
                ),
                detail_capture_id=None,
                page_type=None,
                error_code=(
                    "WEB_DETAIL_HUMAN_VERIFICATION_BLOCKED"
                    if readiness.status == "human_verification_blocked"
                    else "WEB_DETAIL_POPUP_BLOCKED"
                    if readiness.status == "popup_blocked"
                    else "WEB_DETAIL_READINESS_FAILED"
                ),
                diagnostic_type=f"readiness_{readiness.status}",
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                probe_capture_id=probe_capture_id,
                readiness_evidence_capture_ids=readiness.evidence_capture_ids,
                dismissed_layers=readiness.dismissed_layers,
                readiness_interaction_outcomes=readiness.interaction_outcomes,
                detail_action_count=1,
                detail_intent_id=reservation.intent_id,
                detail_action_execution_id=detail_action_execution_id,
                interaction_outcome=interaction_outcome,
                detail_scroll_count=navigation.scroll_count,
                scroll_interaction_outcomes=navigation.scroll_outcomes,
                target_binding_status=navigation.target_binding_status.value,
            )
        try:
            ready_capture = await self._ready_capture_reader.read(
                capture_id=readiness.ready_capture_id,
                page_role="detail",
            )
            ready_capture = replace(
                ready_capture,
                structure_facts=replace(
                    ready_capture.structure_facts,
                    subject_region_facts=with_detail_navigation_source(
                        ready_capture.structure_facts.subject_region_facts, candidate.selector
                    ),
                ),
                menu_entry_ids=(entry.entry_id,),
                classification_hint=WebPageClassificationHint(
                    expected_page_type=candidate.expected_page_type,
                    confidence=0.8,
                    source="trusted_list_detail_transition",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - Navigate 已完成，失败账本必须保留一次动作
            return _detail_stage_failed(
                error_code="WEB_DETAIL_EVIDENCE_READ_FAILED",
                stage="evidence_read",
                exc=exc,
                build_result=post_navigation_failed,
                readiness_result=readiness,
            )
        try:
            page = await self._classifier.classify(ready_capture)
        except Exception as exc:  # noqa: BLE001 - Navigate 已完成，失败账本必须保留一次动作
            return _detail_stage_failed(
                error_code="WEB_DETAIL_CLASSIFICATION_FAILED",
                stage="classification",
                exc=exc,
                build_result=post_navigation_failed,
                readiness_result=readiness,
            )
        facts = ready_capture.interaction_facts
        target_arrived = bool(
            facts
            and (
                catalog_navigation_url_matches(candidate.canonical_url, facts.url)
                or candidate.canonical_url in facts.subject_urls
            )
        )
        matches_target = getattr(self._capture_url_reader, "matches_target", None)
        if not target_arrived and callable(matches_target):
            target_arrived = await matches_target(
                capture_id=readiness.ready_capture_id, expected_url=candidate.canonical_url
            )
        interaction_outcome = assess_web_interaction_outcome(
            plan=navigation.plan,
            command_accepted=web_command_accepted(navigation.receipt),
            before_url=evidence.current_url,
            after_url=facts.url if facts else final_url,
            before_content_identity=web_capture_content_facts(
                evidence.ui_snapshot, url=evidence.current_url
            ).content_identity,
            after_content_identity=facts.content_identity if facts else None,
            postcondition_satisfied=target_arrived and page.page_type in WEB_DETAIL_PAGE_TYPES,
            after_capture_id=readiness.ready_capture_id,
            readiness_accepted=bool(facts and facts.readiness_accepted),
            identity_verified=bool(
                facts
                and facts.identity_verified
                and target_arrived
                and web_receipt_allows_arrival(navigation.receipt)
            ),
            business_layer_visible=bool(facts and facts.business_layer_visible),
            **({"evidence_mode": facts.evidence_mode} if facts else {}),
        )
        if page.page_type not in WEB_DETAIL_PAGE_TYPES:
            return _detail_failed(
                "WEB_DETAIL_PAGE_TYPE_MISMATCH",
                status=WebMenuTerminalStatus.DETAIL_PAGE_TYPE_MISMATCH,
                selected_candidate_id=candidate.candidate_id,
                detail_candidate_ids=candidate_ids,
                detail_selection_confidence=assessment.confidence,
                detail_selection_reason=assessment.reason,
                probe_capture_id=probe_capture_id,
                detail_capture_id=readiness.ready_capture_id,
                page_type=page.page_type,
                readiness_evidence_capture_ids=readiness.evidence_capture_ids,
                dismissed_layers=readiness.dismissed_layers,
                readiness_interaction_outcomes=readiness.interaction_outcomes,
                detail_action_count=1,
                detail_intent_id=reservation.intent_id,
                detail_action_execution_id=detail_action_execution_id,
                interaction_outcome=interaction_outcome,
            )
        if not interaction_outcome.transition_arrived:
            return post_navigation_failed("WEB_DETAIL_POSTCONDITION_NOT_SATISFIED", readiness)
        return WebListDetailResult(
            status=(
                WebMenuTerminalStatus.LOGICAL_TARGET_REUSED
                if page.reused_capture_id
                else WebMenuTerminalStatus.DETAIL_CAPTURED
            ),
            reused_capture_id=page.reused_capture_id,
            reused_logical_page_id=page.reused_logical_page_id,
            detail_capture_id=readiness.ready_capture_id,
            page_type=page.page_type,
            selected_candidate_id=candidate.candidate_id,
            detail_candidate_ids=candidate_ids,
            detail_selection_confidence=assessment.confidence,
            detail_selection_reason=assessment.reason,
            probe_capture_id=probe_capture_id,
            readiness_evidence_capture_ids=readiness.evidence_capture_ids,
            dismissed_layers=readiness.dismissed_layers,
            readiness_interaction_outcomes=readiness.interaction_outcomes,
            detail_action_count=1,
            detail_intent_id=reservation.intent_id,
            detail_action_execution_id=detail_action_execution_id,
            interaction_outcome=interaction_outcome,
            detail_scroll_count=navigation.scroll_count,
            scroll_interaction_outcomes=navigation.scroll_outcomes,
            target_binding_status=navigation.target_binding_status.value,
        )

    async def _navigate_candidate(
        self,
        evidence: WebListDetailEvidence,
        candidate: WebDetailCandidate,
        list_page_type: str,
        *,
        intent_id: str,
        navigation_depth: int,
    ) -> _DetailNavigationResult:
        initial_intent = ActionIntent(
            task_id=evidence.task_id,
            session_id=evidence.session_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=evidence.ready_capture_id,
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="打开当前列表可信详情",
            purpose="采集当前白名单菜单列表的一层可信详情",
            parameters={
                "target_mode": "web_detail_candidate_url",
                "web_detail_candidate_id": candidate.candidate_id,
                "web_detail_source_capture_id": evidence.ready_capture_id,
                "web_detail_list_page_type": list_page_type,
                "web_detail_origin": evidence.origin,
                "web_detail_current_url": evidence.current_url,
                "destination_url": candidate.canonical_url,
                # 详情直达是预期跨页动作，必须显式声明从入口页计算的层级；
                # runtime 菜单可无限加深，因此由当前菜单深度单调递增。
                "navigation_depth": navigation_depth,
                "web_capture_stage": WebCaptureStage.PAGE_PROBE.value,
                "clear_obstructions": False,
            },
            risk_level=InteractionRiskLevel.LOW,
            capture_profile=web_capture_profile("web-list-detail.v1"),
            deadline=evidence.deadline,
        )
        source_epoch = str(evidence.ui_snapshot.get("document_epoch") or "").strip()
        remaining_seconds = (
            evidence.deadline - datetime.now(evidence.deadline.tzinfo)
        ).total_seconds()
        plan = WebInteractionPlan(
            intent=initial_intent,
            source_capture_id=evidence.ready_capture_id,
            source_epoch=source_epoch or evidence.ready_capture_id,
            target_ref=candidate.candidate_id,
            max_scroll_steps=0,
            deadline_seconds=max(0.001, min(300.0, remaining_seconds)),
            postcondition=WebInteractionPostcondition.DETAIL_ARRIVAL,
        )
        intent = initial_intent
        result = await self._coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=evidence.agent_run_id,
                intent=intent,
            )
        )
        capture_id = str(getattr(getattr(result, "after_capture", None), "capture_id", "") or "")
        if not capture_id:
            raise RuntimeError("详情导航后缺少 Capture")
        action_execution_id = _action_execution_id(getattr(result, "interaction", None))
        if action_execution_id is None:
            raise RuntimeError("详情导航后缺少实际 ActionExecution ID")
        return _DetailNavigationResult(
            probe_capture_id=capture_id,
            action_execution_id=action_execution_id,
            plan=plan,
            scroll_count=0,
            target_binding_status=WebTargetBindingStatus.READY,
            scroll_outcomes=(),
            receipt=getattr(result, "interaction", None),
        )


def _detail_navigation_depth(entry: WebSiteMenuEntry | WebRuntimeMenuEntry) -> int:
    """Catalog 菜单按一级动作处理，runtime 子菜单沿真实深度再进入一层详情。"""

    if isinstance(entry, WebRuntimeMenuEntry):
        return max(1, int(entry.menu_depth)) + 1
    return 2


class WebDetailTargetBindingError(RuntimeError):
    """详情目标在有界滚动后仍不能安全绑定。"""

    def __init__(
        self,
        *,
        status: WebTargetBindingStatus,
        capture_id: str,
        scroll_count: int,
        scroll_outcomes: tuple[WebInteractionOutcome, ...] = (),
    ) -> None:
        super().__init__(f"Web 详情目标绑定失败：{status.value}")
        self.status = status
        self.capture_id = capture_id
        self.scroll_count = scroll_count
        self.scroll_outcomes = scroll_outcomes


def build_web_detail_target_observation(
    ui_snapshot: Mapping[str, object],
    *,
    capture_id: str,
    candidate: WebDetailCandidate,
    previous_scroll_offset: float | None = None,
) -> WebTargetObservation:
    """从指定 Capture 构造详情目标绑定事实，离屏目标不会提前授权点击。"""

    raw_elements = ui_snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    matching = [
        item
        for item in elements
        if isinstance(item, Mapping)
        and str(item.get("selector") or "").strip() == candidate.selector
    ]
    target = matching[0] if len(matching) == 1 else {}
    rect = target.get("rect") if isinstance(target, Mapping) else None
    rect = rect if isinstance(rect, Mapping) else {}
    viewport = ui_snapshot.get("viewport")
    viewport = viewport if isinstance(viewport, Mapping) else {}
    width = _nonnegative_number(viewport.get("width"), default=1920.0)
    height = _nonnegative_number(viewport.get("height"), default=1080.0)
    # 坐标允许为负，保留离屏事实，绝不能钳制为零后授权点击。
    x = rect.get("x") if type(rect.get("x")) in (int, float) else float("nan")
    y = rect.get("y") if type(rect.get("y")) in (int, float) else float("nan")
    rect_width = _nonnegative_number(rect.get("width"), default=0.0)
    rect_height = _nonnegative_number(rect.get("height"), default=0.0)
    center_in_viewport = (
        rect_width > 0
        and rect_height > 0
        and x >= 0
        and y >= 0
        and 0 <= x + rect_width / 2 < width
        and 0 <= y + rect_height / 2 < height
    )
    hit_test = target.get("hit_test") if isinstance(target, Mapping) else None
    hit_test = hit_test if isinstance(hit_test, Mapping) else {}
    document = ui_snapshot.get("document")
    document = document if isinstance(document, Mapping) else {}
    scroll_offset = _nonnegative_number(viewport.get("scroll_y"), default=0.0)
    scroll_height = _nonnegative_number(
        document.get("scroll_height"),
        default=max(height, y + rect_height),
    )
    epoch = str(ui_snapshot.get("document_epoch") or "").strip()
    return WebTargetObservation(
        capture_id=capture_id,
        source_epoch=epoch or capture_id,
        matching_target_count=len(matching),
        target_visible=bool(target.get("visible") is True and center_in_viewport),
        target_enabled=bool(
            target.get("interactive") is True and target.get("disabled") is not True
        ),
        target_hit_test_passed=bool(
            center_in_viewport
            and hit_test.get("center_hits_target") is True
            and hit_test.get("visual_occlusion_detected") is not True
        ),
        scroll_offset=scroll_offset,
        previous_scroll_offset=previous_scroll_offset,
        at_scroll_boundary=scroll_offset + height >= scroll_height - 2,
    )


def _nonnegative_number(value: object, *, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    number = float(value)
    return number if number >= 0 else default


def _detail_failed(
    error_code: str,
    *,
    status: WebMenuTerminalStatus = WebMenuTerminalStatus.NAVIGATION_FAILED,
    selected_candidate_id: str | None = None,
    detail_candidate_ids: tuple[str, ...] = (),
    detail_selection_confidence: float | None = None,
    detail_selection_reason: str | None = None,
    detail_selection_diagnostic: Mapping[str, object] | None = None,
    probe_capture_id: str | None = None,
    detail_capture_id: str | None = None,
    page_type: str | None = None,
    readiness_evidence_capture_ids: tuple[str, ...] = (),
    dismissed_layers: tuple[str, ...] = (),
    detail_action_count: int = 0,
    detail_intent_id: str | None = None,
    detail_action_execution_id: str | None = None,
    diagnostic_type: str | None = None,
    diagnostic_message: str | None = None,
    interaction_outcome: WebInteractionOutcome | None = None,
    detail_scroll_count: int = 0,
    target_binding_status: str | None = None,
    scroll_interaction_outcomes: tuple[WebInteractionOutcome, ...] = (),
    readiness_interaction_outcomes: tuple[WebInteractionOutcome, ...] = (),
) -> WebListDetailResult:
    return WebListDetailResult(
        status=status,
        detail_capture_id=detail_capture_id,
        page_type=page_type,
        error_code=error_code,
        selected_candidate_id=selected_candidate_id,
        detail_candidate_ids=detail_candidate_ids,
        detail_selection_confidence=detail_selection_confidence,
        detail_selection_reason=detail_selection_reason,
        detail_selection_diagnostic=detail_selection_diagnostic,
        probe_capture_id=probe_capture_id,
        readiness_evidence_capture_ids=readiness_evidence_capture_ids,
        dismissed_layers=dismissed_layers,
        detail_action_count=detail_action_count,
        detail_intent_id=detail_intent_id,
        detail_action_execution_id=detail_action_execution_id,
        diagnostic_type=diagnostic_type,
        diagnostic_message=diagnostic_message,
        interaction_outcome=interaction_outcome,
        detail_scroll_count=detail_scroll_count,
        target_binding_status=target_binding_status,
        scroll_interaction_outcomes=scroll_interaction_outcomes,
        readiness_interaction_outcomes=readiness_interaction_outcomes,
    )


def _detail_stage_failed(
    *,
    error_code: str,
    stage: str,
    exc: Exception,
    build_result: object,
    readiness_result: WebPageReadinessResult | None = None,
) -> WebListDetailResult:
    """详情动作后按稳定阶段码收口，异常细节只留在受控日志。"""

    _LOGGER.exception(
        "Web 详情阶段失败 stage=%s error_type=%s",
        stage,
        type(exc).__name__,
    )
    if not callable(build_result):
        raise TypeError("详情失败结果构造器必须可调用")
    messages = {
        "url_read": "详情 URL 读取失败",
        "readiness": "详情页就绪准备失败",
        "evidence_read": "详情页证据读取失败",
        "classification": "详情页分类失败",
    }
    return build_result(
        error_code,
        readiness_result,
        diagnostic_type=type(exc).__name__[:128],
        diagnostic_message=messages[stage],
    )


@dataclass(frozen=True, kw_only=True)
class _DetailNavigationResult:
    probe_capture_id: str
    action_execution_id: str
    plan: WebInteractionPlan
    scroll_count: int
    target_binding_status: WebTargetBindingStatus
    scroll_outcomes: tuple[WebInteractionOutcome, ...] = ()
    receipt: object | None = None


def _action_execution_id(interaction: object | None) -> str | None:
    value = str(getattr(interaction, "action_execution_id", "") or "").strip()
    return value or None


__all__ = [
    "WebDetailCandidate",
    "WebDetailSelectionAssessment",
    "WebDetailSelectionPort",
    "WebListDetailEvidence",
    "WebListDetailNavigator",
    "WebListDetailResult",
    "build_web_detail_target_observation",
    "detail_candidates_from_ready_snapshot",
]
